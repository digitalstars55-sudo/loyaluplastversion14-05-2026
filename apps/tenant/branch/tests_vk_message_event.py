"""Нажатие callback-кнопки «поделиться номером» (№78): ответ ВК «открыть ссылку». Без БД."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.tenant.branch.api.vk_message_event import build_payload, event_data_for_link, handle_message_event

LINK = 'https://vk.com/app53418653/#/?company=1&branch=2&source=rfm&phone=true'
CFG = SimpleNamespace(vk_community_token='tok', vk_group_id=211202938)
OPEN_APP = {'type': 'open_app', 'app_id': 53418653, 'hash': '/?company=1&branch=2&source=rfm&phone=true', 'owner_id': -211202938}


def _event(payload, **kw):
    obj = {'event_id': 'e1', 'user_id': 123, 'peer_id': 123, 'payload': payload}
    obj.update(kw)
    return obj


class MessageEventTest(SimpleTestCase):

    def test_opens_link_via_event_answer(self):
        resp = MagicMock()
        resp.json.return_value = {'response': 1}
        with patch('apps.tenant.branch.api.vk_message_event.requests.post', return_value=resp) as post:
            self.assertTrue(handle_message_event(CFG, _event(json.loads(build_payload(LINK)))))
        data = post.call_args[1]['data']
        self.assertEqual((data['event_id'], data['user_id'], data['peer_id']), ('e1', 123, 123))
        self.assertEqual(json.loads(data['event_data']), OPEN_APP)
        self.assertEqual(data['access_token'], 'tok')

    def test_event_data_open_app_vs_open_link(self):
        self.assertEqual(event_data_for_link(LINK, 211202938), OPEN_APP)
        self.assertEqual(event_data_for_link(LINK, None), {'type': 'open_app', 'app_id': 53418653, 'hash': '/?company=1&branch=2&source=rfm&phone=true'})
        self.assertEqual(event_data_for_link('https://vk.com/apparel', 1), {'type': 'open_link', 'link': 'https://vk.com/apparel'})

    def test_payload_as_string_is_accepted(self):
        resp = MagicMock()
        resp.json.return_value = {'response': 1}
        with patch('apps.tenant.branch.api.vk_message_event.requests.post', return_value=resp):
            self.assertTrue(handle_message_event(CFG, _event(build_payload(LINK))))

    def test_foreign_button_ignored(self):
        with patch('apps.tenant.branch.api.vk_message_event.requests.post') as post:
            self.assertFalse(handle_message_event(CFG, _event({'command': 'start'})))
            self.assertFalse(handle_message_event(CFG, _event('not json')))
            self.assertFalse(handle_message_event(CFG, _event(None)))
        post.assert_not_called()

    def test_link_outside_vk_app_rejected(self):
        with patch('apps.tenant.branch.api.vk_message_event.requests.post') as post:
            self.assertFalse(handle_message_event(CFG, _event({'lu': 'phone_request', 'url': 'https://evil.example/'})))
        post.assert_not_called()

    def test_vk_error_and_network_failure_are_soft(self):
        resp = MagicMock()
        resp.json.return_value = {'error': {'error_msg': 'boom'}}
        with patch('apps.tenant.branch.api.vk_message_event.requests.post', return_value=resp):
            self.assertFalse(handle_message_event(CFG, _event(build_payload(LINK))))
        with patch('apps.tenant.branch.api.vk_message_event.requests.post', side_effect=RuntimeError('net')):
            self.assertFalse(handle_message_event(CFG, _event(build_payload(LINK))))

    def test_no_token(self):
        with patch('apps.tenant.branch.api.vk_message_event.requests.post') as post:
            self.assertFalse(handle_message_event(SimpleNamespace(vk_community_token=''), _event(build_payload(LINK))))
        post.assert_not_called()

    def test_payload_fits_vk_limit(self):
        self.assertLessEqual(len(build_payload(LINK)), 255)
