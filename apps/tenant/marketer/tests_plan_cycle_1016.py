"""16.10.2026 — контент-план: цикл «Неделя / Месяц» (ТЗ 15.10.2026, п.1)."""
from datetime import date

from django.test import SimpleTestCase
from django.utils import timezone

from apps.tenant.marketer.api.views import cycle_days, plan_slots_cycle


class ЦиклПлана(SimpleTestCase):

    def test_неделя_семь_дней_месяц_до_той_же_даты(self):
        self.assertEqual(cycle_days(date(2026, 10, 20), 'week'), 7)
        self.assertEqual(cycle_days(date(2026, 10, 20), 'month'), 31)    # 20.10 → 19.11
        self.assertEqual(cycle_days(date(2026, 1, 31), 'month'), 28)     # 31.01 → 27.02
        self.assertEqual(cycle_days(date(2026, 12, 5), 'month'), 31)     # через год

    def test_число_постов_за_цикл_и_пропорция_неполного(self):
        старт = date(2026, 10, 20)
        месяц = plan_slots_cycle(старт, date(2026, 11, 19), 'month', 12, 12)
        self.assertEqual(len(месяц), 12)
        неделя = plan_slots_cycle(старт, date(2026, 10, 26), 'week', 3, 12)
        self.assertEqual(len(неделя), 3)
        полторы = plan_slots_cycle(старт, date(2026, 10, 30), 'week', 3, 12)   # 11 дней → 4,7
        self.assertEqual(len(полторы), 5)
        дни = [timezone.localtime(s).date() for s in полторы]
        self.assertEqual(len(set(дни)), len(дни), 'второй пост на день не ставим')
        self.assertTrue(all(старт <= d <= date(2026, 10, 30) for d in дни))
        self.assertTrue(all(timezone.localtime(s).hour == 12 for s in полторы))
