"""
Скан QR фискального чека (волна 2, 14.10.2026) — `receipts.py`.

Тенантных таблиц в тестовой базе нет: модели подменены там, где их
импортирует `process_receipt`.

Правила:
  1) строка QR разбирается в оба формата времени, сумма — в копейках;
  2) не чек, возврат, будущее время, старый чек — отказ с кодом;
  3) незнакомый ФН — строка без баллов, повтор тем гостем после добавления ФН засчитывается;
  4) чек другого гостя и свой засчитанный — 409;
  5) суточный лимит;
  6) баллы — процент вниз, на счёт точки ФН; 0 % — чек засчитан без транзакции.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from apps.tenant.branch import receipts as rc

QR = 't=20261014T1530&s=1250.50&fn=7380440700123456&i=12345&fp=1234567890&n=1'


def _now():
    return timezone.make_aware(datetime(2026, 10, 14, 18, 0), timezone.get_current_timezone())


class ParseTest(SimpleTestCase):

    def test_разбор(self):
        r = rc.parse_qr(QR)
        self.assertEqual((r.fn, r.fd, r.fp, r.kind), ('7380440700123456', '12345', '1234567890', '1'))
        self.assertEqual(r.amount, Decimal('1250.50'))
        self.assertEqual((r.when.hour, r.when.minute), (15, 30))

    def test_время_с_секундами(self):
        self.assertEqual(rc.parse_qr(QR.replace('T1530', 'T153012')).when.second, 12)

    def test_не_чек(self):
        for raw in ('', 'https://vk.com', 't=x&s=1&fn=1&i=1', QR.replace('s=1250.50', 's=0')):
            with self.assertRaises(rc.ReceiptError) as e:
                rc.parse_qr(raw)
            self.assertEqual(e.exception.code, 'qr_invalid')

    def test_фн_точки(self):
        self.assertEqual(rc.branch_fns('7380440700123456,\n9960440300000001 x 123'),
                         {'7380440700123456', '9960440300000001'})

    def test_баллы_вниз(self):
        self.assertEqual(rc.coins_for(Decimal('1250.50'), 5), 62)
        self.assertEqual(rc.coins_for(Decimal('1250.50'), 0), 0)


class ProcessTest(SimpleTestCase):

    def setUp(self):
        self.rows = {}
        self.created = []
        self.transfers = []
        scans = MagicMock()

        def filter_(**kw):
            q = MagicMock()
            if 'fn' in kw:
                q.first.return_value = self.rows.get((kw['fn'], kw['fd']))
            else:
                q.count.return_value = self.today
            return q
        scans.objects.filter.side_effect = filter_
        scans.objects.create.side_effect = lambda **kw: self.created.append(kw)
        scans.objects.select_for_update.return_value.get.side_effect = lambda pk: self.rows_by_pk[pk]

        def make(**kw):
            obj = SimpleNamespace(**kw, save=lambda: self.created.append(kw))
            return obj
        scans.side_effect = make
        self.today = 0
        self.rows_by_pk = {}
        cb = MagicMock()
        cb.objects.get_or_create.return_value = ('CB', True)
        tx = MagicMock()
        tx.objects.create_transfer.side_effect = lambda *a, **kw: self.transfers.append(a)
        for name, val in (('ReceiptScan', scans), ('ClientBranch', cb), ('CoinTransaction', tx)):
            p = patch(f'apps.tenant.branch.models.{name}', val)
            p.start()
            self.addCleanup(p.stop)
        ta = patch.object(rc.transaction, 'atomic', MagicMock())
        ta.start()
        self.addCleanup(ta.stop)
        self.branch = SimpleNamespace(branch_id=3, name='Ленина')
        fb = patch.object(rc, 'find_branch', side_effect=lambda fn: self.branch)
        fb.start()
        self.addCleanup(fb.stop)
        self.client_obj = SimpleNamespace(pk=7)
        self.cfg = SimpleNamespace(receipt_max_age_hours=48, receipt_daily_limit=3, receipt_cashback_percent=5)

    def _run(self, raw=QR, now=None):
        return rc.process_receipt(self.client_obj, raw, self.cfg, now=now or _now())

    def _code(self, raw=QR, now=None):
        with self.assertRaises(rc.ReceiptError) as e:
            self._run(raw, now)
        return e.exception.code

    def test_засчитан_баллы_на_точку(self):
        out = self._run()
        self.assertEqual((out['status'], out['coins'], out['branch']['id']), ('credited', 62, 3))
        self.assertEqual(self.transfers[0][:2], ('CB', 62))

    def test_ноль_процентов_без_транзакции(self):
        self.cfg.receipt_cashback_percent = 0
        self.assertEqual(self._run()['coins'], 0)
        self.assertEqual(self.transfers, [])

    def test_возврат_будущее_старый(self):
        self.assertEqual(self._code(QR.replace('n=1', 'n=2')), 'not_sale')
        self.assertEqual(self._code(now=_now() - timedelta(hours=3)), 'qr_invalid')
        self.assertEqual(self._code(now=_now() + timedelta(days=3)), 'too_old')

    def test_незнакомый_фн_без_баллов(self):
        self.branch = None
        out = self._run()
        self.assertEqual((out['status'], out['coins']), ('unknown_fn', 0))
        self.assertEqual(self.created[0]['status'], 'unknown_fn')
        self.assertEqual(self.transfers, [])

    def test_чужой_и_свой_повтор(self):
        self.rows[('7380440700123456', '12345')] = SimpleNamespace(client_id=8, status='credited')
        self.assertEqual(self._code(), 'duplicate')
        self.rows[('7380440700123456', '12345')] = SimpleNamespace(client_id=7, status='credited')
        self.assertEqual(self._code(), 'already_yours')

    def test_после_добавления_фн_засчитывается(self):
        row = SimpleNamespace(pk=1, client_id=7, status='unknown_fn', save=MagicMock())
        self.rows[('7380440700123456', '12345')] = row
        self.rows_by_pk[1] = row
        out = self._run()
        self.assertEqual((out['status'], row.status, row.coins), ('credited', 'credited', 62))
        row.save.assert_called_once()

    def test_суточный_лимит(self):
        self.today = 3
        self.assertEqual(self._code(), 'daily_limit')
