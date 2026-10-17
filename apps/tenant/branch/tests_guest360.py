"""
Карточка гостя 360 (18.10.2026). Держим:
  1. прогноз: меньше трёх визитов — честное «мало данных»; долгое отсутствие —
     «остывает», риск растёт, окно следующего визита после последнего;
  2. «что сделать сейчас»: ждёт ответа важнее всего; ДР скоро и автопоздравление
     выключено — подарок; остывает — подарок, тот, что уже возвращал;
  3. поводы: ДР в ближайшую дату, повод из заметки с датой, сортировка по близости.
"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from apps.tenant.branch import guest360 as G

СЕГОДНЯ = date(2026, 10, 18)


class Прогноз(SimpleTestCase):
    def test_мало_визитов(self):
        with mock.patch.object(G, '_дни_визитов', return_value=[date(2026, 10, 1)]):
            self.assertFalse(G.прогноз([1], сегодня=СЕГОДНЯ)['enough'])

    def test_остывает_и_окно(self):
        дни = [date(2026, 8, 1) + timedelta(days=9 * i) for i in range(6)]   # каждые 9 дней, последний 15.09
        with mock.patch.object(G, '_дни_визитов', return_value=дни):
            p = G.прогноз([1], средний_чек=1500, сегодня=СЕГОДНЯ)
        self.assertEqual(p['usual_gap_days'], 9)
        self.assertEqual(p['days_since'], 33)
        self.assertTrue(p['cooling'])
        self.assertGreaterEqual(p['churn_risk_pct'], 90)
        self.assertEqual(p['next_visit_from'], '2026-09-22')
        self.assertGreater(p['overdue_days'], 0)
        self.assertIsNotNone(p['year_value_rub'])

    def test_каждодневный_не_остывает_за_пять_дней(self):
        дни = [СЕГОДНЯ - timedelta(days=5 + i) for i in range(20, -1, -1)]
        with mock.patch.object(G, '_дни_визитов', return_value=дни):
            p = G.прогноз([1], сегодня=СЕГОДНЯ)
        self.assertEqual(p['usual_gap_days'], 3)
        self.assertFalse(p['cooling'])

    def test_ходит_как_обычно(self):
        дни = [СЕГОДНЯ - timedelta(days=7 * i) for i in range(5, 0, -1)]
        with mock.patch.object(G, '_дни_визитов', return_value=дни):
            p = G.прогноз([1], сегодня=СЕГОДНЯ)
        self.assertFalse(p['cooling'])
        self.assertLessEqual(p['churn_risk_pct'], 20)


class ЧтоСделать(SimpleTestCase):
    остыл = {'enough': True, 'cooling': True, 'visits': 6, 'days_since': 33, 'usual_gap_days': 9, 'churn_risk_pct': 90}

    def test_ждёт_ответа_важнее(self):
        a = G.что_сделать(ждёт={'hours': 2.5, 'text': 'Зал свободен?'}, прогноз_=self.остыл, поводы_=[],
                          канал='tg', подарок=None)
        self.assertEqual(a['action'], 'reply')

    def test_др_без_автопоздравления(self):
        a = G.что_сделать(ждёт=None, прогноз_=None, канал='vk', подарок=None,
                          поводы_=[{'kind': 'birthday', 'in_days': 3, 'auto': False}])
        self.assertEqual(a['action'], 'gift')
        self.assertIn('День рождения', a['title'])
        self.assertIsNone(G.что_сделать(ждёт=None, прогноз_=None, канал='vk', подарок=None,
                                        поводы_=[{'kind': 'birthday', 'in_days': 3, 'auto': True}]))

    def test_остывает_с_подарком_который_возвращал(self):
        a = G.что_сделать(ждёт=None, прогноз_=self.остыл, поводы_=[], канал='tg',
                          подарок={'title': 'Десерт', 'catalog_item_id': 5})
        self.assertEqual(a['action'], 'gift')
        self.assertIn('Десерт', a['title'])
        self.assertIn('уже возвращал', a['why'])


class Поводы(SimpleTestCase):
    def test_др_и_повод_из_заметки(self):
        cbs = [SimpleNamespace(birth_date=date(1988, 10, 25), created_at=datetime(2026, 9, 1))]
        заметки = [{'id': 1, 'kind': 'occasion', 'text': 'Годовщина свадьбы', 'occasion_date': '2020-11-18'}]
        with mock.patch('apps.tenant.senler.models.AutoBroadcastTemplate') as T, \
                mock.patch.object(G.timezone, 'localtime', side_effect=lambda d: d):
            T.objects.filter.return_value.exists.return_value = True
            out = G.поводы(SimpleNamespace(), cbs, заметки, сегодня=СЕГОДНЯ)
        self.assertEqual([p['kind'] for p in out], ['birthday', 'note'])
        self.assertEqual(out[0]['in_days'], 7)
        self.assertTrue(out[0]['auto'])
        self.assertEqual(out[1]['date'], '2026-11-18')
