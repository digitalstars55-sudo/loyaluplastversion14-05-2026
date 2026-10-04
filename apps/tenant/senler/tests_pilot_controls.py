from contextlib import contextmanager
from datetime import datetime, timezone as utc
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings

from apps.tenant.senler.pilot_controls import guard_pilot, limits, pilot_lock, remaining_contacts


class PilotLimits(SimpleTestCase):
    def test_existing_unconfigured_tenants_keep_unlimited_behaviour(self):
        self.assertEqual(limits(None), (None, 0))
        self.assertEqual(limits(SimpleNamespace()), (None, 0))

    def test_owner_cap_is_passed_to_reward_picker(self):
        from apps.tenant.inventory.reward_catalog import available_items
        cost, contacts = limits(SimpleNamespace(rf_reward_max_cost_rub=Decimal('100'), rf_daily_contact_limit=10))
        cheap = SimpleNamespace(effective_cost_price=Decimal('88.10'))
        expensive = SimpleNamespace(effective_cost_price=Decimal('100.01'))
        qs = MagicMock();qs.filter.return_value=qs;qs.select_related.return_value=qs
        qs.__iter__.return_value=iter([cheap, expensive])
        with patch('apps.tenant.inventory.models.RewardCatalogItem.objects.filter', return_value=qs):
            self.assertEqual(available_items('G3', max_cost_price=cost), [cheap])
        self.assertEqual(contacts, 10)

    @override_settings(TIME_ZONE='Europe/Moscow')
    def test_daily_budget_uses_moscow_day_and_cannot_go_negative(self):
        now = datetime(2026, 10, 4, 21, 30, tzinfo=utc.utc)
        with patch('apps.tenant.senler.models.AutoBroadcastLog.objects.filter') as query:
            query.return_value.count.return_value=9
            self.assertEqual(remaining_contacts(10, now, {'no_visit_days'}), 1)
            boundary=query.call_args.kwargs['sent_at__gte']
            self.assertEqual((boundary.day, boundary.hour), (5, 0))
            query.return_value.count.return_value=12
            self.assertEqual(remaining_contacts(10, now, {'no_visit_days'}), 0)

    def test_busy_pilot_does_not_send_and_preview_does_not_lock(self):
        @contextmanager
        def busy():yield False
        function=Mock(return_value={'would_send':1})
        guarded=guard_pilot(function)
        config=SimpleNamespace(rf_reward_max_cost_rub=100, rf_daily_contact_limit=10)
        rule=SimpleNamespace(event='no_visit_days')
        with patch('apps.tenant.senler.engine._tenant_client_config', return_value=config), \
             patch('apps.tenant.senler.engine._rf_events', return_value={'no_visit_days'}), \
             patch('apps.tenant.senler.pilot_controls.pilot_lock', busy):
            self.assertEqual(guarded(rule)['reason'], 'pilot_running')
            function.assert_not_called()
            guarded(rule,dry_run=True)
            function.assert_called_once()


class PilotDatabaseLock(TransactionTestCase):
    def test_error_releases_real_postgres_lock(self):
        config=SimpleNamespace(rf_daily_contact_limit=10)
        rule=SimpleNamespace(event='no_visit_days')
        @guard_pilot
        def fail(rule, now=None, dry_run=False):raise RuntimeError('delivery failed')
        with patch('apps.tenant.senler.engine._tenant_client_config', return_value=config), \
             patch('apps.tenant.senler.engine._rf_events', return_value={'no_visit_days'}):
            with self.assertRaises(RuntimeError):fail(rule)
        with pilot_lock() as acquired:self.assertTrue(acquired)
