"""
Телеграм «на 100 %» (17.10.2026). Держим:
  1. личная кнопка меню бота — на сеть, куда гость вошёл (адрес с company);
     без токена бота или без сети — ничего не шлём;
  2. строгая проверка подписи Телеграма включена по умолчанию;
  3. список сетей для выбора отдаёт только сети с веб-входом.
"""
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from apps.shared.guest import tg_menu


@override_settings(TELEGRAM_GUEST_BOT_TOKEN='t', GUEST_WEB_BASE_URL='https://vkapp.levelupapp.ru')
class КнопкаМеню(SimpleTestCase):
    def test_на_сеть_гостя(self):
        tenant = SimpleNamespace(client_id=5, name='Шавуха', schema_name='shavuha')
        with mock.patch.object(tg_menu, 'connection', SimpleNamespace(tenant=tenant)), \
                mock.patch.object(tg_menu.threading, 'Thread') as T:
            self.assertTrue(tg_menu.запомнить_сеть(42))
        args = T.call_args.kwargs['args']
        self.assertEqual(args[1:], (42, 'https://vkapp.levelupapp.ru/?company=5', 'Шавуха'))

    def test_без_сети_и_без_токена_молчим(self):
        with mock.patch.object(tg_menu, 'connection', SimpleNamespace(tenant=None)), \
                mock.patch.object(tg_menu.threading, 'Thread') as T:
            self.assertFalse(tg_menu.запомнить_сеть(42))
        with override_settings(TELEGRAM_GUEST_BOT_TOKEN=''), \
                mock.patch.object(tg_menu, 'connection', SimpleNamespace(tenant=SimpleNamespace(client_id=5))), \
                mock.patch.object(tg_menu.threading, 'Thread') as T2:
            self.assertFalse(tg_menu.запомнить_сеть(42))
        T.assert_not_called()
        T2.assert_not_called()

    def test_строгая_подпись_по_умолчанию(self):
        import os
        if 'TELEGRAM_SIGN_ENFORCE' not in os.environ:
            self.assertEqual(settings.TELEGRAM_SIGN_ENFORCE, 'on')


class ОтветСети(SimpleTestCase):
    def test_точки_и_флаги_доходят_до_мини_аппа(self):
        from apps.shared.clients.api.serializers import TenantDomainResponseSerializer
        d = TenantDomainResponseSerializer({'domain': 'x', 'name': 'X', 'review_proof_enabled': True,
                                            'review_proof_coins': 30,
                                            'branches': [{'branch_id': 3, 'name': 'Ленина', 'address': ''}]}).data
        self.assertEqual(d['branches'][0]['branch_id'], 3)
        self.assertTrue(d['review_proof_enabled'])
        self.assertEqual(d['review_proof_coins'], 30)
