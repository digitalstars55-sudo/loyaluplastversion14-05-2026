"""
Фильтры и пагинация ленты отзывов (api/review_filters.py).

Проверяем собранный SQL (только WHERE), не гоняя запросы: тенантной схемы в
тестах нет, а нам важно лишь то, какие условия накладываются и что без
параметров QuerySet не меняется.
"""
from django.test import SimpleTestCase, TestCase

from apps.tenant.branch.models import TestimonialConversation

from .api.review_filters import (
    MAX_LIMIT, apply_review_filters, parse_checkup_status, parse_pagination,
    parse_sentiments, parse_sources, parse_status,
)


def _where(qs) -> str:
    sql = str(qs.query)
    return sql.split('WHERE', 1)[1] if 'WHERE' in sql else ''


class ParseHelpersTest(SimpleTestCase):

    def test_sentiments(self):
        self.assertEqual(parse_sentiments('NEGATIVE,positive'), ['NEGATIVE', 'POSITIVE'])
        self.assertEqual(parse_sentiments('bad'), ['NEGATIVE', 'PARTIALLY_NEGATIVE'])
        self.assertEqual(parse_sentiments('bad,NEGATIVE,мусор'), ['NEGATIVE', 'PARTIALLY_NEGATIVE'])
        self.assertEqual(parse_sentiments(None), [])
        self.assertEqual(parse_sentiments(''), [])

    def test_sources(self):
        self.assertEqual(parse_sources('app,vk'), ['APP', 'VK_MESSAGE'])
        self.assertEqual(parse_sources('VK_MESSAGE,x'), ['VK_MESSAGE'])
        self.assertEqual(parse_sources(None), [])

    def test_status(self):
        self.assertEqual(parse_status(' Unread '), 'unread')
        self.assertEqual(parse_status('whatever'), 'all')
        self.assertEqual(parse_status(None), 'all')

    def test_checkup_status(self):
        self.assertEqual(parse_checkup_status('resolved'), 'resolved')
        self.assertEqual(parse_checkup_status('none'), 'none')
        self.assertEqual(parse_checkup_status('bogus'), 'any')

    def test_pagination(self):
        self.assertEqual(parse_pagination({}), (None, 0))
        self.assertEqual(parse_pagination({'limit': '20', 'offset': '40'}), (20, 40))
        self.assertEqual(parse_pagination({'limit': '0'}), (1, 0))
        self.assertEqual(parse_pagination({'limit': '9999'}), (MAX_LIMIT, 0))
        self.assertEqual(parse_pagination({'limit': 'abc', 'offset': '-5'}), (None, 0))


class ApplyReviewFiltersTest(TestCase):

    def setUp(self):
        self.base = TestimonialConversation.objects.all()

    def test_no_params_leaves_queryset_alone(self):
        qs = apply_review_filters(self.base, {})
        self.assertEqual(str(qs.query), str(self.base.query))

    def test_unknown_values_are_ignored(self):
        qs = apply_review_filters(self.base, {'sentiment': 'мусор', 'status': 'x', 'source': 'y', 'checkup_status': 'z'})
        self.assertEqual(str(qs.query), str(self.base.query))

    def test_sentiment_in(self):
        w = _where(apply_review_filters(self.base, {'sentiment': 'bad'}))
        self.assertIn('sentiment', w)
        self.assertIn('NEGATIVE', w)
        self.assertIn('PARTIALLY_NEGATIVE', w)

    def test_status_unread(self):
        w = _where(apply_review_filters(self.base, {'status': 'unread'}))
        self.assertIn('has_unread', w)
        self.assertIn('is_replied', w)

    def test_status_replied_and_unanswered(self):
        self.assertIn('is_replied', _where(apply_review_filters(self.base, {'status': 'replied'})))
        self.assertIn('is_replied', _where(apply_review_filters(self.base, {'status': 'unanswered'})))

    def test_source_uses_exists_subquery(self):
        w = _where(apply_review_filters(self.base, {'source': 'app'}))
        self.assertIn('EXISTS', w)
        self.assertIn('APP', w)

    def test_checkup_status(self):
        self.assertIn("checkup_status", _where(apply_review_filters(self.base, {'checkup_status': 'resolved'})))
        w = _where(apply_review_filters(self.base, {'checkup_status': 'none'}))
        self.assertIn('checkup_status', w)

    def test_search(self):
        w = _where(apply_review_filters(self.base, {'q': '  12345 '}))
        self.assertIn('vk_sender_id', w)
        self.assertIn('first_name', w)
