"""Мозг маркетинга CheckUp (04.11.2026): не больше двух сообщений гостю за неделю."""
from datetime import date, timedelta

from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.shared.guest.models import Client
from apps.tenant.analytics.rfm_campaigns import within_weekly_cap
from apps.tenant.senler.models import AutoBroadcastLog, AutoBroadcastType


class Лимит(TenantTestCase):
    @classmethod
    def setup_tenant(cls, tenant):
        tenant.client_id = 990177
        tenant.name = 'Лимит'
        tenant.paid_until = date(2099, 1, 1)
        return tenant

    def test_отсекает_только_тех_кому_уже_ушло(self):
        много = Client.objects.create(vk_id=9301, first_name='А')
        один = Client.objects.create(vk_id=9302, first_name='Б')
        пусто = Client.objects.create(vk_id=9303, first_name='В')
        сейчас = timezone.now()
        for гость, n in ((много, 2), (один, 1)):
            for i in range(n):
                AutoBroadcastLog.objects.create(vk_id=гость.vk_id, trigger_type=AutoBroadcastType.AFTER_GAME_3H)
        keep, отсеяно = within_weekly_cap([много.pk, один.pk, пусто.pk], 2, сейчас)
        self.assertEqual(set(keep), {один.pk, пусто.pk})
        self.assertEqual(отсеяно, 1)

    def test_неделя_назад_не_считается(self):
        гость = Client.objects.create(vk_id=9304, first_name='Г')
        for i in range(3):
            AutoBroadcastLog.objects.create(vk_id=гость.vk_id, trigger_type=AutoBroadcastType.AFTER_GAME_3H)
        keep, _ = within_weekly_cap([гость.pk], 2, timezone.now() + timedelta(days=8))
        self.assertEqual(keep, [гость.pk])
