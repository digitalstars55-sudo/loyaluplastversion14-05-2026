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
        self.assertTrue(resp.data['read_only'])
        self.assertEqual(len(resp.data['flags']), len(F.FLAG_FIELDS))

    def test_no_write_methods(self):
        view = F.FeatureFlagsAPIView()
        for method in ('post', 'patch', 'put', 'delete'):
            self.assertFalse(hasattr(view, method), f'{method} у ручки быть не должно')
