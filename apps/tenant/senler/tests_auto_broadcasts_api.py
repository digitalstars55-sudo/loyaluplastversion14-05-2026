"""
Тесты конструктора авторассылок для внешнего кабинета CheckUp (№31).

Стратегия патчей — как в tests_broadcasts_api.py
────────────────────────────────────────────────
Модели senler/branch/analytics ТЕНАНТНЫЕ: в тестовой БД их таблиц нет, поэтому
в ORM не ходим вообще. Вьюхи импортируют модели и функции движка на уровне
модуля, так что подменяем их прямо в apps.tenant.senler.api.auto_broadcasts:
  AutoBroadcastRule / AutoBroadcastVariant / BroadcastSend / BroadcastRecipient
  / Branch / ClientBranch / RFSegment / CheckUpIdentity — менеджеры-моки;
  rule_stats / resolve_recipients / preview_rule / render_text / pick_variant /
  send_vk_message — функции движка и сервиса;
  effective_branch_ids — RBAC (None = доступны все точки);
  _atomic — пустой контекст вместо transaction.atomic (БД запрещена).

Что проверяем:
  1) КАЖДАЯ вьюха модуля закрыта IsAuthenticated;
  2) карточка содержит ВСЕ старые ключи мобилки (_serialize_rule) — список
     зафиксирован здесь, его нельзя урезать без новой версии контракта;
  3) RBAC: сетевое правило невидимо ограниченному, ограниченный обязан
     прислать branch_ids;
  4) создание — всегда выключенным; валидация (delay_required, reward_invalid,
     variant_weights_invalid, event_unknown);
  5) гейт ★10: PATCH is_active=true от пользователя CheckUp → 400 use_activate,
     от обычного (мобилка) — работает;
  6) activate/: expected_count, confirm, пустая и разъехавшаяся аудитория;
  7) DELETE = архив, а не delete();
  8) варианты A/B: вес < 1, удаление варианта с отправками;
  9) справочник событий, лог, статистика, троттл тест-отправки.
"""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant.senler.api import auto_broadcasts as ab
from apps.tenant.senler.api.auto_broadcasts_serializers import rule_to_dict

AB = 'apps.tenant.senler.api.auto_broadcasts.'

# Ключи старой карточки мобилки (mobile/api/views.py:_serialize_rule). Новая
# карточка — НАДМНОЖЕСТВО: ни один ключ отсюда не имеет права исчезнуть.
LEGACY_RULE_KEYS = [
    'id', 'name', 'event', 'event_label', 'is_active', 'delay_days',
    'send_hour_start', 'send_hour_end', 'message_text', 'priority',
    'branches_count', 'gender_filter', 'segments_count', 'sent_total',
    'sent', 'read', 'failed', 'open_rate', 'variants', 'parent_rule_name',
]

STATS = {'sent': 10, 'read': 5, 'failed': 1, 'open_rate': 50.0,
         'variants': [], 'sent_30d': 4, 'last_run_at': None}


# ── Помощники ─────────────────────────────────────────────────────────────────

def _user(username='t', is_superuser=True):
    return SimpleNamespace(
        is_authenticated=True, is_active=True, is_superuser=is_superuser,
        username=username, pk=1, is_staff=True,
    )


def _branch(pk=1, name='Точка 1'):
    return SimpleNamespace(pk=pk, name=name)


def _qs(rows):
    """Queryset-мок: любой filter/order_by/... возвращает себя же."""
    rows = list(rows)
    qs = MagicMock()
    for name in ('all', 'filter', 'exclude', 'select_related', 'prefetch_related', 'order_by'):
        getattr(qs, name).return_value = qs
    qs.count.return_value = len(rows)
    qs.exists.return_value = bool(rows)
    qs.first.return_value = rows[0] if rows else None
    qs.__iter__ = lambda self: iter(rows)
    qs.__getitem__ = lambda self, item: rows[item]
    qs.values_list.return_value = [getattr(r, 'pk', None) for r in rows]
    return qs


def _rule_mock(**over):
    rule = MagicMock()
    rule.pk = 1
    rule.name = 'Напоминание о подарке'
    rule.event = 'birthday'
    rule.get_event_display.return_value = 'День рождения'
    rule.is_active = False
    rule.is_archived = False
    rule.priority = 0
    rule.delay_days = None
    rule.send_hour_start = 9
    rule.send_hour_end = 21
    rule.active_from = None
    rule.active_to = None
    rule.gender_filter = 'all'
    rule.message_text = 'Привет, {имя}!'
    rule.gift_tier = ''
    rule.gift_lifetime_days = 0
    rule.gift_fallback_text = ''
    rule.parent_rule_id = None
    rule.follow_up_condition = 'not_read'
    rule.created_at = None
    rule.updated_at = None
    rule.branches.all.return_value = [_branch(1)]
    rule.rf_segments.all.return_value = []
    rule.variants.all.return_value = []
    rule.logs.count.return_value = 3
    for key, value in over.items():
        setattr(rule, key, value)
    return rule


def _variant_mock(pk=7, name='А', is_active=True, weight=1):
    variant = MagicMock()
    variant.pk = pk
    variant.name = name
    variant.message_text = 'Текст варианта'
    variant.weight = weight
    variant.is_active = is_active
    return variant


def _call(view, method, path, payload=None, *, user=None, **kwargs):
    factory = APIRequestFactory()
    if method == 'get':
        request = factory.get(path)
    else:
        request = getattr(factory, method)(path, payload or {}, format='json')
    force_authenticate(request, user=user or _user())
    return view.as_view()(request, **kwargs)


class _FakeCache:
    """Кэш в памяти теста — троттл тест-отправки проверяется без Redis."""

    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value, ttl=None):
        self.data[key] = value


class _AutoBroadcastCase(SimpleTestCase):
    """Общая обвязка: модели и движок подменены, RBAC = «все точки»."""

    def setUp(self):
        self.rule = _rule_mock()
        self.rules_qs = _qs([self.rule])

        self.patches = {
            'effective_branch_ids': patch(AB + 'effective_branch_ids', return_value=None),
            'AutoBroadcastRule':    patch(AB + 'AutoBroadcastRule'),
            'AutoBroadcastVariant': patch(AB + 'AutoBroadcastVariant'),
            'BroadcastSend':        patch(AB + 'BroadcastSend'),
            'BroadcastRecipient':   patch(AB + 'BroadcastRecipient'),
            'Branch':               patch(AB + 'Branch'),
            'ClientBranch':         patch(AB + 'ClientBranch'),
            'RFSegment':            patch(AB + 'RFSegment'),
            'CheckUpIdentity':      patch(AB + 'CheckUpIdentity'),
            'rule_stats':           patch(AB + 'rule_stats', return_value=dict(STATS)),
            'resolve_recipients':   patch(AB + 'resolve_recipients', return_value=[]),
            'rule_is_due':          patch(AB + 'rule_is_due', return_value=(False, 'inactive')),
            'render_text':          patch(AB + 'render_text', return_value='Привет, Лев!'),
            'pick_variant':         patch(AB + 'pick_variant', return_value=None),
            'send_vk_message':      patch(AB + 'send_vk_message', return_value=(True, '', 777)),
            '_atomic':              patch(AB + '_atomic', side_effect=nullcontext),
        }
        self.mocks = {}
        for name, patcher in self.patches.items():
            self.addCleanup(patcher.stop)
            self.mocks[name] = patcher.start()

        self.access = self.mocks['effective_branch_ids']
        self.model = self.mocks['AutoBroadcastRule']
        self.model.objects.all.return_value = self.rules_qs
        self.model.objects.create.return_value = self.rule
        # Сеть по умолчанию: пользователь не из обмена CheckUp.
        self.mocks['CheckUpIdentity'].objects.filter.return_value.exists.return_value = False
        # sent_30d / last_run_at считаются по этим менеджерам.
        self.mocks['BroadcastRecipient'].objects.filter.return_value = _qs([])
        self.mocks['BroadcastSend'].objects.filter.return_value = _qs([])
        self.mocks['Branch'].objects.filter.return_value = _qs([_branch(1), _branch(2)])
        self.mocks['RFSegment'].objects.filter.return_value = _qs([])


# ── 1. Авторизация ────────────────────────────────────────────────────────────

class RequiresAuthTest(SimpleTestCase):
    """В settings нет DEFAULT_PERMISSION_CLASSES (= AllowAny) — держим руками."""

    def test_every_view_declares_is_authenticated(self):
        import inspect

        from rest_framework.permissions import IsAuthenticated
        from rest_framework.views import APIView

        checked = 0
        for name, cls in inspect.getmembers(ab, inspect.isclass):
            if cls is APIView or not issubclass(cls, APIView) or cls.__module__ != ab.__name__:
                continue
            checked += 1
            with self.subTest(view=name):
                self.assertIn(IsAuthenticated, cls.permission_classes)
        self.assertGreaterEqual(checked, 11)

    def test_anonymous_is_rejected(self):
        factory = APIRequestFactory()
        for view, path in (
            (ab.AutoBroadcastRuleListCreateAPIView, '/api/v1/auto-broadcasts/'),
            (ab.AutoBroadcastEventsAPIView, '/api/v1/auto-broadcasts/events/'),
        ):
            resp = view.as_view()(factory.get(path))
            self.assertIn(resp.status_code, (401, 403))


# ── 2. Карточка: старые ключи мобилки ─────────────────────────────────────────

class LegacyCardKeysTest(SimpleTestCase):
    """
    Мобильное приложение уже установлено и читает эти ключи. Их значения и типы
    менять нельзя — иначе список авторассылок в мобилке сломается молча.
    """

    def test_all_legacy_keys_present(self):
        card = rule_to_dict(_rule_mock(), stats=dict(STATS), events={})
        for key in LEGACY_RULE_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, card)

    def test_legacy_values(self):
        card = rule_to_dict(_rule_mock(), stats=dict(STATS), events={})
        self.assertEqual(card['branches_count'], 1)     # 0 = все точки
        self.assertEqual(card['segments_count'], 0)
        self.assertEqual(card['sent_total'], 3)         # rule.logs.count()
        self.assertEqual(card['sent'], 10)
        self.assertEqual(card['read'], 5)
        self.assertEqual(card['failed'], 1)
        self.assertEqual(card['open_rate'], 50.0)
        self.assertEqual(card['gender_filter'], 'all')
        self.assertIsNone(card['parent_rule_name'])
        self.assertEqual(card['event_label'], 'День рождения')
        self.assertEqual(card['variants'], [])

    def test_new_nested_form(self):
        card = rule_to_dict(_rule_mock(), stats=dict(STATS), events={})
        self.assertEqual(card['audience']['branch_ids'], [1])
        self.assertEqual(card['audience']['gender_filter'], 'all')
        self.assertEqual(card['reward']['gift_tier'], '')
        self.assertIsNone(card['follow_up'])
        self.assertIsNone(card['image'])
        self.assertFalse(card['is_archived'])
        self.assertEqual(card['stats']['sent_30d'], 4)
        self.assertIn('точки: 1', card['audience_summary'])
        self.assertEqual(card['reward_summary'], 'без подарка')

    def test_card_of_network_rule_with_segments(self):
        rule = _rule_mock()
        rule.branches.all.return_value = []
        rule.rf_segments.all.return_value = [
            SimpleNamespace(pk=3, code='C1', name='Чемпионы', emoji='🏆'),
        ]
        card = rule_to_dict(rule, stats=dict(STATS), events={})
        self.assertEqual(card['audience']['branch_ids'], [])
        self.assertEqual(card['branches_count'], 0)
        self.assertEqual(card['segments_count'], 1)
        self.assertIn('сегменты: Чемпионы', card['audience_summary'])


# ── 3. Список и RBAC ──────────────────────────────────────────────────────────

class RuleListTest(_AutoBroadcastCase):

    def _list(self, path='/api/v1/auto-broadcasts/'):
        return _call(ab.AutoBroadcastRuleListCreateAPIView, 'get', path)

    def test_list_keeps_rules_key_and_adds_page(self):
        resp = self._list()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['rules']), 1)
        self.assertEqual(resp.data['total'], 1)
        # без limit — полный список, limit = total (совместимость с мобилкой)
        self.assertEqual(resp.data['limit'], 1)
        self.assertEqual(resp.data['offset'], 0)

    def test_limit_is_respected(self):
        resp = self._list('/api/v1/auto-broadcasts/?limit=1&offset=0')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['limit'], 1)

    def test_restricted_user_does_not_see_network_rule(self):
        self.rule.branches.all.return_value = []      # сетевое правило
        self.access.return_value = [1]
        resp = self._list()
        self.assertEqual(resp.data['rules'], [])
        self.assertEqual(resp.data['total'], 0)

    def test_restricted_user_sees_own_rule(self):
        self.access.return_value = [1, 2]
        resp = self._list()
        self.assertEqual(len(resp.data['rules']), 1)


class RuleDetailRbacTest(_AutoBroadcastCase):

    def test_network_rule_is_404_for_restricted(self):
        self.rule.branches.all.return_value = []
        self.access.return_value = [1]
        resp = _call(ab.AutoBroadcastRuleDetailAPIView, 'get', '/api/v1/auto-broadcasts/1/', pk=1)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data['code'], 'not_found')
        self.assertEqual(resp.data['detail'], 'Правило не найдено.')

    def test_foreign_branch_rule_is_404(self):
        self.access.return_value = [9]
        resp = _call(ab.AutoBroadcastRuleDetailAPIView, 'get', '/api/v1/auto-broadcasts/1/', pk=1)
        self.assertEqual(resp.status_code, 404)

    def test_missing_rule_is_404(self):
        self.model.objects.all.return_value = _qs([])
        resp = _call(ab.AutoBroadcastRuleDetailAPIView, 'get', '/api/v1/auto-broadcasts/1/', pk=1)
        self.assertEqual(resp.status_code, 404)


# ── 4. Создание ───────────────────────────────────────────────────────────────

class CreateRuleTest(_AutoBroadcastCase):

    def _create(self, payload, user=None):
        return _call(ab.AutoBroadcastRuleListCreateAPIView, 'post',
                     '/api/v1/auto-broadcasts/', payload, user=user)

    def _valid(self, **over):
        payload = {
            'name': 'ДР за 7 дней',
            'event': 'birthday_7d',
            'message_text': 'Привет, {имя}!',
            'delay_days': 7,
            'audience': {'branch_ids': [1]},
        }
        payload.update(over)
        return payload

    def test_created_rule_is_always_inactive(self):
        resp = self._create(self._valid(is_active=True))
        self.assertEqual(resp.status_code, 201)
        kwargs = self.model.objects.create.call_args.kwargs
        self.assertIs(kwargs['is_active'], False)
        self.assertIs(kwargs['is_archived'], False)

    def test_unknown_event(self):
        resp = self._create(self._valid(event='nope'))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'event_unknown')
        self.model.objects.create.assert_not_called()

    def test_delay_required_event_without_delay(self):
        payload = self._valid(event='no_visit_days')
        payload.pop('delay_days')
        resp = self._create(payload)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'delay_required')
        self.model.objects.create.assert_not_called()

    def test_empty_text(self):
        resp = self._create(self._valid(message_text='   '))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')

    def test_text_too_long(self):
        resp = self._create(self._valid(message_text='x' * 4097))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')

    def test_bad_hours(self):
        resp = self._create(self._valid(send_hour_start=21, send_hour_end=9))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')

    def test_bad_period(self):
        resp = self._create(self._valid(active_from='2026-10-01', active_to='2026-09-01'))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')

    def test_bad_date_format(self):
        resp = self._create(self._valid(active_from='01.10.2026'))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')

    def test_reward_invalid_tier(self):
        resp = self._create(self._valid(reward={'gift_tier': 'G5'}))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'reward_invalid')

    def test_variant_weight_zero(self):
        resp = self._create(self._valid(variants=[
            {'name': 'А', 'message_text': 'раз', 'weight': 0},
        ]))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'variant_weights_invalid')
        self.model.objects.create.assert_not_called()

    def test_restricted_user_must_send_branch_ids(self):
        self.access.return_value = [1]
        payload = self._valid()
        payload.pop('audience')
        resp = self._create(payload)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')
        self.assertIn('branch_ids', resp.data['detail'])
        self.model.objects.create.assert_not_called()

    def test_restricted_user_foreign_branch_is_404(self):
        self.access.return_value = [2]
        resp = self._create(self._valid(audience={'branch_ids': [1]}))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data['code'], 'not_found')

    def test_follow_up_without_parent(self):
        resp = self._create(self._valid(event='follow_up', delay_days=3))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')

    def test_variants_are_created(self):
        resp = self._create(self._valid(variants=[
            {'name': 'А', 'message_text': 'раз', 'weight': 1},
            {'name': 'Б', 'message_text': 'два', 'weight': 3, 'is_active': False},
        ]))
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(self.mocks['AutoBroadcastVariant'].objects.create.call_count, 2)


# ── 5. PATCH и гейт включения ─────────────────────────────────────────────────

class PatchRuleTest(_AutoBroadcastCase):

    def _patch(self, payload, user=None):
        return _call(ab.AutoBroadcastRuleDetailAPIView, 'patch',
                     '/api/v1/auto-broadcasts/1/', payload, user=user, pk=1)

    def test_mobile_patch_text_still_works(self):
        resp = self._patch({'message_text': 'новый текст'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.rule.message_text, 'новый текст')
        self.rule.save.assert_called_once()

    def test_mobile_patch_is_active_true_works_for_plain_user(self):
        resp = self._patch({'is_active': True})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(self.rule.is_active, True)

    def test_checkup_user_cannot_switch_is_active(self):
        self.mocks['CheckUpIdentity'].objects.filter.return_value.exists.return_value = True
        resp = self._patch({'is_active': True}, user=_user('checkup-42-levone'))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'use_activate')
        self.rule.save.assert_not_called()

    def test_checkup_user_may_switch_off(self):
        self.mocks['CheckUpIdentity'].objects.filter.return_value.exists.return_value = True
        self.rule.is_active = True
        resp = self._patch({'is_active': False}, user=_user('checkup-42-levone'))
        self.assertEqual(resp.status_code, 200)
        self.assertIs(self.rule.is_active, False)

    def test_archived_rule_is_conflict(self):
        self.rule.is_archived = True
        resp = self._patch({'message_text': 'текст'})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'archived')

    def test_empty_text_is_400(self):
        resp = self._patch({'message_text': '  '})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')
        self.rule.save.assert_not_called()

    def test_audience_is_replaced(self):
        resp = self._patch({'audience': {'branch_ids': [1, 2], 'gender_filter': 'f'}})
        self.assertEqual(resp.status_code, 200)
        self.rule.branches.set.assert_called_once_with([1, 2])
        self.assertEqual(self.rule.gender_filter, 'f')

    def test_restricted_user_cannot_move_rule_to_foreign_branch(self):
        self.access.return_value = [1]
        resp = self._patch({'audience': {'branch_ids': [1, 9]}})
        self.assertEqual(resp.status_code, 404)
        self.rule.branches.set.assert_not_called()

    def test_missing_rule_keeps_mobile_404_text(self):
        self.model.objects.all.return_value = _qs([])
        resp = self._patch({'message_text': 'текст'})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.data['detail'], 'Правило не найдено.')


# ── 6. Архив ──────────────────────────────────────────────────────────────────

class DeleteRuleTest(_AutoBroadcastCase):

    def test_delete_archives_rule(self):
        resp = _call(ab.AutoBroadcastRuleDetailAPIView, 'delete',
                     '/api/v1/auto-broadcasts/1/', pk=1)
        self.assertEqual(resp.status_code, 204)
        self.assertIs(self.rule.is_archived, True)
        self.assertIs(self.rule.is_active, False)
        self.rule.delete.assert_not_called()
        self.rule.save.assert_called_once()

    def test_delete_missing_is_404(self):
        self.model.objects.all.return_value = _qs([])
        resp = _call(ab.AutoBroadcastRuleDetailAPIView, 'delete',
                     '/api/v1/auto-broadcasts/1/', pk=1)
        self.assertEqual(resp.status_code, 404)


# ── 7. Включение ──────────────────────────────────────────────────────────────

class ActivateTest(_AutoBroadcastCase):

    def _activate(self, payload):
        return _call(ab.AutoBroadcastRuleActivateAPIView, 'post',
                     '/api/v1/auto-broadcasts/1/activate/', payload, pk=1)

    def _candidates(self, count):
        return [SimpleNamespace(client_branch=SimpleNamespace(branch=_branch(1)), vk_id=i)
                for i in range(count)]

    def test_expected_count_required(self):
        resp = self._activate({'confirm': True})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'expected_count_required')
        self.mocks['resolve_recipients'].assert_not_called()

    def test_confirm_required(self):
        resp = self._activate({'expected_count': 100})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'confirm_required')
        self.mocks['resolve_recipients'].assert_not_called()

    def test_audience_empty(self):
        self.mocks['resolve_recipients'].return_value = []
        resp = self._activate({'expected_count': 0, 'confirm': True})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'audience_empty')
        self.assertIs(self.rule.is_active, False)

    def test_audience_changed(self):
        self.mocks['resolve_recipients'].return_value = self._candidates(429)
        resp = self._activate({'expected_count': 100, 'confirm': True})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'audience_changed')
        self.assertEqual(resp.data['expected'], 100)
        self.assertEqual(resp.data['actual'], 429)
        self.assertIs(self.rule.is_active, False)

    def test_success(self):
        self.mocks['resolve_recipients'].return_value = self._candidates(100)
        resp = self._activate({'expected_count': 100, 'confirm': True})
        self.assertEqual(resp.status_code, 200)
        self.assertIs(self.rule.is_active, True)
        self.rule.save.assert_called_once()

    def test_already_active(self):
        self.rule.is_active = True
        resp = self._activate({'expected_count': 100, 'confirm': True})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'already_active')

    def test_archived(self):
        self.rule.is_archived = True
        resp = self._activate({'expected_count': 100, 'confirm': True})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'archived')

    def test_preview_failed(self):
        self.mocks['resolve_recipients'].side_effect = RuntimeError('сегменты не посчитаны')
        resp = self._activate({'expected_count': 100, 'confirm': True})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'preview_failed')
        self.assertIs(self.rule.is_active, False)

    def test_deactivate(self):
        self.rule.is_active = True
        resp = _call(ab.AutoBroadcastRuleDeactivateAPIView, 'post',
                     '/api/v1/auto-broadcasts/1/deactivate/', {}, pk=1)
        self.assertEqual(resp.status_code, 200)
        self.assertIs(self.rule.is_active, False)


# ── 8. Предпросмотр ───────────────────────────────────────────────────────────

class PreviewTest(_AutoBroadcastCase):

    def _preview(self):
        return _call(ab.AutoBroadcastRulePreviewAPIView, 'get',
                     '/api/v1/auto-broadcasts/1/preview/', pk=1)

    def test_old_fields_plus_new(self):
        self.mocks['resolve_recipients'].return_value = [
            SimpleNamespace(client_branch=SimpleNamespace(branch=_branch(1)), vk_id=1),
            SimpleNamespace(client_branch=SimpleNamespace(branch=_branch(2, 'Точка 2')), vk_id=2),
            SimpleNamespace(client_branch=SimpleNamespace(branch=_branch(1)), vk_id=3),
        ]
        resp = self._preview()
        self.assertEqual(resp.status_code, 200)
        for key in ('recipients', 'due_now', 'reason', 'sample_text', 'sample_names', 'explanation'):
            self.assertIn(key, resp.data)
        self.assertIn('Кому:', resp.data['explanation'])
        self.assertEqual(resp.data['count'], 3)
        self.assertEqual(resp.data['by_branch'],
                         [{'branch_id': 1, 'name': 'Точка 1', 'count': 2},
                          {'branch_id': 2, 'name': 'Точка 2', 'count': 1}])
        self.assertEqual(resp.data['sample_texts'][0]['variant_id'], None)
        self.assertEqual(resp.data['sample_texts'][0]['text'], 'Привет, Лев!')

    def test_sample_texts_per_variant(self):
        self.rule.variants.all.return_value = [_variant_mock(7, 'А'), _variant_mock(8, 'Б')]
        self.mocks['resolve_recipients'].return_value = [
            SimpleNamespace(client_branch=SimpleNamespace(branch=_branch(1)), vk_id=1),
        ]
        resp = self._preview()
        self.assertEqual([row['variant_id'] for row in resp.data['sample_texts']], [7, 8])

    def test_preview_failed(self):
        self.mocks['resolve_recipients'].side_effect = ValueError('нет данных')
        resp = self._preview()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'preview_failed')


# ── 9. Варианты A/B ───────────────────────────────────────────────────────────

class VariantsTest(_AutoBroadcastCase):

    def test_create_variant(self):
        variant = _variant_mock()
        self.mocks['AutoBroadcastVariant'].objects.create.return_value = variant
        resp = _call(ab.AutoBroadcastVariantCreateAPIView, 'post',
                     '/api/v1/auto-broadcasts/1/variants/',
                     {'name': 'А', 'message_text': 'раз', 'weight': 2}, pk=1)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['id'], 7)

    def test_weight_zero_is_invalid(self):
        resp = _call(ab.AutoBroadcastVariantCreateAPIView, 'post',
                     '/api/v1/auto-broadcasts/1/variants/',
                     {'name': 'А', 'message_text': 'раз', 'weight': 0}, pk=1)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'variant_weights_invalid')
        self.mocks['AutoBroadcastVariant'].objects.create.assert_not_called()

    def test_empty_name_is_invalid(self):
        resp = _call(ab.AutoBroadcastVariantCreateAPIView, 'post',
                     '/api/v1/auto-broadcasts/1/variants/',
                     {'name': '  ', 'message_text': 'раз'}, pk=1)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'variant_weights_invalid')

    def test_patch_variant(self):
        variant = _variant_mock()
        self.mocks['AutoBroadcastVariant'].objects.filter.return_value = _qs([variant])
        resp = _call(ab.AutoBroadcastVariantDetailAPIView, 'patch',
                     '/api/v1/auto-broadcasts/1/variants/7/', {'is_active': False}, pk=1, vid=7)
        self.assertEqual(resp.status_code, 200)
        self.assertIs(variant.is_active, False)

    def test_delete_variant_with_sends_is_conflict(self):
        variant = _variant_mock()
        self.mocks['AutoBroadcastVariant'].objects.filter.return_value = _qs([variant])
        self.mocks['BroadcastSend'].objects.filter.return_value = _qs([MagicMock()])
        resp = _call(ab.AutoBroadcastVariantDetailAPIView, 'delete',
                     '/api/v1/auto-broadcasts/1/variants/7/', pk=1, vid=7)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'has_sends')
        variant.delete.assert_not_called()

    def test_delete_variant_without_sends(self):
        variant = _variant_mock()
        self.mocks['AutoBroadcastVariant'].objects.filter.return_value = _qs([variant])
        self.mocks['BroadcastSend'].objects.filter.return_value = _qs([])
        resp = _call(ab.AutoBroadcastVariantDetailAPIView, 'delete',
                     '/api/v1/auto-broadcasts/1/variants/7/', pk=1, vid=7)
        self.assertEqual(resp.status_code, 204)
        variant.delete.assert_called_once()

    def test_missing_variant_is_404(self):
        self.mocks['AutoBroadcastVariant'].objects.filter.return_value = _qs([])
        resp = _call(ab.AutoBroadcastVariantDetailAPIView, 'delete',
                     '/api/v1/auto-broadcasts/1/variants/7/', pk=1, vid=7)
        self.assertEqual(resp.status_code, 404)


# ── 10. Справочник событий ────────────────────────────────────────────────────

class EventsTest(_AutoBroadcastCase):

    def test_catalog_form(self):
        resp = _call(ab.AutoBroadcastEventsAPIView, 'get', '/api/v1/auto-broadcasts/events/')
        self.assertEqual(resp.status_code, 200)
        events = {e['code']: e for e in resp.data['events']}
        self.assertIn('birthday', events)
        for event in resp.data['events']:
            with self.subTest(event=event['code']):
                for key in ('code', 'label', 'description', 'dedup', 'delay_unit',
                            'default_delay_days', 'delay_required', 'placeholders'):
                    self.assertIn(key, event)
                self.assertEqual(event['delay_unit'], 'days')
                self.assertTrue(event['placeholders'])
                self.assertEqual(event['delay_required'], event['code'] in ('no_visit_days', 'subscribed_days'))

    def test_delay_required_flags(self):
        resp = _call(ab.AutoBroadcastEventsAPIView, 'get', '/api/v1/auto-broadcasts/events/')
        events = {e['code']: e for e in resp.data['events']}
        self.assertTrue(events['no_visit_days']['delay_required'])
        self.assertTrue(events['subscribed_days']['delay_required'])
        self.assertFalse(events['gift_not_claimed']['delay_required'])
        self.assertFalse(events['birthday']['delay_required'])
        self.assertFalse(events['after_game_3h']['delay_required'])
        self.assertEqual(events['gift_not_claimed']['default_delay_days'], 10)

    def test_dictionaries(self):
        resp = _call(ab.AutoBroadcastEventsAPIView, 'get', '/api/v1/auto-broadcasts/events/')
        self.assertEqual([g['code'] for g in resp.data['gender_filters']], ['all', 'm', 'f'])
        self.assertEqual([c['code'] for c in resp.data['follow_up_conditions']],
                         ['not_read', 'not_visited'])
        self.assertEqual([t['code'] for t in resp.data['gift_tiers']], ['', 'G1', 'G1,G2'])


# ── 11. Лог и статистика ──────────────────────────────────────────────────────

class LogAndStatsTest(_AutoBroadcastCase):

    def _recipient(self):
        variant = SimpleNamespace(pk=7, name='А')
        return SimpleNamespace(
            send_id=5, send=SimpleNamespace(auto_broadcast_variant=variant),
            client_branch_id=3,
            client_branch=SimpleNamespace(client=SimpleNamespace(first_name='Лев')),
            vk_id=123456789012, status='sent', sent_at=None, read_at=None, error='',
        )

    def test_log_form(self):
        self.mocks['BroadcastRecipient'].objects.filter.return_value = _qs([self._recipient()])
        resp = _call(ab.AutoBroadcastRuleLogAPIView, 'get',
                     '/api/v1/auto-broadcasts/1/log/?limit=10', pk=1)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['total'], 1)
        self.assertEqual(resp.data['limit'], 10)
        row = resp.data['results'][0]
        self.assertEqual(row['vk_id'], '123456789012')     # строкой
        self.assertEqual(row['name'], 'Лев')
        self.assertEqual(row['variant'], {'id': 7, 'name': 'А'})
        self.assertEqual(row['status'], 'sent')
        self.assertIsNone(row['sent_at'])
        self.assertEqual(row['error'], '')

    def test_stats_form(self):
        resp = _call(ab.AutoBroadcastRuleStatsAPIView, 'get',
                     '/api/v1/auto-broadcasts/1/stats/', pk=1)
        self.assertEqual(resp.status_code, 200)
        for key in ('sent', 'read', 'failed', 'open_rate', 'sent_30d', 'last_run_at', 'variants'):
            self.assertIn(key, resp.data)
        self.assertEqual(resp.data['sent'], 10)
        self.assertEqual(resp.data['variants'], [])

    def test_log_of_foreign_rule_is_404(self):
        self.access.return_value = [9]
        resp = _call(ab.AutoBroadcastRuleLogAPIView, 'get',
                     '/api/v1/auto-broadcasts/1/log/', pk=1)
        self.assertEqual(resp.status_code, 404)


# ── 12. Тест-отправка ─────────────────────────────────────────────────────────

class TestSendTest(_AutoBroadcastCase):

    def setUp(self):
        super().setUp()
        self.cache = _FakeCache()
        patcher = patch(AB + 'cache', self.cache)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.client_branch = SimpleNamespace(
            pk=11, branch=SimpleNamespace(pk=1, name='Точка 1',
                                          senler_config=SimpleNamespace(is_active=True)),
            client=SimpleNamespace(first_name='Лев', vk_id=42),
        )
        self.mocks['ClientBranch'].objects.filter.return_value = _qs([self.client_branch])

    def _send(self, payload=None):
        return _call(ab.AutoBroadcastRuleTestSendAPIView, 'post',
                     '/api/v1/auto-broadcasts/1/test-send/', payload or {'vk_id': 42}, pk=1)

    def test_sends_once_and_writes_nothing(self):
        resp = self._send()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {'ok': True, 'message_id': 777})
        self.mocks['send_vk_message'].assert_called_once()
        # ни лога дедупа, ни запусков, ни получателей
        self.mocks['BroadcastSend'].objects.create.assert_not_called()
        self.mocks['BroadcastRecipient'].objects.create.assert_not_called()

    def test_second_call_is_throttled(self):
        self.assertEqual(self._send().status_code, 200)
        resp = self._send()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'rate_limited')
        self.assertEqual(self.mocks['send_vk_message'].call_count, 1)

    def test_guest_not_found(self):
        self.mocks['ClientBranch'].objects.filter.return_value = _qs([])
        resp = self._send()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'guest_not_found')
        self.mocks['send_vk_message'].assert_not_called()

    def test_no_vk_token(self):
        self.client_branch.branch.senler_config = None
        resp = self._send()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'no_vk_token')

    def test_inactive_community_is_no_vk_token(self):
        self.client_branch.branch.senler_config = SimpleNamespace(is_active=False)
        resp = self._send()
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data['code'], 'no_vk_token')

    def test_bad_vk_id(self):
        resp = self._send({'vk_id': 'abc'})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')

    def test_vk_refusal(self):
        self.mocks['send_vk_message'].return_value = (False, 'VK error 901: message denied', None)
        resp = self._send()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'not_subscribed')


# ── волна 3: предпросмотр словами (контракт 3в.4) ────────────────────────────

class ExplainPreviewTest(SimpleTestCase):

    def _spec(self, **over):
        base = dict(label='Не приходил N дней (реактивация)', dedup='year', default_delay_days=None)
        base.update(over)
        return SimpleNamespace(**base)

    def test_full_sentence_set(self):
        from apps.tenant.senler.api.auto_broadcasts_serializers import explain_preview
        rule = _rule_mock()
        rule.event = 'no_visit_days'
        rule.delay_days = 30
        rule.active_from = None
        rule.active_to = None
        text = explain_preview(rule, self._spec(), False, 'inactive', 12, weekly_cap=3, orchestrator=True)
        for piece in ('Событие «Не приходил N дней (реактивация)», задержка 30 дн.', 'Кому: точки: 1',
                      'не чаще раза в год', 'не больше 3 авто-сообщений', 'RF-оркестратор',
                      'с 9:00 до 21:00 по МСК', 'Правило выключено', '12 получателям'):
            with self.subTest(piece=piece):
                self.assertIn(piece, text)

    def test_due_now_and_default_delay(self):
        from apps.tenant.senler.api.auto_broadcasts_serializers import explain_preview
        rule = _rule_mock()
        rule.delay_days = None
        text = explain_preview(rule, self._spec(label='Подарок не забран', dedup='entity', default_delay_days=10),
                               True, '', 0)
        self.assertIn('(задержка по умолчанию 10 дн.)', text)
        self.assertIn('по каждому подарку один раз', text)
        self.assertIn('Сейчас правило отправило бы 0 получателям', text)
        self.assertNotIn('Лимит сети', text)
