"""
Тесты мониторинга.

Проверки (checks.py) тестируются SimpleTestCase — без БД и без сети: всё
внешнее подменяется фейковым fetch_*, «сегодня» приходит параметром. Доставка
(alerts.py) требует БД ради MonitorAlertState, но Expo и получателей мокаем —
тестовый прогон не имеет права разбудить владельца настоящим пушем.
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .alerts import deliver, should_send
from .checks import (
    SEVERITY_CRITICAL,
    SEVERITY_WARN,
    Finding,
    _parse_whois_date,
    check_domains,
    check_probes,
    check_tenants_paid,
    check_tls,
    check_vk_callbacks,
    heal_vk_callbacks,
)
from .models import MonitorAlertState

TODAY = date(2026, 9, 16)
WARN_DAYS = 14


def _by_key(findings):
    return {f.key: f for f in findings}


# ── TLS ───────────────────────────────────────────────────────────────────────

class CheckTlsTest(SimpleTestCase):

    def _fetch(self, mapping):
        def fetch(host):
            value = mapping[host]
            if isinstance(value, Exception):
                raise value
            return value
        return fetch

    def test_far_expiry_is_silent(self):
        found = check_tls(['a.ru'], TODAY, WARN_DAYS, self._fetch({'a.ru': TODAY + timedelta(days=60)}))
        self.assertEqual(found, [])

    def test_warn_window(self):
        found = check_tls(['a.ru'], TODAY, WARN_DAYS, self._fetch({'a.ru': TODAY + timedelta(days=14)}))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].key, 'tls:a.ru')
        self.assertEqual(found[0].severity, SEVERITY_WARN)
        self.assertEqual(found[0].data['days_left'], 14)

    def test_three_days_left_is_critical(self):
        found = check_tls(['a.ru'], TODAY, WARN_DAYS, self._fetch({'a.ru': TODAY + timedelta(days=3)}))
        self.assertEqual(found[0].severity, SEVERITY_CRITICAL)

    def test_expired_is_critical(self):
        found = check_tls(['a.ru'], TODAY, WARN_DAYS, self._fetch({'a.ru': TODAY - timedelta(days=2)}))
        self.assertEqual(found[0].key, 'tls:a.ru')
        self.assertEqual(found[0].severity, SEVERITY_CRITICAL)
        self.assertEqual(found[0].data['days_left'], -2)

    def test_connection_error_is_separate_critical_signal(self):
        found = check_tls(['a.ru'], TODAY, WARN_DAYS, self._fetch({'a.ru': OSError('timed out')}))
        self.assertEqual(found[0].key, 'tls:a.ru:error')
        self.assertEqual(found[0].severity, SEVERITY_CRITICAL)
        self.assertIn('timed out', found[0].body)

    def test_one_broken_host_does_not_stop_the_rest(self):
        found = check_tls(
            ['bad.ru', 'good.ru', 'soon.ru'],
            TODAY, WARN_DAYS,
            self._fetch({
                'bad.ru': OSError('нет связи'),
                'good.ru': TODAY + timedelta(days=100),
                'soon.ru': TODAY + timedelta(days=5),
            }),
        )
        keys = _by_key(found)
        self.assertEqual(set(keys), {'tls:bad.ru:error', 'tls:soon.ru'})


# ── Домены ────────────────────────────────────────────────────────────────────

class CheckDomainsTest(SimpleTestCase):

    def test_whois_wins_over_fallback(self):
        found = check_domains(
            {'levelupapp.ru': '2027-01-01'}, TODAY, WARN_DAYS,
            whois_paid_till=lambda d: TODAY + timedelta(days=5),
        )
        self.assertEqual(found[0].key, 'domain:levelupapp.ru')
        self.assertEqual(found[0].data['source'], 'whois')
        self.assertEqual(found[0].data['days_left'], 5)

    def test_fallback_used_when_whois_silent(self):
        fallback = (TODAY + timedelta(days=4)).isoformat()
        found = check_domains({'levonework.ru': fallback}, TODAY, WARN_DAYS, whois_paid_till=lambda d: None)
        self.assertEqual(found[0].data['source'], 'fallback')
        self.assertIn('сверь после продления', found[0].body)

    def test_fallback_used_when_whois_raises(self):
        def boom(domain):
            raise OSError('reset by peer')

        fallback = (TODAY + timedelta(days=1)).isoformat()
        found = check_domains({'levonework.ru': fallback}, TODAY, WARN_DAYS, whois_paid_till=boom)
        self.assertEqual(found[0].severity, SEVERITY_CRITICAL)
        self.assertEqual(found[0].data['source'], 'fallback')

    def test_whois_date_formats(self):
        # Реестр отдаёт paid-till то с временем, то через точки.
        self.assertEqual(_parse_whois_date('2026-12-19T21:00:00Z'), date(2026, 12, 19))
        self.assertEqual(_parse_whois_date('2026.12.19'), date(2026, 12, 19))
        self.assertEqual(_parse_whois_date('  2026-12-19  '), date(2026, 12, 19))
        self.assertIsNone(_parse_whois_date('никогда'))
        self.assertIsNone(_parse_whois_date(''))

    def test_date_lands_in_data(self):
        found = check_domains({'x.ru': ''}, TODAY, WARN_DAYS, whois_paid_till=lambda d: date(2026, 9, 20))
        self.assertEqual(found[0].data['paid_till'], '2026-09-20')

    def test_expired_domain_is_critical(self):
        found = check_domains({'x.ru': ''}, TODAY, WARN_DAYS, whois_paid_till=lambda d: TODAY - timedelta(days=1))
        self.assertEqual(found[0].severity, SEVERITY_CRITICAL)

    def test_no_date_at_all_is_not_a_signal(self):
        found = check_domains({'x.ru': 'непонятно'}, TODAY, WARN_DAYS, whois_paid_till=lambda d: None)
        self.assertEqual(found, [])

    def test_far_date_is_silent(self):
        found = check_domains({'x.ru': ''}, TODAY, WARN_DAYS, whois_paid_till=lambda d: TODAY + timedelta(days=90))
        self.assertEqual(found, [])


# ── Оплата сетей ──────────────────────────────────────────────────────────────

class CheckTenantsPaidTest(SimpleTestCase):

    def _company(self, schema_name, days=None, is_active=True, name=None):
        return SimpleNamespace(
            schema_name=schema_name,
            name=name or schema_name,
            is_active=is_active,
            paid_until=None if days is None else TODAY + timedelta(days=days),
        )

    def test_public_and_inactive_and_empty_are_skipped(self):
        found = check_tenants_paid([
            self._company('public', days=-100),
            self._company('asap_murom', days=-30, is_active=False),
            self._company('dev', days=None),
        ], TODAY, WARN_DAYS)
        self.assertEqual(found, [])

    def test_expired_is_critical(self):
        found = check_tenants_paid([self._company('asap_orel', days=-5, name='Автосуши Орёл')], TODAY, WARN_DAYS)
        self.assertEqual(found[0].key, 'paid:asap_orel')
        self.assertEqual(found[0].severity, SEVERITY_CRITICAL)
        self.assertIn('гард', found[0].body)
        self.assertEqual(found[0].data['days_left'], -5)

    def test_soon_is_warn(self):
        found = check_tenants_paid([self._company('levone', days=10)], TODAY, WARN_DAYS)
        self.assertEqual(found[0].severity, SEVERITY_WARN)

    def test_today_is_not_yet_expired(self):
        # Оплачено «по сегодня» — это ещё не долг, но уже повод предупредить.
        found = check_tenants_paid([self._company('levone', days=0)], TODAY, WARN_DAYS)
        self.assertEqual(found[0].severity, SEVERITY_WARN)

    def test_far_paid_is_silent(self):
        found = check_tenants_paid([self._company('levone', days=200)], TODAY, WARN_DAYS)
        self.assertEqual(found, [])


# ── Callback ВК ───────────────────────────────────────────────────────────────

class CheckVkCallbacksTest(SimpleTestCase):

    def test_failed_our_server_is_critical(self):
        found = check_vk_callbacks(
            [('levone', 111, 'tok')],
            lambda gid, tok: [{'url': 'https://levone.levelupapp.ru/api/v1/vk/callback/', 'status': 'failed', 'title': 'LoyalUP'}],
        )
        self.assertEqual(found[0].key, 'vkcb:levone:111')
        self.assertEqual(found[0].severity, SEVERITY_CRITICAL)
        self.assertEqual(found[0].data['servers'][0]['status'], 'failed')

    def test_ok_server_is_silent(self):
        found = check_vk_callbacks(
            [('levone', 111, 'tok')],
            lambda gid, tok: [{'url': 'https://levone.levelupapp.ru/api/v1/vk/callback/', 'status': 'ok', 'title': 'LoyalUP'}],
        )
        self.assertEqual(found, [])

    def test_foreign_broken_server_is_not_ours(self):
        # У части сетей в той же группе живёт callback Senler — чиним не мы.
        found = check_vk_callbacks(
            [('levone', 111, 'tok')],
            lambda gid, tok: [{'url': 'https://senler.ru/callback', 'status': 'failed', 'title': 'Senler'}],
        )
        self.assertEqual(found, [])

    def test_api_error_is_warn(self):
        def boom(gid, tok):
            raise RuntimeError('User authorization failed')

        found = check_vk_callbacks([('shavuha', 222, 'tok')], boom)
        self.assertEqual(found[0].key, 'vkcb:shavuha:222:error')
        self.assertEqual(found[0].severity, SEVERITY_WARN)

    def test_one_tenant_error_does_not_stop_others(self):
        def fetch(gid, tok):
            if gid == 222:
                raise RuntimeError('нет токена')
            return [{'url': 'https://a.levelupapp.ru/cb', 'status': 'unconfigured', 'title': 'LoyalUP'}]

        found = check_vk_callbacks([('shavuha', 222, 'tok'), ('levone', 111, 'tok')], fetch)
        self.assertEqual(set(_by_key(found)), {'vkcb:shavuha:222:error', 'vkcb:levone:111'})


class HealVkCallbacksTest(SimpleTestCase):
    """Самовосстановление: свежий код подтверждения и переподключение только своего сервера."""

    def _api(self, servers, code='abc12345'):
        calls = []

        def vk_call(method, **params):
            calls.append((method, params))
            if method == 'groups.getCallbackConfirmationCode':
                return {'code': code}
            if method == 'groups.getCallbackServers':
                return {'items': servers}
            return 1
        return vk_call, calls

    def test_failed_ours_is_reconnected_with_same_url_and_secret(self):
        vk_call, calls = self._api([{'id': 19, 'url': 'https://a.levelupapp.ru/api/v1/vk/callback/',
                                     'title': 'LoyalUP', 'status': 'failed', 'secret_key': 's3'}])
        synced = []
        out = heal_vk_callbacks([('a', 111, 'tok')], vk_call,
                                lambda s, g, c: synced.append((s, g, c)) or 2)
        edit = [p for m, p in calls if m == 'groups.editCallbackServer']
        self.assertEqual(len(edit), 1)
        self.assertEqual(edit[0]['server_id'], 19)
        self.assertEqual(edit[0]['url'], 'https://a.levelupapp.ru/api/v1/vk/callback/')
        self.assertEqual(edit[0]['secret_key'], 's3')
        self.assertEqual(synced, [('a', 111, 'abc12345')])
        self.assertEqual(out[0]['reconnected'], [19])

    def test_code_is_synced_before_reconnect(self):
        order = []
        vk_call, calls = self._api([{'id': 1, 'url': 'https://a.levelupapp.ru/cb', 'status': 'failed'}])

        def tracking(method, **params):
            order.append(method)
            return vk_call(method, **params)
        heal_vk_callbacks([('a', 111, 'tok')], tracking, lambda s, g, c: order.append('sync') or 1)
        self.assertLess(order.index('sync'), order.index('groups.editCallbackServer'))

    def test_ok_wait_and_foreign_servers_are_not_touched(self):
        vk_call, calls = self._api([
            {'id': 1, 'url': 'https://a.levelupapp.ru/cb', 'status': 'ok'},
            {'id': 2, 'url': 'https://a.levelupapp.ru/cb', 'status': 'wait'},
            {'id': 3, 'url': 'https://callback.senler.ru/x', 'status': 'failed'},
        ])
        heal_vk_callbacks([('a', 111, 'tok')], vk_call, lambda s, g, c: 0)
        self.assertFalse([m for m, _ in calls if m == 'groups.editCallbackServer'])

    def test_no_code_means_no_reconnect(self):
        vk_call, calls = self._api([{'id': 1, 'url': 'https://a.levelupapp.ru/cb', 'status': 'failed'}], code='')
        out = heal_vk_callbacks([('a', 111, 'tok')], vk_call, lambda s, g, c: 0)
        self.assertFalse([m for m, _ in calls if m == 'groups.editCallbackServer'])
        self.assertEqual(out[0]['skipped'], [1])

    def test_one_group_error_does_not_stop_others(self):
        def vk_call(method, **params):
            if params.get('group_id') == 222:
                raise RuntimeError('нет прав')
            if method == 'groups.getCallbackConfirmationCode':
                return {'code': 'c'}
            if method == 'groups.getCallbackServers':
                return {'items': [{'id': 5, 'url': 'https://b.levelupapp.ru/cb', 'status': 'failed'}]}
            return 1
        out = heal_vk_callbacks([('x', 222, 't'), ('b', 111, 't')], vk_call, lambda s, g, c: 0)
        by = {e['group_id']: e for e in out}
        self.assertIn('error', by[222])
        self.assertEqual(by[111]['reconnected'], [5])

    def test_nothing_to_do_leaves_no_journal_entry(self):
        vk_call, _ = self._api([{'id': 1, 'url': 'https://a.levelupapp.ru/cb', 'status': 'ok'}])
        self.assertEqual(heal_vk_callbacks([('a', 111, 'tok')], vk_call, lambda s, g, c: 0), [])

    def test_wait_status_is_not_an_alert(self):
        found = check_vk_callbacks(
            [('levone', 111, 'tok')],
            lambda gid, tok: [{'url': 'https://levone.levelupapp.ru/cb', 'status': 'wait', 'title': 'LoyalUP'}],
        )
        self.assertEqual(found, [])


# ── Доступность входа ─────────────────────────────────────────────────────────

class CheckProbesTest(SimpleTestCase):

    def test_200_is_silent(self):
        self.assertEqual(check_probes(['https://a.ru/'], lambda url: 200), [])

    def test_500_is_critical(self):
        found = check_probes(['https://a.ru/'], lambda url: 500)
        self.assertEqual(found[0].key, 'probe:https://a.ru/')
        self.assertEqual(found[0].severity, SEVERITY_CRITICAL)
        self.assertEqual(found[0].data['status'], 500)

    def test_exception_is_critical(self):
        def boom(url):
            raise OSError('connection refused')

        found = check_probes(['https://a.ru/'], boom)
        self.assertEqual(found[0].key, 'probe:https://a.ru/')
        self.assertEqual(found[0].severity, SEVERITY_CRITICAL)
        self.assertIn('connection refused', found[0].data['error'])


# ── Решение «будить или нет» (без БД) ─────────────────────────────────────────

class ShouldSendTest(SimpleTestCase):

    def setUp(self):
        self.now = timezone.now()
        self.warn = Finding('tls:a.ru', SEVERITY_WARN, 'TLS', 'body', {})
        self.crit = Finding('tls:a.ru', SEVERITY_CRITICAL, 'TLS', 'body', {})

    def test_new_signal_is_sent(self):
        self.assertTrue(should_send(None, self.warn, self.now, 24))

    def test_never_sent_state_is_sent(self):
        state = SimpleNamespace(severity=SEVERITY_WARN, last_sent_at=None)
        self.assertTrue(should_send(state, self.warn, self.now, 24))

    def test_inside_window_is_suppressed(self):
        state = SimpleNamespace(severity=SEVERITY_WARN, last_sent_at=self.now - timedelta(hours=3))
        self.assertFalse(should_send(state, self.warn, self.now, 24))

    def test_after_window_is_sent(self):
        state = SimpleNamespace(severity=SEVERITY_WARN, last_sent_at=self.now - timedelta(hours=25))
        self.assertTrue(should_send(state, self.warn, self.now, 24))

    def test_escalation_breaks_the_window(self):
        state = SimpleNamespace(severity=SEVERITY_WARN, last_sent_at=self.now - timedelta(minutes=5))
        self.assertTrue(should_send(state, self.crit, self.now, 24))

    def test_critical_repeat_inside_window_is_suppressed(self):
        state = SimpleNamespace(severity=SEVERITY_CRITICAL, last_sent_at=self.now - timedelta(hours=1))
        self.assertFalse(should_send(state, self.crit, self.now, 24))


# ── Доставка (БД + моки Expo) ─────────────────────────────────────────────────

class DeliverTest(TestCase):
    """
    MonitorAlertState живёт в public — тестовому прогону хватает обычной
    TestCase. Наружу (Expo, получатели) не ходим вообще.
    """

    def setUp(self):
        self.now = timezone.now()
        self.user = SimpleNamespace(id=1)

        patcher_recipients = patch(
            'apps.shared.monitoring.alerts._superadmin_recipients',
            return_value=([self.user], ['ExponentPushToken[test]']),
        )
        patcher_push = patch('apps.shared.monitoring.alerts.send_expo_push', return_value={'sent': 1})
        patcher_log = patch('apps.shared.monitoring.alerts.log_notification')
        self.recipients = patcher_recipients.start()
        self.push = patcher_push.start()
        self.log = patcher_log.start()
        self.addCleanup(patcher_recipients.stop)
        self.addCleanup(patcher_push.stop)
        self.addCleanup(patcher_log.stop)

    def _finding(self, key='tls:a.ru', severity=SEVERITY_WARN, title='TLS a.ru'):
        return Finding(key, severity, title, 'текст сигнала', {'host': 'a.ru'})

    def test_new_finding_is_stored_and_pushed(self):
        stats = deliver([self._finding()], self.now, 24)
        self.assertEqual(stats['new'], 1)
        self.assertEqual(stats['pushed'], 1)
        self.assertEqual(self.push.call_count, 1)
        self.assertEqual(self.log.call_count, 1)

        state = MonitorAlertState.objects.get(key='tls:a.ru')
        self.assertEqual(state.sent_count, 1)
        self.assertEqual(state.last_sent_at, self.now)
        self.assertIsNone(state.resolved_at)

    def test_repeat_inside_window_is_suppressed(self):
        deliver([self._finding()], self.now, 24)
        self.push.reset_mock()

        stats = deliver([self._finding()], self.now + timedelta(hours=2), 24)
        self.assertEqual(stats['suppressed'], 1)
        self.assertEqual(stats['pushed'], 0)
        self.assertEqual(self.push.call_count, 0)
        self.assertEqual(MonitorAlertState.objects.get(key='tls:a.ru').sent_count, 1)

    def test_repeat_after_window_is_pushed_again(self):
        deliver([self._finding()], self.now, 24)
        stats = deliver([self._finding()], self.now + timedelta(hours=25), 24)
        self.assertEqual(stats['repeated'], 1)
        self.assertEqual(stats['pushed'], 1)
        self.assertEqual(MonitorAlertState.objects.get(key='tls:a.ru').sent_count, 2)

    def test_escalation_pushes_inside_window(self):
        deliver([self._finding(severity=SEVERITY_WARN)], self.now, 24)
        self.push.reset_mock()

        stats = deliver([self._finding(severity=SEVERITY_CRITICAL)], self.now + timedelta(minutes=10), 24)
        self.assertEqual(stats['repeated'], 1)
        self.assertEqual(self.push.call_count, 1)
        self.assertEqual(MonitorAlertState.objects.get(key='tls:a.ru').severity, SEVERITY_CRITICAL)

    def test_disappeared_signal_is_resolved_and_announced(self):
        deliver([self._finding()], self.now, 24)
        self.push.reset_mock()

        stats = deliver([], self.now + timedelta(hours=1), 24)
        self.assertEqual(stats['resolved'], 1)
        self.assertEqual(stats['pushed'], 1)
        title = self.push.call_args.kwargs['title']
        self.assertIn('пришло в норму', title)
        self.assertIsNotNone(MonitorAlertState.objects.get(key='tls:a.ru').resolved_at)

    def test_resolved_without_push_history_stays_quiet(self):
        MonitorAlertState.objects.create(
            key='tls:silent.ru', severity=SEVERITY_WARN, title='TLS silent.ru',
            body='', data={}, last_seen_at=self.now, last_sent_at=None,
        )
        stats = deliver([], self.now + timedelta(hours=1), 24)
        self.assertEqual(stats['resolved'], 1)
        self.assertEqual(stats['pushed'], 0)
        self.assertEqual(self.push.call_count, 0)

    def test_returning_signal_reopens_state(self):
        deliver([self._finding()], self.now, 24)
        deliver([], self.now + timedelta(hours=1), 24)
        deliver([self._finding()], self.now + timedelta(hours=2), 24)
        self.assertIsNone(MonitorAlertState.objects.get(key='tls:a.ru').resolved_at)

    def test_more_than_three_findings_fold_into_one_push(self):
        findings = [self._finding(key=f'probe:{i}', title=f'Сигнал {i}') for i in range(5)]
        stats = deliver(findings, self.now, 24)
        self.assertEqual(stats['new'], 5)
        self.assertEqual(stats['pushed'], 1)
        self.assertEqual(self.push.call_count, 1)
        body = self.push.call_args.kwargs['body']
        self.assertIn('Сигнал 0', body)
        self.assertIn('Сигнал 4', body)
        # но отметку об отправке получают все пять
        self.assertEqual(MonitorAlertState.objects.filter(sent_count=1).count(), 5)

    def test_three_findings_are_sent_separately(self):
        findings = [self._finding(key=f'probe:{i}', title=f'Сигнал {i}') for i in range(3)]
        stats = deliver(findings, self.now, 24)
        self.assertEqual(stats['pushed'], 3)
        self.assertEqual(self.push.call_count, 3)
