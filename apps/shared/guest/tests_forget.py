"""«Удалить мои данные» (15.10.2026): только доказанный гость, обезличивание + журнал."""

from unittest.mock import patch

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIRequestFactory

from apps.shared.guest.forget import УДАЛЕНО, забыть, условный_vk, хэш_vk
from apps.shared.guest.models import Client, GuestForgetLog, TelegramLink
from apps.tenant.branch.api.views import ClientForgetView


def _post(data, vk_user_id=None, status_='valid'):
    request = APIRequestFactory().post('/', data, format='json')
    request.vk_user_id = vk_user_id
    request.vk_sign_status = status_
    return ClientForgetView.as_view()(request)


class _NoTenants:
    """Сетей нет — проверяем общую часть; сетевая — отдельным тестом с моками."""
    class objects:
        @staticmethod
        def exclude(**_kw):
            return []


@patch('django_tenants.utils.get_tenant_model', lambda: _NoTenants)
class ForgetTest(TestCase):

    def setUp(self):
        self.guest = Client.objects.create(vk_id=200002444631, first_name='Анна', last_name='Петрова',
                                           photo_url='https://vk.com/a.jpg', phone='+79001234567')
        TelegramLink.objects.create(tg_user_id=42, client=self.guest)

    def test_guest_is_anonymised_and_logged_without_vk_id(self):
        забыть(self.guest, via='valid')
        self.guest.refresh_from_db()
        self.assertEqual(self.guest.vk_id, условный_vk(self.guest))
        self.assertEqual((self.guest.first_name, self.guest.last_name, self.guest.photo_url, self.guest.phone),
                         ('Гость', '', '', ''))
        self.assertFalse(TelegramLink.objects.exists())
        log = GuestForgetLog.objects.get()
        self.assertEqual(log.vk_id_hash, хэш_vk(200002444631))
        self.assertNotIn('200002444631', str(log.counts) + log.vk_id_hash)

    def test_second_call_is_noop(self):
        забыть(self.guest)
        self.assertEqual(забыть(Client.objects.get(pk=self.guest.pk)), {})
        self.assertEqual(GuestForgetLog.objects.count(), 1)

    def test_view_requires_proof_not_body_vk_id(self):
        r = _post({'confirm': True, 'vk_id': 200002444631}, vk_user_id=None, status_='missing')
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Client.objects.get(pk=self.guest.pk).vk_id, 200002444631)

    def test_view_requires_confirm(self):
        r = _post({}, vk_user_id=200002444631)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Client.objects.get(pk=self.guest.pk).first_name, 'Анна')

    def test_view_forgets_proven_guest(self):
        r = _post({'confirm': True}, vk_user_id=200002444631, status_='web')
        self.assertEqual(r.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Client.objects.get(pk=self.guest.pk).first_name, 'Гость')
        self.assertEqual(GuestForgetLog.objects.get().via, 'web')

    def test_tenant_part_runs_per_network(self):
        class _One:
            class objects:
                @staticmethod
                def exclude(**_kw):
                    return [type('T', (), {'schema_name': 'levone'})()]
        with patch('django_tenants.utils.get_tenant_model', lambda: _One), \
                patch('django_tenants.utils.schema_context'), \
                patch('apps.shared.guest.forget._в_сети', return_value={'messages': 3}) as сеть:
            counts = забыть(self.guest)
        сеть.assert_called_once_with(self.guest, 200002444631, условный_vk(self.guest))
        self.assertEqual(counts['levone'], {'messages': 3})
        self.assertEqual(УДАЛЕНО, '[удалено по просьбе гостя]')
