"""
Флаги механик сети (контракт 3б.7, №56) — только чтение.

Проверяем ровно две вещи, из-за которых ручка и заведена: белый список (касса,
интеграции и секреты не должны попасть в ответ НИКОГДА) и различение
«настроено сетью» против «дефолт из кода».
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.shared.config.models import ClientConfig
from apps.tenant.branch.api import feature_flags as F

FP = 'apps.tenant.branch.api.feature_flags.'
FORBIDDEN = ('pos_type', 'dooglys_api_key', 'iiko_login', 'vk_community_token',
             'vk_wall_token', 'senler_api_key')


def _user(role='client'):
    return SimpleNamespace(is_authenticated=True, is_active=True, is_superuser=False,
                           is_client=(role == 'client'), is_network_admin=False,
                           username='u', pk=1, is_staff=True)


class WhitelistTest(SimpleTestCase):

    def test_every_flag_exists_on_the_model(self):
        names = {f.name for f in ClientConfig._meta.get_fields()}
        for flag in F.FLAG_FIELDS:
            with self.subTest(flag=flag):
                self.assertIn(flag, names)

    def test_secrets_are_not_in_the_whitelist(self):
        for name in F.FLAG_FIELDS:
            self.assertNotIn('token', name)
            self.assertNotIn('secret', name)
            self.assertNotIn('key', name)

    def test_integration_fields_are_never_returned(self):
        cfg = SimpleNamespace(**{name: None for name in F.FLAG_FIELDS})
        for forbidden in FORBIDDEN:
            setattr(cfg, forbidden, 'секрет')
        payload = F.flags_payload(cfg)
        body = str(payload)
        for forbidden in FORBIDDEN:
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, payload)
                self.assertNotIn('секрет', body)


class SourceTest(SimpleTestCase):

    def test_default_when_value_equals_field_default(self):
        cfg = SimpleNamespace(**{name: F._field_default(name) for name in F.FLAG_FIELDS})
        payload = F.flags_payload(cfg)
        for name in F.FLAG_FIELDS:
            with self.subTest(flag=name):
                self.assertEqual(payload[name]['source'], 'default')

    def test_network_when_value_differs(self):
        cfg = SimpleNamespace(**{name: F._field_default(name) for name in F.FLAG_FIELDS})
        cfg.story_game_enabled = not F._field_default('story_game_enabled')
        cfg.guest_phone_reward_coins = 999
        payload = F.flags_payload(cfg)
        self.assertEqual(payload['story_game_enabled']['source'], 'network')
        self.assertEqual(payload['guest_phone_reward_coins'], {'value': 999, 'source': 'network'})

    def test_without_config_everything_is_default(self):
        payload = F.flags_payload(None)
        self.assertEqual({v['source'] for v in payload.values()}, {'default'})

    def test_dates_are_plain_strings(self):
        from datetime import date
        cfg = SimpleNamespace(**{name: F._field_default(name) for name in F.FLAG_FIELDS})
        cfg.story_campaign_start = date(2026, 9, 1)
        self.assertEqual(F.flags_payload(cfg)['story_campaign_start']['value'], '2026-09-01')


class EndpointTest(SimpleTestCase):

    def test_read_only_flag_and_auth(self):
        self.assertIn(IsAuthenticated, F.FeatureFlagsAPIView.permission_classes)
        objects = MagicMock()
        objects.filter.return_value.first.return_value = None
        with patch.object(F.ClientConfig, 'objects', objects), \
             patch(FP + 'connection', SimpleNamespace(tenant=SimpleNamespace(pk=1))):
            factory = APIRequestFactory()
            request = factory.get('/api/v1/settings/features/')
            force_authenticate(request, user=_user())
            resp = F.FeatureFlagsAPIView.as_view()(request)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data['read_only'])
        self.assertEqual(len(resp.data['flags']), len(F.FLAG_FIELDS))
        self.assertEqual(set(resp.data['editable']) | set(resp.data['readonly']), set(F.FLAG_FIELDS))

    def test_only_patch_is_a_write_method(self):
        view = F.FeatureFlagsAPIView()
        for method in ('post', 'put', 'delete'):
            self.assertFalse(hasattr(view, method), f'{method} у ручки быть не должно')
        self.assertTrue(hasattr(view, 'patch'))


# ── запись (v1.8) ─────────────────────────────────────────────────────────────

def _admin():
    user = _user(role='network_admin')
    user.is_network_admin = True
    user.role = 'network_admin'
    return user


def _patch(data, user=None):
    factory = APIRequestFactory()
    request = factory.patch('/api/v1/settings/features/', data, format='json')
    force_authenticate(request, user=user or _admin())
    return F.FeatureFlagsAPIView.as_view()(request)


class PatchFlagsTest(SimpleTestCase):

    def setUp(self):
        self.cfg = SimpleNamespace(**{name: F._field_default(name) for name in F.FLAG_FIELDS})
        self.cfg.save = MagicMock()
        objects = MagicMock()
        objects.get_or_create.return_value = (self.cfg, False)
        objects.filter.return_value.first.return_value = self.cfg
        p1 = patch.object(F.ClientConfig, 'objects', objects)
        p2 = patch(FP + 'connection', SimpleNamespace(tenant=SimpleNamespace(pk=1)))
        p3 = patch(FP + 'current_schema_name', return_value='dev')
        for p in (p1, p2, p3):
            p.start(); self.addCleanup(p.stop)

    def test_client_cannot_write(self):
        resp = _patch({'birthday_window_days': 5}, user=_user(role='client'))
        self.assertEqual((resp.status_code, resp.data['code']), (403, 'role_not_allowed'))

    def test_unknown_and_readonly_flags(self):
        resp = _patch({'pos_type': 'iiko'})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'unknown_flag'))
        resp = _patch({'web_entry_enabled': True})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'readonly_flag'))
        self.assertIn('editable', resp.data)
        self.cfg.save.assert_not_called()

    def test_patch_network_values_and_source(self):
        resp = _patch({'birthday_window_days': 4, 'rf_orchestrator_enabled': 'true', 'brand_color': '#6a1b9a'})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self.cfg.birthday_window_days, 4)
        self.assertIs(self.cfg.rf_orchestrator_enabled, True)
        self.cfg.save.assert_called_once()
        self.assertEqual(sorted(self.cfg.save.call_args.kwargs['update_fields']),
                         ['birthday_window_days', 'brand_color', 'rf_orchestrator_enabled'])
        self.assertEqual(resp.data['flags']['birthday_window_days'], {'value': 4, 'source': 'network'})
        self.assertFalse(resp.data['read_only'])

    def test_type_errors_are_400(self):
        for body in ({'birthday_window_days': 'abc'}, {'birthday_window_days': -1},
                     {'rf_orchestrator_enabled': 'maybe'}, {'story_campaign_start': '31.12.2026'}):
            with self.subTest(body=body):
                resp = _patch(body)
                self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))

    def test_empty_body_is_400(self):
        resp = _patch({})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))

    def test_branch_override_only_for_overridable_flags(self):
        branch = SimpleNamespace(pk=3, branch_id=990002, name='Точка', config=None)
        branch_cfg = SimpleNamespace(birthday_window_days=None, story_game_enabled=None, story_min_order_amount=None,
                                     story_cafe_address='', story_activation_text='', story_saved_text='')
        branch_cfg.save = MagicMock()
        bc_objects = MagicMock(); bc_objects.get_or_create.return_value = (branch_cfg, False)
        with patch(FP + '_branch_or_none', return_value=branch), patch.object(F.BranchConfig, 'objects', bc_objects):
            resp = _patch({'branch_id': 3, 'auto_broadcast_weekly_cap': 2})
            self.assertEqual((resp.status_code, resp.data['code']), (400, 'no_branch_override'))
            resp = _patch({'branch_id': 3, 'birthday_window_days': 0, 'story_game_enabled': False})
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(branch_cfg.birthday_window_days, 0)
        self.assertIs(branch_cfg.story_game_enabled, False)
        self.assertEqual(resp.data['branch']['branch_id'], 990002)
        self.assertEqual(resp.data['flags']['birthday_window_days'], {'value': 0, 'source': 'branch'},
                         'у окна ДР 0 у точки — настоящий ноль, не наследование')
        self.assertEqual(resp.data['flags']['story_game_enabled']['source'], 'branch')
        self.assertEqual(resp.data['flags']['story_min_order_amount']['source'], 'default')

    def test_branch_not_found_is_404(self):
        with patch(FP + '_branch_or_none', return_value=None):
            resp = _patch({'branch_id': 99, 'birthday_window_days': 1})
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))
