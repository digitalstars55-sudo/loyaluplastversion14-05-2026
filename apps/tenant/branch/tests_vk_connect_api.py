"""
Тесты «подключения ВКонтакте» для кабинета CheckUp (контракт платформы, №55).

Стратегия патчей — как в senler/tests_broadcasts_api.py и
branch/tests_story_settings_api.py: модели тенантные, в тестовую БД не ходим.
Вьюхи импортируют модели и функции ВК на уровне модуля, поэтому подменяем их
прямо в `apps.tenant.branch.api.vk_connect`. Настоящих вызовов ВК здесь нет ни
одного — `requests.post` патчится даже в тестах сервисов.

Что стережём:
  1) ни одна ручка не отдаёт токен, секрет и строку подтверждения — проверка
     идёт по СЫРОЙ строке ответа, а не по ключам: новое поле с секретом внутри
     обязано уронить тест;
  2) `confirm: true` на смену токена/группы/секрета — без него ошибка;
  3) секрет раскладывается по ВСЕМ конфигам группы (тот самый инцидент с
     разъехавшимися секретами, из-за которого ВК отключил callback), а сводка
     показывает `secrets_consistent: false`, пока они разные;
  4) отказ ВК = 424, недоступность ВК = 504, и никогда 500;
  5) проверка (`.../vk/check/`) не пишет НИЧЕГО;
  6) роль `client` не правит и не проверяет, чужая точка — 404.
"""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant.branch.api import vk_connect as VC
from apps.tenant.senler.services import (
    VkApiError, vk_callback_confirmation_code, vk_group_info, vk_token_permissions,
)

VK = 'apps.tenant.branch.api.vk_connect.'

TOKEN = 'vk1.a.SUPERSECRETCOMMUNITYTOKEN9876'
OTHER_TOKEN = 'vk1.a.ANOTHERSECRETTOKENVALUE1234'
SECRET = 'secret-callback-string'
CONFIRMATION = 'ab12cd34'


# ── мини-ORM для моков ───────────────────────────────────────────────────────

def _match(obj, kw) -> bool:
    for key, val in kw.items():
        if key.endswith('__in'):
            if getattr(obj, key[:-4], None) not in val:
                return False
        elif getattr(obj, key, None) != val:
            return False
    return True


class _QS:
    """Ровно тот кусок QuerySet, который трогают вьюхи."""

    def __init__(self, items):
        self._items = list(items)

    def filter(self, **kw):
        return _QS([o for o in self._items if _match(o, kw)])

    def exclude(self, **kw):
        return _QS([o for o in self._items if not _match(o, kw)])

    def select_related(self, *a, **kw):
        return self

    def order_by(self, *a):
        return self

    def all(self):
        return self

    def first(self):
        return self._items[0] if self._items else None

    def exists(self):
        return bool(self._items)

    def count(self):
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)


class _Manager(_QS):
    def __init__(self, items=(), factory=None):
        super().__init__(items)
        self._factory = factory
        self.created = []

    def create(self, **kw):
        obj = self._factory(**kw)
        self.created.append(kw)
        self._items.append(obj)
        return obj


def _branch(pk=1, branch_id=101, name='Центральная'):
    return SimpleNamespace(pk=pk, id=pk, branch_id=branch_id, name=name)


class _Cfg:
    """SenlerConfig-подобный объект: пишет, что и когда у него сохраняли."""

    def __init__(self, branch=None, vk_group_id=None, vk_community_token='',
                 is_active=True, vk_callback_secret='', vk_callback_confirmation='',
                 notes='', updated_at=None):
        self.branch = branch
        self.branch_id = getattr(branch, 'pk', None)
        self.pk = getattr(branch, 'pk', None)
        self.vk_group_id = vk_group_id
        self.vk_community_token = vk_community_token
        self.is_active = is_active
        self.vk_callback_secret = vk_callback_secret
        self.vk_callback_confirmation = vk_callback_confirmation
        self.notes = notes
        self.updated_at = updated_at
        self.saved = []

    def save(self, update_fields=None):
        self.saved.append(sorted(update_fields or []))


def _user(role='network_admin', is_superuser=False):
    return SimpleNamespace(
        is_authenticated=True, is_active=True, is_superuser=is_superuser,
        is_superadmin=(role == 'superadmin'), is_network_admin=(role == 'network_admin'),
        is_client=(role == 'client'), role=role, username='t', pk=1, is_staff=True,
    )


def _call(view_cls, method, path, *, data=None, user=None, anonymous=False, **kwargs):
    factory = APIRequestFactory()
    if method == 'get':
        request = factory.get(path)
    else:
        request = getattr(factory, method)(path, data if data is not None else {}, format='json')
    if not anonymous:
        force_authenticate(request, user=user or _user())
    return view_cls.as_view()(request, **kwargs)


def _body(resp) -> str:
    """Ответ строкой — чтобы искать секреты где угодно, а не только в ключах."""
    return repr(resp.data)


class _Env:
    """Набор патчей модуля вьюх: модели, ВК, домен, транзакция, аудит."""

    def __init__(self, test, *, branches, configs, all_configs=None,
                 permissions=('messages', 'manage'), group=None,
                 vk_exc=None, servers=(), has_history=False, scope=None):
        self.branch_manager = _Manager(branches)
        self.config_manager = _Manager(all_configs if all_configs is not None else configs,
                                       factory=_Cfg)
        self.record_event = MagicMock()
        self.send_model = MagicMock()
        self.send_model.objects.filter.return_value.exists.return_value = has_history
        self.conv_model = MagicMock()
        self.conv_model.objects.filter.return_value.exists.return_value = has_history

        group = group if group is not None else {
            'id': 111, 'name': 'Кафе', 'screen_name': 'cafe', 'photo': 'http://p/1.jpg'}
        self.perm_mock = MagicMock(side_effect=vk_exc) if vk_exc else \
            MagicMock(return_value=list(permissions))
        self.group_mock = MagicMock(side_effect=vk_exc) if vk_exc else \
            MagicMock(return_value=group)
        self.servers_mock = MagicMock(return_value=list(servers))

        patches = [
            patch(VK + 'Branch', SimpleNamespace(objects=self.branch_manager)),
            patch(VK + 'SenlerConfig', SimpleNamespace(objects=self.config_manager)),
            patch(VK + 'BroadcastSend', self.send_model),
            patch(VK + 'TestimonialConversation', self.conv_model),
            patch(VK + 'effective_branch_ids', return_value=scope),
            patch(VK + 'current_schema_name', return_value='levone'),
            patch(VK + '_primary_domain', return_value='levone.levelupapp.ru'),
            patch(VK + '_atomic', side_effect=lambda: nullcontext()),
            patch(VK + 'vk_token_permissions', self.perm_mock),
            patch(VK + 'vk_group_info', self.group_mock),
            patch(VK + 'fetch_vk_callback_servers', self.servers_mock),
            patch('apps.shared.audit.services.record_event', self.record_event),
        ]
        for p in patches:
            p.start()
            test.addCleanup(p.stop)


# ── 1. Аутентификация ────────────────────────────────────────────────────────

class RequiresAuthTest(SimpleTestCase):
    """Каждая ручка закрыта IsAuthenticated (DRF по умолчанию = AllowAny)."""

    VIEWS = (VC.NetworkVkSettingsAPIView, VC.BranchVkAPIView, VC.BranchVkCheckAPIView)

    def test_permission_classes(self):
        for view in self.VIEWS:
            with self.subTest(view=view.__name__):
                self.assertIn(IsAuthenticated, view.permission_classes)

    def test_anonymous_is_rejected(self):
        cases = [
            (VC.NetworkVkSettingsAPIView, 'get', '/api/v1/settings/vk/', {}),
            (VC.BranchVkAPIView, 'get', '/api/v1/mobile/branches/1/vk/', {'pk': 1}),
            (VC.BranchVkAPIView, 'patch', '/api/v1/mobile/branches/1/vk/', {'pk': 1}),
            (VC.BranchVkCheckAPIView, 'post', '/api/v1/mobile/branches/1/vk/check/', {'pk': 1}),
        ]
        for view, method, path, kwargs in cases:
            with self.subTest(view=view.__name__, method=method):
                resp = _call(view, method, path, anonymous=True, **kwargs)
                self.assertIn(resp.status_code, (401, 403))


# ── 2. Секреты наружу не выходят ─────────────────────────────────────────────

class NoSecretsInResponsesTest(SimpleTestCase):
    """
    Ни токен, ни секрет, ни строка подтверждения не встречаются в ответе.

    Ищем по СЫРОЙ строке ответа: если завтра кто-то добавит поле
    `vk_community_token` «для удобства кабинета» — тест упадёт.
    """

    def setUp(self):
        self.branch = _branch()
        self.cfg = _Cfg(branch=self.branch, vk_group_id=111, vk_community_token=TOKEN,
                        vk_callback_secret=SECRET, vk_callback_confirmation=CONFIRMATION,
                        notes='рабочее сообщество')
        self.env = _Env(self, branches=[self.branch], configs=[self.cfg])

    def _assert_clean(self, resp):
        raw = _body(resp)
        for secret in (TOKEN, SECRET, CONFIRMATION):
            self.assertNotIn(secret, raw)

    def test_summary(self):
        resp = _call(VC.NetworkVkSettingsAPIView, 'get', '/api/v1/settings/vk/')
        self.assertEqual(resp.status_code, 200)
        self._assert_clean(resp)
        row = resp.data['branches'][0]
        self.assertTrue(row['token_set'] and row['secret_set'] and row['confirmation_set'])
        self.assertEqual(row['token_last4'], TOKEN[-4:])
        self.assertEqual(row['callback_url'],
                         'https://levone.levelupapp.ru/api/v1/vk/callback/')

    def test_card(self):
        resp = _call(VC.BranchVkAPIView, 'get', '/api/v1/mobile/branches/1/vk/', pk=1)
        self.assertEqual(resp.status_code, 200)
        self._assert_clean(resp)
        self.assertEqual(resp.data['branch'], {'id': 1, 'branch_id': 101, 'name': 'Центральная'})
        self.assertTrue(resp.data['connected'])
        self.assertEqual(resp.data['vk_group_id'], 111)
        self.assertEqual(resp.data['group_shared_with'], [])

    def test_patch(self):
        resp = _call(VC.BranchVkAPIView, 'patch', '/api/v1/mobile/branches/1/vk/',
                     data={'vk_callback_secret': 'new-secret-value', 'confirm': True}, pk=1)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertNotIn('new-secret-value', _body(resp))
        self._assert_clean(resp)

    def test_check(self):
        resp = _call(VC.BranchVkCheckAPIView, 'post',
                     '/api/v1/mobile/branches/1/vk/check/', data={}, pk=1)
        self.assertEqual(resp.status_code, 200, resp.data)
        self._assert_clean(resp)

    def test_audit_meta_has_field_names_only(self):
        _call(VC.BranchVkAPIView, 'patch', '/api/v1/mobile/branches/1/vk/',
              data={'notes': 'заметка'}, pk=1)
        self.assertTrue(self.env.record_event.called)
        meta = self.env.record_event.call_args.kwargs['meta']
        self.assertEqual(meta['fields'], ['notes'])
        self.assertNotIn(TOKEN, repr(meta))
        self.assertNotIn(SECRET, repr(meta))


# ── 3. Роли и чужие точки ────────────────────────────────────────────────────

class RoleAndScopeTest(SimpleTestCase):

    def setUp(self):
        self.branch = _branch()
        self.cfg = _Cfg(branch=self.branch, vk_group_id=111, vk_community_token=TOKEN)
        self.env = _Env(self, branches=[self.branch], configs=[self.cfg])

    def test_client_may_read_card(self):
        resp = _call(VC.BranchVkAPIView, 'get', '/api/v1/mobile/branches/1/vk/',
                     user=_user('client'), pk=1)
        self.assertEqual(resp.status_code, 200)

    def test_client_may_not_patch(self):
        resp = _call(VC.BranchVkAPIView, 'patch', '/api/v1/mobile/branches/1/vk/',
                     data={'notes': 'x'}, user=_user('client'), pk=1)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data['code'], 'role_not_allowed')
        self.assertEqual(self.cfg.saved, [])

    def test_client_may_not_check(self):
        resp = _call(VC.BranchVkCheckAPIView, 'post', '/api/v1/mobile/branches/1/vk/check/',
                     data={}, user=_user('client'), pk=1)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.data['code'], 'role_not_allowed')
        self.env.perm_mock.assert_not_called()

    def test_foreign_branch_is_404_everywhere(self):
        # Сотруднику доступна только точка 2 — про точку 1 он не узнаёт ничего.
        _Env(self, branches=[self.branch], configs=[self.cfg], scope=[2])
        for view, method, data in ((VC.BranchVkAPIView, 'get', None),
                                   (VC.BranchVkAPIView, 'patch', {'notes': 'x'}),
                                   (VC.BranchVkCheckAPIView, 'post', {})):
            with self.subTest(view=view.__name__, method=method):
                resp = _call(view, method, '/api/v1/mobile/branches/1/vk/', data=data, pk=1)
                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.data['code'], 'not_found')

    def test_summary_lists_only_allowed_branches(self):
        other = _branch(pk=2, branch_id=202, name='Институтская')
        _Env(self, branches=[self.branch, other], configs=[self.cfg], scope=[2])
        resp = _call(VC.NetworkVkSettingsAPIView, 'get', '/api/v1/settings/vk/')
        self.assertEqual([r['id'] for r in resp.data['branches']], [2])
        self.assertFalse(resp.data['branches'][0]['connected'])


# ── 4. PATCH: подтверждение риска и белый список ─────────────────────────────

class PatchGuardsTest(SimpleTestCase):

    def setUp(self):
        self.branch = _branch()
        self.cfg = _Cfg(branch=self.branch, vk_group_id=111, vk_community_token=TOKEN,
                        vk_callback_secret=SECRET)
        self.env = _Env(self, branches=[self.branch], configs=[self.cfg])

    def _patch(self, data, pk=1):
        return _call(VC.BranchVkAPIView, 'patch', '/api/v1/mobile/branches/1/vk/',
                     data=data, pk=pk)

    def test_token_change_requires_confirm(self):
        resp = self._patch({'vk_community_token': OTHER_TOKEN})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'confirm_required')
        self.assertEqual(resp.data['fields'], ['vk_community_token'])
        self.assertEqual(self.cfg.saved, [])
        self.env.perm_mock.assert_not_called()

    def test_group_and_secret_change_require_confirm(self):
        for payload, field in (({'vk_group_id': 222}, 'vk_group_id'),
                               ({'vk_callback_secret': 'other'}, 'vk_callback_secret')):
            with self.subTest(field=field):
                resp = self._patch(payload)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.data['code'], 'confirm_required')
                self.assertEqual(resp.data['fields'], [field])

    def test_safe_fields_need_no_confirm(self):
        resp = self._patch({'notes': 'смена вывески', 'is_active': False})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['changed'], ['is_active', 'notes'])
        self.assertEqual(self.cfg.saved, [['is_active', 'notes']])

    def test_same_value_is_not_a_change(self):
        """Повтор тем же токеном не требует confirm и не дёргает ВК."""
        resp = self._patch({'vk_community_token': TOKEN})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['changed'], [])
        self.env.perm_mock.assert_not_called()
        self.assertEqual(self.cfg.saved, [])

    def test_unknown_field_is_rejected_with_editable(self):
        resp = self._patch({'vk_secret': 'x'})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')
        self.assertIn('vk_secret', resp.data['unknown'])
        self.assertIn('vk_callback_secret', resp.data['editable'])

    def test_negative_group_id_is_rejected(self):
        resp = self._patch({'vk_group_id': -111, 'confirm': True})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')

    def test_create_requires_group_and_token(self):
        env = _Env(self, branches=[self.branch], configs=[])
        resp = self._patch({'notes': 'пока без токена'})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')
        self.assertEqual(env.config_manager.created, [])

    def test_create_makes_config_after_vk_check(self):
        env = _Env(self, branches=[self.branch], configs=[])
        resp = self._patch({'vk_group_id': 111, 'vk_community_token': OTHER_TOKEN})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(len(env.config_manager.created), 1)
        self.assertTrue(resp.data['connected'])
        self.assertEqual(sorted(resp.data['changed']), ['vk_community_token', 'vk_group_id'])
        env.perm_mock.assert_called_once()


class GroupMismatchTest(SimpleTestCase):
    """Токен другой группы записывать нельзя: рассылки ушли бы не туда."""

    def test_409_group_mismatch(self):
        branch = _branch()
        cfg = _Cfg(branch=branch, vk_group_id=111, vk_community_token=TOKEN)
        env = _Env(self, branches=[branch], configs=[cfg],
                   group={'id': 999, 'name': 'Чужое', 'screen_name': 'alien', 'photo': ''})
        resp = _call(VC.BranchVkAPIView, 'patch', '/api/v1/mobile/branches/1/vk/',
                     data={'vk_community_token': OTHER_TOKEN, 'confirm': True}, pk=1)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'group_mismatch')
        self.assertEqual(resp.data['token_group_id'], 999)
        self.assertEqual(cfg.saved, [])
        self.assertEqual(cfg.vk_community_token, TOKEN)
        self.assertNotIn(OTHER_TOKEN, _body(resp))
        env.servers_mock.assert_not_called()


class GroupInUseTest(SimpleTestCase):
    """Смена сообщества у точки с историей запрещена."""

    def _resp(self, has_history):
        branch = _branch()
        cfg = _Cfg(branch=branch, vk_group_id=111, vk_community_token=TOKEN)
        _Env(self, branches=[branch], configs=[cfg], has_history=has_history,
             group={'id': 222, 'name': 'Новое', 'screen_name': 'new', 'photo': ''})
        return cfg, _call(VC.BranchVkAPIView, 'patch', '/api/v1/mobile/branches/1/vk/',
                          data={'vk_group_id': 222, 'confirm': True}, pk=1)

    def test_409_when_history_exists(self):
        cfg, resp = self._resp(True)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'group_in_use')
        self.assertEqual(cfg.saved, [])
        self.assertEqual(cfg.vk_group_id, 111)

    def test_ok_when_no_history(self):
        cfg, resp = self._resp(False)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(cfg.vk_group_id, 222)


# ── 5. Секрет на всю группу и согласованность ────────────────────────────────

class SecretAppliedToWholeGroupTest(SimpleTestCase):
    """
    Инцидент: у конфигов одной группы разошлись секреты → приёмник отвечал 403
    → ВК отключил callback. PATCH обязан выровнять ВСЮ группу.
    """

    def setUp(self):
        self.b1 = _branch(pk=1, branch_id=101, name='Центральная')
        self.b2 = _branch(pk=2, branch_id=202, name='Институтская')
        self.b3 = _branch(pk=3, branch_id=303, name='Другая группа')
        self.c1 = _Cfg(branch=self.b1, vk_group_id=111, vk_community_token=TOKEN,
                       vk_callback_secret='old-1')
        self.c2 = _Cfg(branch=self.b2, vk_group_id=111, vk_community_token=TOKEN,
                       vk_callback_secret='old-2')
        self.c3 = _Cfg(branch=self.b3, vk_group_id=777, vk_community_token=OTHER_TOKEN,
                       vk_callback_secret='alien')
        _Env(self, branches=[self.b1, self.b2, self.b3],
             configs=[self.c1, self.c2, self.c3])

    def test_secret_and_confirmation_go_to_every_config_of_group(self):
        resp = _call(VC.BranchVkAPIView, 'patch', '/api/v1/mobile/branches/1/vk/',
                     data={'vk_callback_secret': 'one-secret',
                           'vk_callback_confirmation': CONFIRMATION, 'confirm': True}, pk=1)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['applied_to'], [101, 202])
        self.assertEqual(self.c2.vk_callback_secret, 'one-secret')
        self.assertEqual(self.c2.vk_callback_confirmation, CONFIRMATION)
        self.assertEqual(self.c2.saved,
                         [['vk_callback_confirmation', 'vk_callback_secret']])
        # Чужая группа не тронута.
        self.assertEqual(self.c3.vk_callback_secret, 'alien')
        self.assertEqual(self.c3.saved, [])

    def test_card_shows_group_neighbours(self):
        resp = _call(VC.BranchVkAPIView, 'get', '/api/v1/mobile/branches/1/vk/', pk=1)
        self.assertEqual(resp.data['group_shared_with'], [202])

    def test_summary_flags_inconsistent_secrets(self):
        resp = _call(VC.NetworkVkSettingsAPIView, 'get', '/api/v1/settings/vk/')
        groups = {g['vk_group_id']: g for g in resp.data['groups']}
        self.assertFalse(groups[111]['secrets_consistent'])
        self.assertEqual(groups[111]['branches'], [1, 2])
        self.assertTrue(groups[777]['secrets_consistent'])

    def test_summary_consistent_after_alignment(self):
        self.c2.vk_callback_secret = 'old-1'
        resp = _call(VC.NetworkVkSettingsAPIView, 'get', '/api/v1/settings/vk/')
        groups = {g['vk_group_id']: g for g in resp.data['groups']}
        self.assertTrue(groups[111]['secrets_consistent'])

    def test_empty_secret_is_not_consistent(self):
        self.c1.vk_callback_secret = ''
        self.c2.vk_callback_secret = ''
        resp = _call(VC.NetworkVkSettingsAPIView, 'get', '/api/v1/settings/vk/')
        groups = {g['vk_group_id']: g for g in resp.data['groups']}
        self.assertFalse(groups[111]['secrets_consistent'])


# ── 6. Проверка подключения ──────────────────────────────────────────────────

class CheckTest(SimpleTestCase):

    def setUp(self):
        self.branch = _branch()
        self.cfg = _Cfg(branch=self.branch, vk_group_id=111, vk_community_token=TOKEN)

    def _check(self, data=None, **env_kw):
        self.env = _Env(self, branches=[self.branch], configs=[self.cfg], **env_kw)
        return _call(VC.BranchVkCheckAPIView, 'post',
                     '/api/v1/mobile/branches/1/vk/check/', data=data or {}, pk=1)

    def test_ok_shape(self):
        resp = self._check(servers=[
            {'id': 1, 'title': 'LoyalUP', 'url': 'https://levone.levelupapp.ru/api/v1/vk/callback/',
             'status': 'ok'},
            {'id': 2, 'title': 'Senler', 'url': 'https://senler.ru/hook', 'status': 'failed'},
        ])
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data['ok'])
        self.assertTrue(resp.data['can_save'])
        self.assertTrue(resp.data['group_matches'])
        self.assertEqual(resp.data['missing_permissions'], [])
        self.assertEqual(resp.data['group']['screen_name'], 'cafe')
        self.assertEqual([s['ours'] for s in resp.data['callback_servers']], [True, False])
        self.assertIsNone(resp.data['callback_servers_error'])

    def test_writes_nothing(self):
        self._check({'vk_community_token': OTHER_TOKEN, 'vk_group_id': 111})
        self.assertEqual(self.cfg.saved, [])
        self.assertEqual(self.env.config_manager.created, [])
        self.assertEqual(self.cfg.vk_community_token, TOKEN)

    def test_no_more_than_three_vk_calls(self):
        self._check({'vk_community_token': OTHER_TOKEN})
        total = (self.env.perm_mock.call_count + self.env.group_mock.call_count
                 + self.env.servers_mock.call_count)
        self.assertLessEqual(total, 3)

    def test_missing_permissions_block_saving(self):
        resp = self._check(data={}, permissions=('messages',))
        self.assertEqual(resp.data['missing_permissions'], ['manage'])
        self.assertFalse(resp.data['can_save'])
        self.assertTrue(resp.data['ok'])

    def test_group_mismatch_blocks_saving(self):
        resp = self._check(data={'vk_group_id': 999})
        self.assertFalse(resp.data['group_matches'])
        self.assertFalse(resp.data['can_save'])

    def test_vk_error_is_424(self):
        resp = self._check(vk_exc=VkApiError(5, 'User authorization failed: invalid access_token.'))
        self.assertEqual(resp.status_code, 424)
        self.assertEqual(resp.data['code'], 'vk_error')
        self.assertEqual(resp.data['vk_error_code'], 5)
        self.assertIn('invalid access_token', resp.data['vk_error_msg'])

    def test_vk_timeout_is_504(self):
        resp = self._check(vk_exc=VkApiError(None, 'groups.getTokenPermissions: ВК не ответил'))
        self.assertEqual(resp.status_code, 504)
        self.assertEqual(resp.data['code'], 'vk_timeout')

    def test_unexpected_exception_is_not_500(self):
        """п.5 контракта: любая беда VK-функции — 424/504, никогда 500."""
        resp = self._check(vk_exc=RuntimeError('boom'))
        self.assertEqual(resp.status_code, 504)
        self.assertEqual(resp.data['code'], 'vk_timeout')

    def test_callback_servers_failure_does_not_break_check(self):
        self.env = _Env(self, branches=[self.branch], configs=[self.cfg])
        self.env.servers_mock.side_effect = RuntimeError('Access denied')
        resp = _call(VC.BranchVkCheckAPIView, 'post',
                     '/api/v1/mobile/branches/1/vk/check/', data={}, pk=1)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['callback_servers'], [])
        self.assertIn('Access denied', resp.data['callback_servers_error'])

    def test_no_token_anywhere_is_400(self):
        self.cfg.vk_community_token = ''
        resp = self._check()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')

    def test_unknown_field_is_400(self):
        resp = self._check({'token': 'x'})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')


class PatchVkFailureTest(SimpleTestCase):
    """Отказ ВК на записи: настройки остаются нетронутыми."""

    def _patch(self, vk_exc):
        branch = _branch()
        cfg = _Cfg(branch=branch, vk_group_id=111, vk_community_token=TOKEN)
        _Env(self, branches=[branch], configs=[cfg], vk_exc=vk_exc)
        return cfg, _call(VC.BranchVkAPIView, 'patch', '/api/v1/mobile/branches/1/vk/',
                          data={'vk_community_token': OTHER_TOKEN, 'confirm': True}, pk=1)

    def test_424(self):
        cfg, resp = self._patch(VkApiError(27, 'Community authorization failed.'))
        self.assertEqual(resp.status_code, 424)
        self.assertEqual(resp.data['vk_error_code'], 27)
        self.assertEqual(cfg.vk_community_token, TOKEN)
        self.assertEqual(cfg.saved, [])

    def test_504(self):
        cfg, resp = self._patch(VkApiError(None, 'timeout'))
        self.assertEqual(resp.status_code, 504)
        self.assertEqual(resp.data['code'], 'vk_timeout')
        self.assertEqual(cfg.saved, [])


# ── 7. Сервисы ВК (requests.post замокан) ────────────────────────────────────

class VkServiceTest(SimpleTestCase):
    """Реальных вызовов ВК нет: патчим `requests.post` внутри `_vk_call`."""

    @staticmethod
    def _resp(payload):
        resp = MagicMock()
        resp.json.return_value = payload
        resp.raise_for_status.return_value = None
        return resp

    def test_group_info_v5131_list(self):
        with patch('requests.post', return_value=self._resp({'response': [
                {'id': 111, 'name': 'Кафе', 'screen_name': 'cafe', 'photo_100': 'p.jpg'}]})):
            self.assertEqual(vk_group_info(TOKEN, 111),
                             {'id': 111, 'name': 'Кафе', 'screen_name': 'cafe', 'photo': 'p.jpg'})

    def test_group_info_new_api_shape(self):
        with patch('requests.post', return_value=self._resp({'response': {'groups': [
                {'id': 111, 'name': 'Кафе', 'screen_name': 'cafe', 'photo_100': 'p.jpg'}]}})):
            self.assertEqual(vk_group_info(TOKEN)['id'], 111)

    def test_group_info_without_group_id_asks_about_token_owner(self):
        with patch('requests.post', return_value=self._resp({'response': [{'id': 7}]})) as post:
            vk_group_info(TOKEN)
            self.assertNotIn('group_ids', post.call_args.kwargs['data'])

    def test_vk_error_becomes_exception_with_code(self):
        payload = {'error': {'error_code': 5, 'error_msg': 'invalid access_token'}}
        with patch('requests.post', return_value=self._resp(payload)):
            with self.assertRaises(VkApiError) as ctx:
                vk_token_permissions(TOKEN)
            self.assertEqual(ctx.exception.code, 5)

    def test_network_failure_becomes_exception_without_code(self):
        with patch('requests.post', side_effect=OSError('timed out')):
            with self.assertRaises(VkApiError) as ctx:
                vk_token_permissions(TOKEN)
            self.assertIsNone(ctx.exception.code)

    def test_permissions_are_names(self):
        payload = {'response': {'mask': 4096, 'permissions': [
            {'name': 'messages', 'setting': 4096}, {'name': 'manage', 'setting': 262144}]}}
        with patch('requests.post', return_value=self._resp(payload)):
            self.assertEqual(vk_token_permissions(TOKEN), ['manage', 'messages'])

    def test_confirmation_code(self):
        with patch('requests.post', return_value=self._resp({'response': {'code': 'ab12'}})):
            self.assertEqual(vk_callback_confirmation_code(TOKEN, 111), 'ab12')

    def test_timeout_is_capped_at_ten_seconds(self):
        with patch('requests.post', return_value=self._resp({'response': {}})) as post:
            vk_token_permissions(TOKEN)
            self.assertLessEqual(post.call_args.kwargs['timeout'], 10)
