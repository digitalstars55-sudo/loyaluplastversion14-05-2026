"""16.10.2026 — «Чекап» по п.10 ТЗ: пять состояний и «Что улучшить» по данным сети."""
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.tenant.analytics import setup_check as SC


class ЧтоУлучшить(SimpleTestCase):

    def test_сначала_проблемы_не_больше_трёх_и_с_экраном(self):
        blocks = [SC.block('digitize', 'Оцифровка', SC.OK, ''),
                  SC.block('marketer', 'AI', SC.SETUP, 'пусто', 'нет базы', href='/loyalty/marketer'),
                  SC.block('auto', 'Авто', SC.PROBLEM, '', 'срок кончился', href='/loyalty/auto-broadcasts'),
                  SC.block('reviews', 'Отзывы', SC.PROBLEM, '', '3 без ответа', href='/loyalty/reviews'),
                  SC.block('rf', 'RF', SC.NODATA, '', '', href='/loyalty/rf')]
        with patch('apps.tenant.analytics.rf_transitions.build', return_value={}):
            recs = SC.recommendations(blocks)
        self.assertEqual([r['key'] for r in recs], ['auto', 'reviews', 'marketer'])
        self.assertTrue(all(r['href'] for r in recs))
        self.assertEqual(recs[0]['reason'], 'срок кончился')

    def test_неизвестное_не_выглядит_исправным(self):
        b = SC.block('rf', 'RF', SC.NODATA, '')
        self.assertEqual((b['state_label'], b['status']), ('Нет данных для проверки', 'warn'))
