"""
Привязка Телеграма к гостю (15.10.2026): вход через VK ID внутри Телеграм-
мини-аппа запоминает пару, дальше бот пускает гостя без VK ID.
"""

import time
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIRequestFactory

from apps.shared.guest.models import Client, TelegramLink
from apps.shared.guest.tests import TG_TOKEN, tg_init_data
from apps.shared.guest.tg_init import linked_client, remember_link
from apps.shared.guest.web_session import verify_web_token
from apps.tenant.branch.api.views import TelegramSessionView, VKAuthView


def _req(init_data=None):
    meta = {'HTTP_X_TELEGRAM_INIT_DATA': init_data} if init_data else {}
    return APIRequestFactory().post('/', {}, format='json', **meta)


@override_settings(TELEGRAM_GUEST_BOT_TOKEN=TG_TOKEN)
class TelegramLinkTest(TestCase):

    def setUp(self):
        self.guest = Client.objects.create(vk_id=200002444631, first_name='Гость')

    def test_vk_login_inside_telegram_links_and_bot_lets_in(self):
        self.assertTrue(remember_link(_req(tg_init_data(user_id=42)), self.guest))
        self.assertEqual(linked_client(_req(tg_init_data(user_id=42))), self.guest)

        response = TelegramSessionView.as_view()(_req(tg_init_data(user_id=42)))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['vk_id'], 200002444631)
        self.assertEqual(verify_web_token(response.data['web_token']), 200002444631)

    def test_unknown_telegram_gets_404(self):
        response = TelegramSessionView.as_view()(_req(tg_init_data(user_id=43)))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data['code'], 'tg_not_linked')

    def test_forged_or_missing_init_data_never_links_or_logs_in(self):
        forged = tg_init_data(user_id=42, token='999:other-bot')
        self.assertFalse(remember_link(_req(forged), self.guest))
        self.assertFalse(remember_link(_req(None), self.guest))
        TelegramLink.objects.create(tg_user_id=42, client=self.guest)
        self.assertEqual(TelegramSessionView.as_view()(_req(forged)).status_code, 404)
        self.assertEqual(TelegramSessionView.as_view()(_req(None)).status_code, 404)

    def test_stale_launch_does_not_log_in(self):
        TelegramLink.objects.create(tg_user_id=42, client=self.guest)
        old = tg_init_data(user_id=42, auth_date=int(time.time()) - 3 * 86400)
        self.assertEqual(TelegramSessionView.as_view()(_req(old)).status_code, 404)

    def test_relink_to_latest_vk_account(self):
        other = Client.objects.create(vk_id=777)
        remember_link(_req(tg_init_data(user_id=42)), self.guest)
        remember_link(_req(tg_init_data(user_id=42)), other)
        self.assertEqual(TelegramLink.objects.get(tg_user_id=42).client, other)

    @patch('apps.tenant.branch.api.views.vk_web_auth')
    def test_vk_auth_view_reports_link(self, mock_auth):
        profile = MagicMock()
        profile.client = self.guest
        mock_auth.return_value = (profile, False)
        with patch('apps.tenant.branch.api.views.ClientProfileResponseSerializer') as ser:
            ser.side_effect = lambda *_a, **_k: MagicMock(data={'vk_id': self.guest.vk_id})
            from apps.tenant.branch.api.tests import _VK_AUTH_PAYLOAD as body
            inside = VKAuthView.as_view()(APIRequestFactory().post(
                '/', body, format='json', HTTP_X_TELEGRAM_INIT_DATA=tg_init_data(user_id=55)))
            outside = VKAuthView.as_view()(APIRequestFactory().post('/', body, format='json'))
        self.assertEqual(inside.status_code, status.HTTP_200_OK, inside.data)
        self.assertTrue(inside.data['telegram_linked'])
        self.assertFalse(outside.data['telegram_linked'])
        self.assertEqual(TelegramLink.objects.get(tg_user_id=55).client, self.guest)
