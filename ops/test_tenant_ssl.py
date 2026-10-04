"""Offline safety regressions. Never access a real database, CA, DNS, or nginx."""
import copy
import json
from pathlib import Path
import subprocess
import unittest

from tenant_ssl import WEEK, plan, reconcile

CONFIG = json.loads(Path(__file__).with_name('tenant_ssl.example.json').read_text())
BASE = {'levelupapp.ru', 'www.levelupapp.ru', 'vkapp.levelupapp.ru', 'levone.levelupapp.ru'}
NEW = 'new-client.levelupapp.ru'


class FakeRuntime:
    def __init__(self, domains=(NEW,)):
        self.current = set(BASE)
        self.tenant_domains = list(domains)
        self.addresses = {NEW: {'201.24.51.207'}}
        self.events = []
        self.saved = {}
        self.failure = None
        self.bad_certificate = False
        self.reload_failure = False

    def names(self):
        return set(self.current)

    def domains(self):
        return self.tenant_domains

    def resolve(self, domain):
        value = self.addresses.get(domain, set())
        if isinstance(value, Exception):
            raise value
        return value

    def save(self, state):
        self.saved = copy.deepcopy(state)

    def nginx_check(self):
        self.events.append('check')

    def backup(self):
        self.events.append('backup')

    def issue(self, names):
        self.events.append(('issue', set(names)))
        if self.failure:
            raise self.failure
        self.current = set(names)
        if self.bad_certificate:
            self.current.remove('vkapp.levelupapp.ru')

    def reload(self):
        self.events.append('reload')
        if self.reload_failure:
            raise RuntimeError('nginx reload failed')


class TenantSSLTest(unittest.TestCase):
    def test_new_client_retains_every_previous_name_including_non_tenant_names(self):
        runtime, state = FakeRuntime(), {}
        result = reconcile(CONFIG, state, runtime, 1000000)
        self.assertTrue(result['issued'])
        self.assertEqual(runtime.current, BASE | {NEW})
        self.assertEqual(runtime.events, ['check', 'backup', ('issue', BASE | {NEW}), 'reload'])
        self.assertNotIn('pending_reload', state)

    def test_second_run_does_not_issue_or_reload(self):
        runtime, state = FakeRuntime(), {}
        reconcile(CONFIG, state, runtime, 1000000)
        runtime.events.clear()
        reconcile(CONFIG, state, runtime, 1000060)
        self.assertEqual(runtime.events, [])

    def test_check_mode_only_reports_plan(self):
        runtime, state = FakeRuntime(), {}
        result = reconcile(CONFIG, state, runtime, 1000000, check_only=True)
        self.assertEqual(result['add'], [NEW])
        self.assertEqual(runtime.events, [])
        self.assertEqual(state, {})

    def test_waits_for_dns_and_then_adds_persisted_domain(self):
        runtime, state = FakeRuntime(), {}
        runtime.addresses[NEW] = OSError('DNS propagation')
        result = reconcile(CONFIG, state, runtime, 1000000)
        self.assertEqual(result['waiting_dns'], [NEW])
        self.assertEqual(runtime.events, [])
        runtime.addresses[NEW] = {'201.24.51.207'}
        self.assertTrue(reconcile(CONFIG, state, runtime, 1000060)['issued'])

    def test_external_server_and_mixed_ipv6_addresses_wait(self):
        for addresses in ({'46.243.211.179'}, {'201.24.51.207', '2001:db8::1'}):
            runtime = FakeRuntime()
            runtime.addresses[NEW] = addresses
            result = reconcile(CONFIG, {}, runtime, 1000000)
            self.assertEqual(result['waiting_dns'], [NEW])
            self.assertEqual(runtime.events, [])

    def test_excluded_custom_nested_and_malformed_domains_are_never_requested(self):
        domains = ['api-ya.levelupapp.ru', 'elsewhere.example', 'nested.host.levelupapp.ru',
                   '-bad.levelupapp.ru', 'bad-.levelupapp.ru', 'x;id.levelupapp.ru']
        required, waiting = plan(BASE, domains, CONFIG, lambda domain: {'201.24.51.207'})
        self.assertEqual(required, BASE)
        self.assertEqual(waiting, [])

    def test_certificate_failure_preserves_service_and_retries_after_backoff(self):
        runtime, state = FakeRuntime(), {}
        runtime.failure = subprocess.CalledProcessError(1, ['certbot'])
        with self.assertRaises(subprocess.CalledProcessError):
            reconcile(CONFIG, state, runtime, 1000000)
        self.assertEqual(runtime.current, BASE)
        self.assertNotIn('reload', runtime.events)
        runtime.failure = None
        runtime.events.clear()
        self.assertIn('deferred', reconcile(CONFIG, state, runtime, 1000060))
        self.assertEqual(runtime.events, [])
        self.assertTrue(reconcile(CONFIG, state, runtime, 1003600)['issued'])

    def test_missing_previous_name_prevents_nginx_reload(self):
        runtime, state = FakeRuntime(), {}
        runtime.bad_certificate = True
        with self.assertRaisesRegex(RuntimeError, 'lost required names'):
            reconcile(CONFIG, state, runtime, 1000000)
        self.assertNotIn('reload', runtime.events)
        runtime.events.clear()
        with self.assertRaisesRegex(RuntimeError, 'lost previous names'):
            reconcile(CONFIG, state, runtime, 1000060)
        self.assertEqual(runtime.events, [])

    def test_failed_reload_is_recovered_without_another_certificate_order(self):
        runtime, state = FakeRuntime(), {}
        runtime.reload_failure = True
        with self.assertRaises(RuntimeError):
            reconcile(CONFIG, state, runtime, 1000000)
        self.assertIn('pending_reload', state)
        runtime.reload_failure = False
        runtime.events.clear()
        reconcile(CONFIG, state, runtime, 1000060)
        self.assertEqual(runtime.events, ['reload'])
        self.assertNotIn('pending_reload', state)

    def test_crash_after_issuance_reloads_certificate_without_ordering_again(self):
        runtime = FakeRuntime()
        runtime.current.add(NEW)
        state = {'pending_reload': sorted(BASE | {NEW}), 'previous_names': sorted(BASE),
                 'retry_after': 1003600, 'attempts': [1000000]}
        reconcile(CONFIG, state, runtime, 1000060)
        self.assertEqual(runtime.events, ['reload'])
        self.assertNotIn('pending_reload', state)

    def test_cooldown_batches_additional_clients_without_dropping_names(self):
        runtime, state = FakeRuntime(), {}
        reconcile(CONFIG, state, runtime, 1000000)
        second = 'second-client.levelupapp.ru'
        runtime.tenant_domains.append(second)
        runtime.addresses[second] = {'201.24.51.207'}
        runtime.events.clear()
        self.assertIn('deferred', reconcile(CONFIG, state, runtime, 1000060))
        self.assertEqual(runtime.events, [])
        self.assertTrue(reconcile(CONFIG, state, runtime, 1000900)['issued'])
        self.assertEqual(runtime.current, BASE | {NEW, second})

    def test_weekly_budget_expires_without_forgetting_existing_names(self):
        runtime = FakeRuntime()
        state = {'attempts': [1000000] * CONFIG['weekly_attempt_limit']}
        self.assertIn('deferred', reconcile(CONFIG, state, runtime, 1000060))
        self.assertEqual(runtime.events, [])
        self.assertTrue(reconcile(CONFIG, state, runtime, 1000000 + WEEK + 1)['issued'])

    def test_san_capacity_limit_does_not_remove_domains_or_order_certificate(self):
        runtime = FakeRuntime()
        runtime.current = {f'client{i}.levelupapp.ru' for i in range(100)}
        with self.assertRaisesRegex(RuntimeError, 'SAN limit'):
            reconcile(CONFIG, {}, runtime, 1000000)
        self.assertEqual(runtime.events, [])


if __name__ == '__main__':
    unittest.main()
