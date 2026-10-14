"""
Статистика кампаний и RF-переходы (волна 2, 14.10.2026) — чистая часть без БД.

Правила:
  1) возврат засчитывается последней отправке до события и только в окне 14 дней;
  2) двух кампаний на одно событие не бывает;
  3) чек и заказ по карте одного дня — одна покупка (большая сумма);
  4) деньги без покрытия — «нет данных» (None), а не 0;
  5) «+N ₽» — только при достаточном покрытии в обеих группах;
  6) RF: гость считается один раз — от сегмента до первого перехода к последнему.
"""
from datetime import datetime, timedelta, timezone as dtz
from decimal import Decimal

from django.test import SimpleTestCase

from apps.tenant.analytics import campaign_stats as cs
from apps.tenant.analytics import rf_transitions as rt

T0 = datetime(2026, 10, 1, 12, tzinfo=dtz.utc)


def d(days, hours=0):
    return T0 + timedelta(days=days, hours=hours)


class AttributionTest(SimpleTestCase):

    def test_последняя_отправка_и_окно(self):
        sends = [(1, d(0), 'A'), (1, d(3), 'B'), (2, d(0), 'A')]
        visits = [(1, d(4)), (2, d(20))]
        g = cs.attribute(sends, visits, [], set())
        self.assertEqual(g['B'].returned, {1})
        self.assertEqual(g['A'].returned, set())          # гость 2 вернулся после окна
        self.assertEqual(len(g['A'].guests), 2)

    def test_до_отправки_не_считается(self):
        g = cs.attribute([(1, d(5), 'A')], [(1, d(4))], [], set())
        self.assertEqual(g['A'].returned, set())

    def test_покупка_одного_дня_одна(self):
        p = cs.daily_purchases([(1, d(1), Decimal('500'))], [(1, d(1, 2), Decimal('700')), (1, d(2), Decimal('100'))])
        self.assertEqual(sorted(a for _, _, a in p), [Decimal('100'), Decimal('700')])

    def test_деньги_без_покрытия_нет_данных(self):
        g = cs.attribute([(1, d(0), 'A')], [], [], set())
        self.assertIsNone(cs.group_dict(g['A'])['revenue'])

    def test_контроль_и_плюс_рубли(self):
        sent = [(i, d(0), 'A') for i in range(20)]
        ctrl = [(100 + i, d(0)) for i in range(20)]
        covered = set(range(20)) | {100 + i for i in range(20)}
        purchases = [(i, d(2), Decimal('1000')) for i in range(10)] + [(100 + i, d(2), Decimal('1000')) for i in range(4)]
        g = cs.attribute(sent, [], purchases, covered)['A']
        c = cs.control_group(ctrl, [], purchases, covered)
        out = cs.compare(g, c)
        self.assertEqual((g.rate(), c.rate(), out['uplift_pp']), (50.0, 20.0, 30.0))
        self.assertEqual(out['extra_returns'], 6)
        self.assertEqual(out['extra_revenue'], 6000.0)   # (500 − 200) ₽ на гостя × 20

    def test_мало_покрытия_рубли_не_считаем(self):
        g = cs.attribute([(1, d(0), 'A')], [], [(1, d(1), Decimal('900'))], {1})['A']
        c = cs.control_group([(2, d(0))], [], [], {2})
        out = cs.compare(g, c)
        self.assertIsNone(out['extra_revenue'])
        self.assertEqual(out['uplift_pp'], 100.0)


class RFTransitionsTest(SimpleTestCase):

    def test_классы(self):
        paths = {1: ('R1F1', 'R3F2'), 2: ('R3F3', 'R1F1'), 3: ('', 'R1F1'), 4: ('R2F1', 'R1F2')}
        out = rt.classify(paths, scored_total=10)
        self.assertEqual((out['warmed'], out['cooled'], out['new'], out['unchanged']), (1, 1, 1, 7))
        self.assertIn('поровну', out['conclusion'])

    def test_остыло_больше(self):
        out = rt.classify({1: ('R3F3', 'R1F1'), 2: ('R3F2', 'R1F1')}, 5)
        self.assertIn('Остыло больше', out['conclusion'])

    def test_пусто(self):
        self.assertIn('не переходили', rt.classify({}, 0)['conclusion'])
