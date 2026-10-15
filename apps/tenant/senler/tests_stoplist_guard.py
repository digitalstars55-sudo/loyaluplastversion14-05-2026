"""Стоп-лист перед рассылкой (stoplist_guard.py), 15.10.2026."""
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.tenant.senler import stoplist_guard as sg

ITEMS = [{'name': 'Сырники со сметаной 180 г', 'branch': 'Ленина'},
         {'name': 'Пицца Маргарита', 'branch': 'Ленина'},
         {'name': 'Соус сырный', 'branch': 'Ленина'}]


class FindConflictsTest(SimpleTestCase):
    def test_word_forms_found(self):
        got = sg.find_conflicts(['Только сегодня скидка на сырников и десерты!'], ITEMS)
        self.assertEqual([c['name'] for c in got], ['Сырники со сметаной 180 г'])

    def test_generic_word_skipped(self):
        # «соус» не значимое слово: блюдо узнаётся по «сырный», а не по «соус»
        self.assertEqual(sg.find_conflicts(['Новый соус к бургерам'], ITEMS), [])

    def test_no_mention(self):
        self.assertEqual(sg.find_conflicts(['Ждём вас на завтрак'], ITEMS), [])

    def test_variant_texts_checked(self):
        got = sg.find_conflicts(['Вариант А без еды', 'Вариант Б: пицца в подарок'], ITEMS)
        self.assertEqual([c['name'] for c in got], ['Пицца Маргарита'])


@override_settings(LOYALUP_RELAY_SECRET='s', CHECKUP_COMPLAINTS_URL='https://checkupapp.ru/api/v1/loyalup/complaints/inbound/')
class FetchTest(SimpleTestCase):
    def test_url_and_secret(self):
        self.assertEqual(sg._url(), 'https://checkupapp.ru/api/v1/loyalup/stoplist/')
        resp = SimpleNamespace(status_code=200, json=lambda: {'items': ITEMS})
        with mock.patch('requests.Session.get', return_value=resp) as get:
            self.assertEqual(len(sg.fetch_stoplist('levone', '1')), 3)
        self.assertEqual(get.call_args.kwargs['headers']['X-LoyalUP-Relay-Secret'], 's')
        self.assertEqual(get.call_args.kwargs['params'], {'tenant': 'levone', 'point_id': '1'})

    def test_checkup_down_does_not_block(self):
        with mock.patch('requests.Session.get', side_effect=OSError('down')):
            self.assertIsNone(sg.fetch_stoplist('levone'))
        with mock.patch('requests.Session.get', side_effect=OSError('down')), \
                mock.patch('apps.tenant.branch.models.Branch.objects') as objs:
            objs.filter.return_value.values_list.return_value = [1]
            self.assertEqual(sg.check('levone', ['сырники'], [5]), [])

    @override_settings(LOYALUP_RELAY_SECRET='')
    def test_no_secret_no_call(self):
        with mock.patch('requests.Session.get') as get:
            self.assertIsNone(sg.fetch_stoplist('levone'))
        self.assertFalse(get.called)
