"""
Комментарии к отчёту и печатная версия (контракт 3б.4, №28).

Модель тенантная, в тестовую БД не ходим: менеджер подменяется через
`patch.object(LoyaltyReportComment, 'objects')`, настоящие `make_branch_key` и
`Meta` остаются на месте. Вызов Claude подменяется функцией
`generate_comment_text` — сетевых запросов в тестах нет.

Главные тесты: `SectionsMatchWebPageTest` (названия секций не должны разъехаться
с веб-страницей, иначе кабинет подпишет комментарий не тем разделом) и
`PrintViewTest` (печатная версия обязана быть самодостаточной: ни скриптов,
кроме кнопки, ни внешних адресов, ни `?token=` в ссылках).
"""
import inspect
from datetime import date, datetime, timezone as dt_timezone
from types import SimpleNamespace
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant.analytics.api import report_comments as RC
from apps.tenant.analytics.models import LoyaltyReportComment

RCP = 'apps.tenant.analytics.api.report_comments.'
START, END = date(2026, 9, 1), date(2026, 9, 30)


def _user(username='alina'):
    return SimpleNamespace(is_authenticated=True, is_active=True, is_superuser=True,
                           username=username, pk=1, is_staff=True)


def _row(section_num=1, text='Текст', is_ai=False, author='alina',
         updated_at=datetime(2026, 9, 18, 12, 0, tzinfo=dt_timezone.utc)):
    row = SimpleNamespace(section_num=section_num, text=text, is_ai=is_ai, author=author,
                          updated_at=updated_at)
    row.save = MagicMock()
    row.delete = MagicMock()
    return row


def _call(view_cls, method, path, *, data=None, params=None, user=None):
    factory = APIRequestFactory()
    if method == 'get':
        request = factory.get(path, params or {})
    else:
        request = getattr(factory, method)(path + ('?' + '&'.join(
            f'{k}={v}' for k, v in (params or {}).items()) if params else ''),
            data or {}, format='json')
    force_authenticate(request, user=user or _user())
    return view_cls.as_view()(request)


# ── секции ───────────────────────────────────────────────────────────────────

class SectionsMatchWebPageTest(SimpleTestCase):
    """Названия и порядок секций — те же, что на веб-странице отчёта."""

    def test_eleven_sections_in_order(self):
        nums = [num for num, _, _ in RC.SECTIONS]
        self.assertEqual(nums, list(range(1, 12)))

    def test_titles_are_the_same_as_in_the_web_view(self):
        from apps.tenant.analytics.views import LoyaltyReportView
        source = inspect.getsource(LoyaltyReportView.get)
        for num, title, _ in RC.SECTIONS:
            with self.subTest(section=num, title=title):
                self.assertIn(title, source)

    def test_every_section_has_metrics_entry(self):
        for num, _, _ in RC.SECTIONS:
            self.assertIn(num, RC.SECTION_METRICS)

    def test_endpoint_returns_sections(self):
        resp = _call(RC.ReportSectionsAPIView, 'get', '/api/v1/analytics/report/sections/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['sections']), 11)
        self.assertEqual(resp.data['sections'][0],
                         {'num': 1, 'title': 'Ключевые метрики', 'metric_keys': ['stats']})

    def test_requires_auth(self):
        self.assertIn(IsAuthenticated, RC.ReportSectionsAPIView.permission_classes)
        factory = APIRequestFactory()
        resp = RC.ReportSectionsAPIView.as_view()(factory.get('/api/v1/analytics/report/sections/'))
        self.assertIn(resp.status_code, (401, 403))


# ── ключ ячейки отчёта ───────────────────────────────────────────────────────

class PeriodKeyTest(SimpleTestCase):

    def test_branch_key_sorts_and_dedups(self):
        self.assertEqual(LoyaltyReportComment.make_branch_key([3, 1, 3]), '1,3')

    def test_branch_key_all(self):
        self.assertEqual(LoyaltyReportComment.make_branch_key([]), 'all')
        self.assertEqual(LoyaltyReportComment.make_branch_key(None), 'all')

    def test_period_key(self):
        self.assertEqual(RC.period_key(START, END, [3, 1]), '2026-09-01_2026-09-30_1,3')
        self.assertEqual(RC.period_key(START, END, []), '2026-09-01_2026-09-30_all')


# ── разбор тела PUT ──────────────────────────────────────────────────────────

class ParseItemsTest(SimpleTestCase):

    def test_ok(self):
        items = RC._parse_items({'comments': [{'section_num': 2, 'text': ' вывод '}]})
        self.assertEqual(items, [{'section_num': 2, 'text': 'вывод',
                                  'expected_updated_at': None, 'is_ai': False}])

    def test_bad_bodies(self):
        for data in ({}, {'comments': []}, {'comments': 'нет'},
                     {'comments': [{'section_num': 99, 'text': 'x'}]},
                     {'comments': [{'section_num': 'abc', 'text': 'x'}]},
                     {'comments': [{'section_num': 1, 'text': 'x' * (RC.MAX_TEXT_LEN + 1)}]},
                     {'comments': [{'section_num': 1, 'text': 'a'}, {'section_num': 1, 'text': 'b'}]}):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    RC._parse_items(data)

    def test_stale_detection(self):
        row = _row()
        self.assertFalse(RC._stale(row, None))
        self.assertFalse(RC._stale(None, '2026-09-18T12:00:00+00:00'))
        self.assertFalse(RC._stale(row, '2026-09-18T12:00:00+00:00'))
        self.assertTrue(RC._stale(row, '2026-09-18T11:00:00+00:00'))


# ── ручка комментариев ───────────────────────────────────────────────────────

class CommentsViewTest(SimpleTestCase):

    def _objects(self, rows):
        objects = MagicMock()
        objects.filter.return_value.order_by.return_value = rows
        objects.select_for_update.return_value.filter.return_value = rows
        return objects

    def test_get_returns_comments_of_the_cell(self):
        objects = self._objects([_row(section_num=7, text='Негатива стало меньше')])
        with patch.object(RC.LoyaltyReportComment, 'objects', objects), \
             patch(RCP + '_atomic', side_effect=nullcontext), \
             patch(RCP + 'effective_branch_ids', return_value=[3]), \
             patch(RCP + 'current_schema_name', return_value='levone'):
            resp = _call(RC.ReportCommentsAPIView, 'get', '/api/v1/analytics/report/comments/',
                         params={'start': '2026-09-01', 'end': '2026-09-30'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['period_key'], '2026-09-01_2026-09-30_3')
        self.assertEqual(resp.data['comments'][0]['section_num'], 7)
        self.assertEqual(resp.data['comments'][0]['section_title'], 'Отзывы гостей')

    def test_put_conflict_when_someone_saved_earlier(self):
        existing = _row(section_num=1)
        objects = self._objects([existing])
        with patch.object(RC.LoyaltyReportComment, 'objects', objects), \
             patch(RCP + '_atomic', side_effect=nullcontext), \
             patch(RCP + 'effective_branch_ids', return_value=[]), \
             patch(RCP + 'current_schema_name', return_value='levone'):
            resp = _call(RC.ReportCommentsAPIView, 'put', '/api/v1/analytics/report/comments/',
                         params={'start': '2026-09-01', 'end': '2026-09-30'},
                         data={'comments': [{'section_num': 1, 'text': 'моя версия',
                                             'updated_at': '2026-09-18T10:00:00+00:00'}]})
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'conflict'))
        self.assertEqual(resp.data['conflicts'][0]['section_num'], 1)
        existing.save.assert_not_called()

    def test_put_updates_existing_and_creates_missing(self):
        existing = _row(section_num=1, text='старое')
        objects = self._objects([existing])
        with patch.object(RC.LoyaltyReportComment, 'objects', objects), \
             patch(RCP + '_atomic', side_effect=nullcontext), \
             patch(RCP + 'effective_branch_ids', return_value=[]), \
             patch(RCP + 'current_schema_name', return_value='levone'):
            resp = _call(RC.ReportCommentsAPIView, 'put', '/api/v1/analytics/report/comments/',
                         params={'start': '2026-09-01', 'end': '2026-09-30'},
                         data={'comments': [{'section_num': 1, 'text': 'новое'},
                                            {'section_num': 2, 'text': 'второй раздел'}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(existing.text, 'новое')
        self.assertEqual(existing.author, 'alina')
        existing.save.assert_called_once()
        objects.create.assert_called_once()
        created = objects.create.call_args.kwargs
        self.assertEqual((created['section_num'], created['text']), (2, 'второй раздел'))
        self.assertEqual(created['branch_key'], 'all')

    def test_put_empty_text_deletes(self):
        existing = _row(section_num=3, text='было')
        objects = self._objects([existing])
        with patch.object(RC.LoyaltyReportComment, 'objects', objects), \
             patch(RCP + '_atomic', side_effect=nullcontext), \
             patch(RCP + 'effective_branch_ids', return_value=[]), \
             patch(RCP + 'current_schema_name', return_value='levone'):
            resp = _call(RC.ReportCommentsAPIView, 'put', '/api/v1/analytics/report/comments/',
                         params={'start': '2026-09-01', 'end': '2026-09-30'},
                         data={'comments': [{'section_num': 3, 'text': '   '}]})
        self.assertEqual(resp.status_code, 200)
        existing.delete.assert_called_once()
        objects.create.assert_not_called()

    def test_bad_section_is_400(self):
        with patch.object(RC.LoyaltyReportComment, 'objects', self._objects([])), \
             patch(RCP + 'effective_branch_ids', return_value=[]), \
             patch(RCP + 'current_schema_name', return_value='levone'):
            resp = _call(RC.ReportCommentsAPIView, 'put', '/api/v1/analytics/report/comments/',
                         params={'start': '2026-09-01', 'end': '2026-09-30'},
                         data={'comments': [{'section_num': 42, 'text': 'x'}]})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))


# ── генерация ИИ ─────────────────────────────────────────────────────────────

class GenerateViewTest(SimpleTestCase):

    def test_ai_unavailable_is_not_500(self):
        err = RC.AIUnavailable('ANTHROPIC_API_KEY не настроен', 503)
        with patch(RCP + 'generate_comment_text', side_effect=err), \
             patch(RCP + 'effective_branch_ids', return_value=[]), \
             patch(RCP + 'current_schema_name', return_value='levone'), \
             patch(RCP + '_company_name', return_value='LevOne'):
            resp = _call(RC.GenerateReportCommentSaveAPIView, 'post',
                         '/api/v1/analytics/report/comments/generate/',
                         params={'start': '2026-09-01', 'end': '2026-09-30'},
                         data={'section_num': 1})
        self.assertEqual((resp.status_code, resp.data['code']), (503, 'ai_unavailable'))

    def test_save_writes_ai_comment(self):
        objects = MagicMock()
        with patch(RCP + 'generate_comment_text', return_value='Сканов стало больше'), \
             patch.object(RC.LoyaltyReportComment, 'objects', objects), \
             patch(RCP + 'effective_branch_ids', return_value=[3]), \
             patch(RCP + 'current_schema_name', return_value='levone'), \
             patch(RCP + '_company_name', return_value='LevOne'):
            resp = _call(RC.GenerateReportCommentSaveAPIView, 'post',
                         '/api/v1/analytics/report/comments/generate/',
                         params={'start': '2026-09-01', 'end': '2026-09-30'},
                         data={'section_num': 1, 'save': True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['saved'])
        self.assertEqual(resp.data['text'], 'Сканов стало больше')
        kwargs = objects.update_or_create.call_args.kwargs
        self.assertEqual(kwargs['section_num'], 1)
        self.assertEqual(kwargs['branch_key'], '3')
        self.assertTrue(kwargs['defaults']['is_ai'])

    def test_without_save_nothing_is_written(self):
        objects = MagicMock()
        with patch(RCP + 'generate_comment_text', return_value='текст'), \
             patch.object(RC.LoyaltyReportComment, 'objects', objects), \
             patch(RCP + 'effective_branch_ids', return_value=[]), \
             patch(RCP + 'current_schema_name', return_value='levone'), \
             patch(RCP + '_company_name', return_value='LevOne'):
            resp = _call(RC.GenerateReportCommentSaveAPIView, 'post',
                         '/api/v1/analytics/report/comments/generate/',
                         params={'start': '2026-09-01', 'end': '2026-09-30'},
                         data={'section_num': 1})
        self.assertFalse(resp.data['saved'])
        objects.update_or_create.assert_not_called()

    def test_bad_section_is_400(self):
        with patch(RCP + 'effective_branch_ids', return_value=[]), \
             patch(RCP + 'current_schema_name', return_value='levone'):
            resp = _call(RC.GenerateReportCommentSaveAPIView, 'post',
                         '/api/v1/analytics/report/comments/generate/',
                         params={'start': '2026-09-01', 'end': '2026-09-30'},
                         data={'section_num': 0})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))

    def test_prompt_is_shared_with_the_live_endpoint(self):
        """Промпт не должен разъехаться с живой generate-comment/."""
        from apps.tenant.analytics.api.views import GenerateReportCommentAPIView
        live = inspect.getsource(GenerateReportCommentAPIView.post)
        mine = inspect.getsource(RC.generate_comment_text)
        prompt_line = 'Ты — менеджер системы лояльности ресторана/кафе.'
        self.assertIn(prompt_line, live)
        self.assertIn(prompt_line, mine)
        # Модель у нас вынесена в константу AI_MODEL — сверяем её с литералом живой ручки.
        self.assertEqual(RC.AI_MODEL, 'claude-haiku-4-5-20251001')
        self.assertIn(RC.AI_MODEL, live)


# ── строки метрик и печать ───────────────────────────────────────────────────

class MetricRowsTest(SimpleTestCase):

    PAYLOAD = {'stats': {'total_scans': 120, 'game_reached': 80, 'gift_cost_rub': 1234.5},
               'reviews': {'positive': 10, 'negative': 2},
               'sources': {'from_cafe': 90, 'from_delivery': 30}}

    def test_dig(self):
        self.assertEqual(RC._dig(self.PAYLOAD, 'stats.total_scans'), 120)
        self.assertIsNone(RC._dig(self.PAYLOAD, 'stats.nope'))
        self.assertIsNone(RC._dig(self.PAYLOAD, 'nope.nope'))

    def test_fmt(self):
        self.assertEqual(RC._fmt(None), '—')
        self.assertEqual(RC._fmt(1234.5), '1234,50')
        self.assertEqual(RC._fmt(True), 'да')
        self.assertEqual(RC._fmt([1, 2, 3]), '3')

    def test_missing_metrics_are_skipped(self):
        rows = RC.metric_rows(self.PAYLOAD, 1)
        labels = [r['label'] for r in rows]
        self.assertIn('Сканирований QR', labels)
        self.assertNotIn('Оцифрованных гостей', labels, 'метрики нет в payload — строки нет')

    def test_sections_without_metrics(self):
        self.assertEqual(RC.metric_rows(self.PAYLOAD, 10), [])


class PrintViewTest(SimpleTestCase):

    PAYLOAD = {'stats': {'total_scans': 120}, 'reviews': {'positive': 10, 'negative': 2},
               'sources': {'from_cafe': 90, 'from_delivery': 30}}

    def _render(self, comments=()):
        with patch(RCP + 'report_payload', return_value=self.PAYLOAD), \
             patch(RCP + 'comments_for', return_value=list(comments)), \
             patch(RCP + 'effective_branch_ids', return_value=[]), \
             patch(RCP + 'current_schema_name', return_value='levone'), \
             patch(RCP + '_company_name', return_value='LevOne'):
            return _call(RC.ReportPrintView, 'get', '/api/v1/analytics/report/print/',
                         params={'start': '2026-09-01', 'end': '2026-09-30'})

    def test_html_is_self_contained(self):
        resp = self._render()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp['Content-Type'].startswith('text/html'))
        html = resp.content.decode()
        # Единственный допустимый скрипт — кнопка печати; никаких <script>,
        # внешних адресов и токенов в ссылках (★21 контракта).
        self.assertNotIn('<script', html)
        self.assertNotIn('http://', html)
        self.assertNotIn('?token=', html)
        self.assertIn('window.print()', html)
        self.assertIn('LevOne', html)
        self.assertIn('01.09.2026 — 30.09.2026', html)
        self.assertIn('Отзывы гостей', html)

    def test_comment_from_database_is_printed(self):
        resp = self._render([{'section_num': 7, 'section_title': 'Отзывы гостей',
                              'text': 'Негатива стало меньше', 'is_ai': True,
                              'author': 'alina', 'updated_at': '2026-09-18T12:00:00+00:00'}])
        html = resp.content.decode()
        self.assertIn('Негатива стало меньше', html)
        self.assertIn('Комментарий ИИ', html)

    def test_empty_comment_is_marked(self):
        self.assertIn('Комментарий к разделу не заполнен', self._render().content.decode())

    def test_requires_auth(self):
        self.assertIn(IsAuthenticated, RC.ReportPrintView.permission_classes)


class ReportApiIncludesCommentsTest(SimpleTestCase):
    """`GET /analytics/report/` отдаёт `comments` — правка живой вьюхи в одну строку."""

    def test_key_is_in_the_response(self):
        from apps.tenant.analytics.api.views import LoyaltyReportAPIView
        source = inspect.getsource(LoyaltyReportAPIView.get)
        self.assertIn("'comments'", source)
        self.assertIn('report_comments.comments_for(start_date, end_date, branch_ids)', source)
