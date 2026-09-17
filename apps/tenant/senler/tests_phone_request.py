"""Авторассылка «Просьба поделиться номером» (№78): кнопка и плейсхолдер {награда}. Без БД."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.tenant.senler import engine
from apps.tenant.senler.engine import Candidate, get_events, phone_request_keyboard, render_text


def _cand(branch_id=239014483):
    cb = SimpleNamespace(branch=SimpleNamespace(branch_id=branch_id), client=SimpleNamespace(first_name='Лев'))
    return Candidate(client_branch=cb, vk_id=123456789, entity_key='phone:123456789')


@override_settings(VK_MINI_APP_ID=53418653)
class PhoneRequestKeyboardTest(SimpleTestCase):

    def test_button_opens_app_with_company_branch_and_phone(self):
        with patch.object(engine, '_tenant_client_id', return_value=100), \
                patch.object(engine, '_phone_reward_coins', return_value=50):
            kb = phone_request_keyboard(_cand())
        self.assertTrue(kb['inline'])
        button = kb['buttons'][0][0]
        self.assertEqual(button['color'], 'primary')
        btn = button['action']
        self.assertEqual(btn['type'], 'callback')
        payload = json.loads(btn['payload'])
        self.assertEqual(payload['lu'], 'phone_request')
        self.assertEqual(payload['url'], 'https://vk.com/app53418653/#/?company=100&branch=239014483&source=rfm&phone=true')
        self.assertLessEqual(len(btn['payload']), 255)
        self.assertEqual(btn['label'], 'Оставить номер · +50 баллов')

    def test_label_without_reward(self):
        with patch.object(engine, '_tenant_client_id', return_value=100), \
                patch.object(engine, '_phone_reward_coins', return_value=0):
            kb = phone_request_keyboard(_cand(branch_id=None))
        btn = kb['buttons'][0][0]['action']
        self.assertEqual(json.loads(btn['payload'])['url'], 'https://vk.com/app53418653/#/?company=100&source=rfm&phone=true')
        self.assertEqual(btn['label'], 'Поделиться номером')

    def test_no_company_no_keyboard(self):
        with patch.object(engine, '_tenant_client_id', return_value=None):
            self.assertIsNone(phone_request_keyboard(_cand()))


class PhoneRequestEventTest(SimpleTestCase):

    def test_event_registered_with_entity_dedup_and_default_delay(self):
        from apps.tenant.senler.models import AutoBroadcastType
        spec = get_events()[AutoBroadcastType.PHONE_REQUEST]
        self.assertEqual(spec.dedup, engine.DEDUP_ENTITY)
        self.assertEqual(spec.default_delay_days, 1)
        self.assertIn('{награда}', spec.placeholders)
        self.assertEqual(engine.PHONE_REQUEST_EVENT, AutoBroadcastType.PHONE_REQUEST)

    def test_render_text_reward_placeholder(self):
        rule = SimpleNamespace(message_text='Привет, {имя}! За номер — {награда} баллов.')
        with patch.object(engine, '_phone_reward_coins', return_value=50), \
                patch.object(engine, 'tenant_addresses', return_value=''):
            self.assertEqual(render_text(rule, _cand()), 'Привет, Лев! За номер — 50 баллов.')
