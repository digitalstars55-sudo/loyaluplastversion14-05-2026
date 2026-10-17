"""
Склейка гостя, шаг Б (17.10.2026). Держим:
  1. склейка переносит каналы, привязку Телеграма и пустые поля к цели,
     источник помечен «влит», журнал знает каждое поле;
  2. отмена возвращает всё, повторная отмена — отказ;
  3. две настоящие страницы ВК сами не склеиваются (только руками);
  4. гость из Телеграма без ВК — служебная карточка; вошёл потом через VK ID
     в Телеграме — служебная вливается в настоящую сама;
  5. «возможно, один человек» — пары с тем же номером, склеенные не предлагаются.
Таблиц сетей в тестовой базе нет — проход по сетям заглушён (на копии прода
склейка проверяется отдельно, вся сеть целиком в откатываемой транзакции).
"""
from unittest import mock

from django.test import RequestFactory, TestCase, override_settings

from apps.shared.guest import merge as M
from apps.shared.guest.channels import СЛУЖЕБНЫЙ_VK_ОТ
from apps.shared.guest.models import Client, GuestChannel, GuestMerge, TelegramLink

БЕЗ_СЕТЕЙ = mock.patch('django_tenants.utils.get_tenant_model')


class _Сети:
    def setUp(self):
        p = БЕЗ_СЕТЕЙ.start()
        p.return_value.objects.exclude.return_value = []
        self.addCleanup(БЕЗ_СЕТЕЙ.stop)
        patcher = mock.patch('apps.tenant.branch.archive.из_архива')
        patcher.start()
        self.addCleanup(patcher.stop)


class Склейка(_Сети, TestCase):
    def test_перенос_и_отмена(self):
        тг = M.из_телеграма(555, {'first_name': 'Аня'})
        настоящая = Client.objects.create(vk_id=800001)
        self.assertTrue(тг.is_synthetic_vk)
        self.assertEqual(тг.vk_id, СЛУЖЕБНЫЙ_VK_ОТ + 555)
        g = M.склеить(тг, настоящая, reason='tg', by='тест')
        настоящая.refresh_from_db()
        тг.refresh_from_db()
        self.assertEqual(тг.merged_into_id, настоящая.pk)
        self.assertEqual(TelegramLink.objects.get(tg_user_id=555).client, настоящая)
        self.assertTrue(GuestChannel.objects.filter(client=настоящая, kind='tg', external_id='555').exists())
        self.assertEqual(настоящая.first_name, 'Аня')
        self.assertTrue(g.moves)
        M.отменить(g, by='тест')
        тг.refresh_from_db()
        настоящая.refresh_from_db()
        self.assertIsNone(тг.merged_into_id)
        self.assertEqual(TelegramLink.objects.get(tg_user_id=555).client, тг)
        self.assertEqual(настоящая.first_name, '')
        with self.assertRaises(M.Отказ):
            M.отменить(GuestMerge.objects.get(pk=g.pk))

    def test_две_страницы_вк_сами_не_склеиваются(self):
        a = Client.objects.create(vk_id=800002)
        b = Client.objects.create(vk_id=800003)
        with self.assertRaises(M.Отказ):
            M.склеить(a, b, reason='phone')
        M.склеить(a, b, reason='manual')          # руками — можно
        with self.assertRaises(M.Отказ):
            M.склеить(a, b, reason='manual')      # уже влит

    def test_вход_через_vk_id_в_телеграме_вливает_служебную(self):
        from apps.shared.guest import tg_init
        тг = M.из_телеграма(777, {})
        настоящая = Client.objects.create(vk_id=800004)
        with mock.patch.object(tg_init, 'proven_tg_user_id', return_value=777):
            self.assertTrue(tg_init.remember_link(RequestFactory().post('/'), настоящая))
        тг.refresh_from_db()
        self.assertEqual(тг.merged_into_id, настоящая.pk)
        self.assertEqual(GuestMerge.objects.get().reason, 'tg')

    def test_возможно_один(self):
        a = Client.objects.create(vk_id=800005, phone='+79100000009', phone_source='vk')
        b = Client.objects.create(vk_id=800006, phone='+79100000009', phone_source='vk_unverified')
        пары = M.возможно_один()
        self.assertEqual([(p['a'], p['b']) for p in пары], [(a.pk, b.pk)])
        M.склеить(b, a, reason='manual')
        self.assertEqual(M.возможно_один(), [])


class ГостьБезВк(_Сети, TestCase):
    @override_settings(TELEGRAM_GUEST_WITHOUT_VK=True)
    def test_флаг_включён_карточка_создаётся(self):
        from apps.tenant.branch.api.views import TelegramSessionView
        r = RequestFactory().post('/api/v1/vk/tg-session/')
        with mock.patch('apps.tenant.branch.api.views.linked_client', return_value=None), \
                mock.patch('apps.shared.guest.tg_init.proven_tg_user', return_value={'id': 901, 'first_name': 'Иван'}), \
                mock.patch('apps.tenant.branch.api.views.issue_web_token', return_value=('t', mock.Mock(isoformat=lambda: 'x'))):
            resp = TelegramSessionView.as_view()(r)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data['vk_id'], СЛУЖЕБНЫЙ_VK_ОТ + 901)

    def test_флаг_выключен_как_раньше(self):
        from apps.tenant.branch.api.views import TelegramSessionView
        r = RequestFactory().post('/api/v1/vk/tg-session/')
        with mock.patch('apps.tenant.branch.api.views.linked_client', return_value=None):
            resp = TelegramSessionView.as_view()(r)
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(Client.objects.filter(is_synthetic_vk=True).exists())
