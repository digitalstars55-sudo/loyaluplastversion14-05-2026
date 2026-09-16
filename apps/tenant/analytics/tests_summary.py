"""
Сводка отзывов, детализация метрики, дашборд дня (api/summary.py) — без тенантной схемы:
права объявлены, мусор в query отбивается до похода в базу, словарь тональностей полный.
"""
from types import SimpleNamespace

from django.test import SimpleTestCase
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIRequestFactory, force_authenticate

from .api.summary import (
    SENTIMENT_LABELS, DashboardTodayAPIView, ReviewsSummaryAPIView, StatsDetailAPIView,
)


def _get(view_cls, path, **params):
    request = APIRequestFactory().get(path, params)
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True, is_superuser=True, pk=1))
    return view_cls.as_view()(request)


class SummaryViewsDeclareAuthTest(SimpleTestCase):

    def test_is_authenticated_everywhere(self):
        for cls in (ReviewsSummaryAPIView, StatsDetailAPIView, DashboardTodayAPIView):
            self.assertIn(IsAuthenticated, cls.permission_classes, cls.__name__)

    def test_anonymous_gets_401_or_403(self):
        request = APIRequestFactory().get('/api/v1/dashboard/today/')
        self.assertIn(DashboardTodayAPIView.as_view()(request).status_code, (401, 403))


class QueryValidationTest(SimpleTestCase):

    def test_bad_period_is_400(self):
        r = _get(ReviewsSummaryAPIView, '/api/v1/analytics/reviews/summary/', period='bogus')
        self.assertEqual(r.status_code, 400)

    def test_bad_branch_ids_is_400(self):
        r = _get(ReviewsSummaryAPIView, '/api/v1/analytics/reviews/summary/', branch_ids='a,b')
        self.assertEqual(r.status_code, 400)

    def test_unknown_metric_lists_known(self):
        r = _get(StatsDetailAPIView, '/api/v1/analytics/stats/detail/', metric='nope')
        self.assertEqual(r.status_code, 400)
        self.assertIn('qr_scans', r.data['metrics'])

    def test_metric_required(self):
        self.assertEqual(_get(StatsDetailAPIView, '/api/v1/analytics/stats/detail/').status_code, 400)


class SentimentLabelsTest(SimpleTestCase):

    def test_all_six_sentiments_present_in_web_order(self):
        keys = [k for k, _ in SENTIMENT_LABELS]
        self.assertEqual(keys, ['POSITIVE', 'NEGATIVE', 'PARTIALLY_NEGATIVE', 'NEUTRAL', 'SPAM', 'WAITING'])
