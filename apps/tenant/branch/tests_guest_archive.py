"""
Архив гостя (17.10.2026): «удалить» = в архив, вход гостя возвращает.

Тенантных таблиц в тестовой БД нет — запросы подменяются там, где их делает код:
  1) в архив отправляет только администратор сети, остальным 403, ничего не пишется;
  2) в архив — отметка ВСЕХ ещё не архивных профилей гостя, из архива — снятие;
  3) рассылки и аналитика отсекают архивных там же, где сотрудников.
"""
import pathlib
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant.branch import archive

rf = APIRequestFactory()


class Помощник(SimpleTestCase):
    def test_в_архив_и_обратно_только_нужные_профили(self):
        with patch('apps.tenant.branch.models.ClientBranch.objects') as objs:
            objs.filter.return_value.update.return_value = 2
            гость = SimpleNamespace(pk=5)
            self.assertEqual(archive.в_архив(гость, 'admin@x'), 2)
            objs.filter.assert_called_with(client=гость, archived_at__isnull=True)
            self.assertEqual(objs.filter.return_value.update.call_args.kwargs['archived_by'], 'admin@x')
            archive.из_архива(гость)
            objs.filter.assert_called_with(client=гость, archived_at__isnull=False)
            self.assertEqual(objs.filter.return_value.update.call_args.kwargs,
                             {'archived_at': None, 'archived_by': ''})


class Права(SimpleTestCase):
    def call(self, user):
        from apps.tenant.mobile.api.views import GuestArchiveAPIView
        req = rf.post('/api/v1/guests/42/archive/', {'archived': True}, format='json')
        force_authenticate(req, user=user)
        with patch('apps.shared.users.access.has_tenant_access', return_value=True), \
             patch('apps.shared.users.access.current_schema_name', return_value='levone'), \
             patch.object(GuestArchiveAPIView, 'permission_classes', []), \
             patch('apps.tenant.branch.archive.в_архив', return_value=1) as в_архив, \
             patch('apps.shared.guest.models.Client.objects') as clients, \
             patch('apps.tenant.branch.models.ClientBranch.objects') as cbs:
            clients.filter.return_value.first.return_value = SimpleNamespace(pk=1, vk_id=42)
            cbs.filter.return_value.exists.return_value = True
            cbs.filter.return_value.order_by.return_value.first.return_value = None
            return GuestArchiveAPIView.as_view()(req, vk_id=42), в_архив

    def test_не_админ_403_и_ничего_не_пишет(self):
        r, в_архив = self.call(SimpleNamespace(is_authenticated=True, is_superuser=False, role='manager',
                                               is_network_admin=False, pk=3))
        self.assertEqual(r.status_code, 403)
        в_архив.assert_not_called()

    def test_админ_сети_отправляет_в_архив(self):
        r, в_архив = self.call(SimpleNamespace(is_authenticated=True, is_superuser=False, role='network_admin',
                                               pk=3, email='a@b'))
        self.assertEqual(r.status_code, 200)
        в_архив.assert_called_once()


class ОтсечкаКакУСотрудников(SimpleTestCase):
    def test_рассылки_и_аналитика_отсекают_архивных(self):
        корень = pathlib.Path(__file__).resolve().parents[1]
        for файл in ('senler/engine.py', 'senler/tasks.py', 'senler/services.py', 'senler/api/broadcasts.py',
                     'analytics/autobroadcast_forecast.py', 'analytics/rfm_campaigns.py'):
            for n, строка in enumerate((корень / файл).read_text().split('\n'), 1):
                код = строка.split('#')[0]
                # строки фильтров (`…is_employee=False,` / `…=False)`), не текст докстрингов
                if re.search(r'\w*is_employee=False\s*[,)]', код) and not re.search(r'[А-Яа-я]', код):
                    self.assertIn('archived_at__isnull=True', код, f'{файл}:{n}: архивные гости не отсечены')
