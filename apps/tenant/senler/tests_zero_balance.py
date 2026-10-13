"""
Ветка «при нулевом балансе» авторассылок (ТЗ маркетолога 14.10.2026, п.7).

Тенантных таблиц в тестовой БД нет — как в tests_auto_broadcasts_api.py, ORM
подменяется; движок проверяется через run_rule с подменёнными зависимостями.

Правила:
  1) баланс > 0 — основной текст, ≤ 0 — текст ветки; ровно одно сообщение;
  2) баланс берётся прямо перед отправкой; не посчитался — гость пропущен
     до следующего тика (отсутствие данных ≠ 0), ничего не записано;
  3) в журнал пишется ветка и баланс;
  4) без текста ветки — всё как раньше, ветка пустая;
  5) кабинет: {баланс} в тексте ветки — 400; включить правило с {баланс} без
     текста ветки — 400; «тест себе» шлёт выбранную версию;
  6) миграция находит пять правил по сегменту и сроку.
"""
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.tenant.senler import engine
from apps.tenant.senler.api import auto_broadcasts as ab
from apps.tenant.senler.tests_auto_broadcasts_api import _AutoBroadcastCase, _call, _FakeCache, _qs, AB

E = 'apps.tenant.senler.engine.'
MAIN = 'У вас {баланс} б. Ждём: {адреса}'
ZERO = 'Покрутите колесо фортуны! Ждём: {адреса}'


def _engine_rule(zero=ZERO):
    rule = MagicMock()
    rule.pk = 11
    rule.event = 'no_visit_days'
    rule.message_text = MAIN
    rule.zero_balance_text = zero
    rule.image = None
    rule.variants.all.return_value = []
    return rule


class RunRuleBranchTest(SimpleTestCase):
    """run_rule с одним кандидатом; модели и ВК подменены."""

    def setUp(self):
        self.cb = SimpleNamespace(pk=5, client=SimpleNamespace(first_name='Лев'),
                                  branch=SimpleNamespace(senler_config=SimpleNamespace(pk=1, is_active=True)))
        self.cand = engine.Candidate(client_branch=self.cb, vk_id=42)
        self.sent_texts = []
        self.recipients = []
        models = MagicMock()
        models.BroadcastRecipient.objects.create.side_effect = lambda **kw: self.recipients.append(kw)
        patches = [
            patch(E + 'rule_is_due', return_value=(True, 'ok')),
            patch(E + 'get_events', return_value={}),
            patch(E + 'resolve_recipients', return_value=[self.cand]),
            patch(E + 'tenant_addresses', return_value='Ленина 1'),
            patch(E + 'time', create=True),
            patch('apps.tenant.senler.models.AutoBroadcastLog', models.AutoBroadcastLog),
            patch('apps.tenant.senler.models.BroadcastRecipient', models.BroadcastRecipient),
            patch('apps.tenant.senler.models.BroadcastSend', models.BroadcastSend),
            patch('apps.tenant.senler.services.send_vk_message', side_effect=self._send),
            patch('time.sleep'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _send(self, cfg, vk_id, text, attachment, keyboard=None):
        self.sent_texts.append(text)
        return True, '', 900

    def _run(self, balance, zero=ZERO):
        with patch(E + '_coin_balance', side_effect=balance if callable(balance) else lambda cb: balance):
            return engine.run_rule(_engine_rule(zero))

    def test_положительный_баланс_основной_текст(self):
        self._run(120)
        self.assertEqual(self.sent_texts, ['У вас 120 б. Ждём: Ленина 1'])
        self.assertEqual(self.recipients[0]['message_branch'], 'main')
        self.assertEqual(self.recipients[0]['balance_at_send'], 120)

    def test_ноль_текст_ветки_и_одно_сообщение(self):
        self._run(0)
        self.assertEqual(self.sent_texts, ['Покрутите колесо фортуны! Ждём: Ленина 1'])
        self.assertEqual(self.recipients[0]['message_branch'], 'zero_balance')
        self.assertEqual(self.recipients[0]['balance_at_send'], 0)

    def test_минус_тоже_ноль(self):
        self._run(-5)
        self.assertEqual(self.recipients[0]['message_branch'], 'zero_balance')

    def test_баланс_не_посчитался_гость_ждёт_тика(self):
        def boom(cb):
            raise RuntimeError('db')
        result = self._run(boom)
        self.assertEqual(self.sent_texts, [])
        self.assertEqual(self.recipients, [])
        self.assertEqual(result['sent'], 0)

    def test_без_текста_ветки_как_раньше(self):
        self._run(0, zero='')
        self.assertEqual(self.sent_texts, ['У вас 0 б. Ждём: Ленина 1'])
        self.assertEqual(self.recipients[0]['message_branch'], '')
        self.assertIsNone(self.recipients[0]['balance_at_send'])


class CabinetZeroBalanceTest(_AutoBroadcastCase):

    def test_баланс_в_тексте_ветки_запрещён(self):
        resp = _call(ab.AutoBroadcastRuleDetailAPIView, 'patch', '/api/v1/auto-broadcasts/1/',
                     {'zero_balance_text': 'У вас {баланс} б.'}, pk=1)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'zero_balance_has_balance')

    def test_текст_ветки_сохраняется_и_в_карточке(self):
        resp = _call(ab.AutoBroadcastRuleDetailAPIView, 'patch', '/api/v1/auto-broadcasts/1/',
                     {'zero_balance_text': ZERO}, pk=1)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self.rule.zero_balance_text, ZERO)
        self.assertEqual(resp.data['zero_balance_text'], ZERO)

    def test_включить_с_балансом_без_ветки_нельзя(self):
        self.rule.message_text = MAIN
        resp = _call(ab.AutoBroadcastRuleActivateAPIView, 'post', '/api/v1/auto-broadcasts/1/activate/',
                     {'expected_count': 1, 'confirm': True}, pk=1)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'zero_balance_text_required')
        self.assertIs(self.rule.is_active, False)

    def test_включить_с_веткой_можно(self):
        self.rule.message_text = MAIN
        self.rule.zero_balance_text = ZERO
        self.mocks['resolve_recipients'].return_value = [
            SimpleNamespace(client_branch=SimpleNamespace(branch=SimpleNamespace(pk=1, name='Т')), vk_id=1)]
        resp = _call(ab.AutoBroadcastRuleActivateAPIView, 'post', '/api/v1/auto-broadcasts/1/activate/',
                     {'expected_count': 1, 'confirm': True}, pk=1)
        self.assertEqual(resp.status_code, 200, resp.data)


class TestSendBranchTest(_AutoBroadcastCase):

    def setUp(self):
        super().setUp()
        p = patch(AB + 'cache', _FakeCache())
        p.start()
        self.addCleanup(p.stop)
        cb = SimpleNamespace(pk=11, branch=SimpleNamespace(pk=1, name='Т', senler_config=SimpleNamespace(is_active=True)),
                             client=SimpleNamespace(first_name='Лев', vk_id=42))
        self.mocks['ClientBranch'].objects.filter.return_value = _qs([cb])

    def _send(self, branch):
        return _call(ab.AutoBroadcastRuleTestSendAPIView, 'post', '/api/v1/auto-broadcasts/1/test-send/',
                     {'vk_id': 42, 'balance_branch': branch}, pk=1)

    def test_тест_себе_версия_при_нуле(self):
        self.rule.zero_balance_text = ZERO
        self.assertEqual(self._send('zero_balance').status_code, 200)
        kwargs = self.mocks['render_text'].call_args.kwargs
        self.assertEqual(kwargs.get('template_override'), ZERO)

    def test_тест_себе_без_текста_ветки(self):
        resp = self._send('zero_balance')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'no_zero_balance_text')
        self.mocks['send_vk_message'].assert_not_called()


class MigrationTextsTest(SimpleTestCase):

    def test_пять_правил_и_без_баланса_в_текстах(self):
        mig = import_module('apps.tenant.senler.migrations.0017_fill_zero_balance_texts')
        self.assertEqual(set(mig.TEXTS), {('R3F1', 7), ('R3F2', 10), ('R3F3', 12), ('R2F1', 16), ('R2F2', 17)})
        for text in mig.TEXTS.values():
            self.assertNotIn('{баланс}', text)
            self.assertTrue(text.endswith('{адреса}'))
