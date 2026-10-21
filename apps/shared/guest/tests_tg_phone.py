"""
Привязка Телеграма к гостю по номеру (21.10.2026): для тех, кому VK ID внутри
Телеграма недоступен (вход по электронному ключу).
"""
import json
import time
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory

from apps.shared.guest.models import Client, TelegramLink
from apps.shared.guest.tests import TG_TOKEN, tg_init_data
from apps.shared.guest.web_session import verify_web_token
from apps.tenant.branch.api.views import TelegramPhoneLinkView


def _контакт(user_id=42, phone='79001234567', token=TG_TOKEN):
    """Ответ requestContact так, как его подписывает Телеграм (поля, как у initData)."""
    return tg_init_data(user_id=user_id, token=token, extra={
        'contact': json.dumps({'user_id': user_id, 'phone_number': phone, 'first_name': 'Гость'},
                              separators=(',', ':')),
    })


def _контакт_как_у_телеграма(user_id=42, phone='79001234567', token=TG_TOKEN):
    """Точная форма getRequestedContact: только contact, auth_date и hash — без user и query_id."""
    import hashlib
    import hmac
    from urllib.parse import urlencode
    fields = {
        'auth_date': str(int(time.time())),
        'contact': json.dumps({'user_id': user_id, 'phone_number': phone, 'first_name': 'Гость'},
                              separators=(',', ':')),
    }
    check = '\n'.join(f'{k}={fields[k]}' for k in sorted(fields))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def _запрос(contact, tg_id=42):
    meta = {'HTTP_X_TELEGRAM_INIT_DATA': tg_init_data(user_id=tg_id)} if tg_id else {}
    return APIRequestFactory().post('/', {'contact': contact}, format='json', **meta)


@override_settings(TELEGRAM_GUEST_BOT_TOKEN=TG_TOKEN)
class ПривязкаПоНомеру(TestCase):

    def setUp(self):
        self.vk = Client.objects.create(vk_id=200002444631, first_name='Левон', phone='+79001234567',
                                        phone_source='vk')

    def _привязать(self, contact, tg_id=42):
        with patch('apps.shared.guest.merge.склеить') as склеить:
            r = TelegramPhoneLinkView.as_view()(_запрос(contact, tg_id))
        return r, склеить

    def test_свой_номер_привязывает_и_вливает_карточку_из_телеграма(self):
        служебная = Client.objects.create(vk_id=9000000000042, is_synthetic_vk=True)
        TelegramLink.objects.create(tg_user_id=42, client=служебная)
        r, склеить = self._привязать(_контакт())
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['vk_id'], self.vk.vk_id)
        self.assertEqual(verify_web_token(r.data['web_token']), self.vk.vk_id)
        self.assertEqual(TelegramLink.objects.get(tg_user_id=42).client, self.vk)
        склеить.assert_called_once()
        self.assertEqual(склеить.call_args[0][:2], (служебная, self.vk))

    def test_строка_ровно_как_у_телеграма_без_user(self):
        r, _ = self._привязать(_контакт_как_у_телеграма(phone='+79001234567'))
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(TelegramLink.objects.get(tg_user_id=42).client, self.vk)

    def test_номер_с_восьмёркой_тоже_находит(self):
        r, _ = self._привязать(_контакт(phone='89001234567'))
        self.assertEqual(r.status_code, 200)

    def test_чужой_номер_не_привязывает(self):
        чужой = _контакт(user_id=99)          # контакт другого пользователя Телеграма
        r, _ = self._привязать(чужой)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data['code'], 'contact_foreign')
        self.assertFalse(TelegramLink.objects.exists())

    def test_поддельная_подпись_не_привязывает(self):
        r, _ = self._привязать(_контакт(token='999:other-bot'))
        self.assertEqual(r.data['code'], 'contact_invalid')
        self.assertFalse(TelegramLink.objects.exists())

    def test_без_телеграма_нельзя(self):
        r, _ = self._привязать(_контакт(), tg_id=None)
        self.assertEqual(r.status_code, 403)

    def test_старый_контакт_не_принимается(self):
        старый = tg_init_data(user_id=42, auth_date=int(time.time()) - 3 * 86400, extra={
            'contact': json.dumps({'user_id': 42, 'phone_number': '79001234567'})})
        r, _ = self._привязать(старый)
        self.assertEqual(r.data['code'], 'contact_invalid')

    def test_неподтверждённый_номер_не_склеивает(self):
        self.vk.phone_source = 'vk_unverified'
        self.vk.save(update_fields=['phone_source'])
        r, _ = self._привязать(_контакт())
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.data['code'], 'phone_not_found')

    def test_два_гостя_с_одним_номером_не_гадаем(self):
        Client.objects.create(vk_id=777, phone='+79001234567', phone_source='vk')
        r, склеить = self._привязать(_контакт())
        self.assertEqual(r.status_code, 409)
        склеить.assert_not_called()
        self.assertFalse(TelegramLink.objects.exists())
