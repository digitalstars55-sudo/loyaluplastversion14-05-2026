"""
Тесты выгрузки выданных подарков для 1С (apps.tenant.analytics.gifts_export).

Без базы: агрегация и файлы — чистые функции; сбор фактов проверяется с
подменёнными выборками (_fetch_*), вьюха — с подменёнными Branch/render/доступом.
"""
import csv
import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from django.test import RequestFactory, SimpleTestCase
from django.utils import timezone

from apps.tenant.analytics import gifts_export as gx

MSK = ZoneInfo('Europe/Moscow')
_GX = 'apps.tenant.analytics.gifts_export'


def _issue(branch_id, day, name, branch_name=None):
    if branch_name is None and branch_id is not None:
        branch_name = f'Бар {branch_id}'
    return gx.GiftIssue(branch_id, branch_name, name, day)


def _msk(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=MSK)


def _inv(activated_at=None, used_at=None, branch_id=1, branch_name='Ленина 23',
         product='Сыр коса, 100 гр.', source='super_prize', cb_id=100):
    return {
        'acquired_from': source, 'activated_at': activated_at, 'used_at': used_at,
        'client_branch_id': cb_id, 'client_branch__branch_id': branch_id,
        'client_branch__branch__name': branch_name, 'product__name': product,
    }


def _story(activated_at=None, used_at=None, activated_branch=None, branch_id=1,
           branch_name='Ленина 23', product='0,5 л пенного'):
    ab_id, ab_name = activated_branch or (None, None)
    return {
        'activated_at': activated_at, 'used_at': used_at,
        'activated_branch_id': ab_id, 'activated_branch__name': ab_name,
        'client_branch__branch_id': branch_id, 'client_branch__branch__name': branch_name,
        'product__name': product,
    }


# ── Агрегация ────────────────────────────────────────────────────────────────

class AggregateIssuesTest(SimpleTestCase):
    START, END = date(2026, 9, 1), date(2026, 9, 3)

    def test_group_by_day_counts_and_sorts(self):
        issues = [
            _issue(2, date(2026, 9, 2), 'Фисташки, 100 гр', 'Никитина 9'),
            _issue(1, date(2026, 9, 2), 'Сыр коса, 100 гр.', 'Ленина 23'),
            _issue(1, date(2026, 9, 1), 'Сыр коса, 100 гр.', 'Ленина 23'),
            _issue(1, date(2026, 9, 1), 'Арахис в асс. 100 гр.', 'Ленина 23'),
            _issue(1, date(2026, 9, 1), 'Сыр коса, 100 гр.', 'Ленина 23'),
        ]
        rows = gx.aggregate_issues(issues, self.START, self.END, gx.GROUP_DAY)
        self.assertEqual(
            [(r.bar, r.date_label, r.name, r.qty) for r in rows],
            [
                ('Ленина 23', '01.09.2026', 'Арахис в асс. 100 гр.', 1),
                ('Ленина 23', '01.09.2026', 'Сыр коса, 100 гр.', 2),
                ('Ленина 23', '02.09.2026', 'Сыр коса, 100 гр.', 1),
                ('Никитина 9', '02.09.2026', 'Фисташки, 100 гр', 1),
            ],
        )
        self.assertEqual(gx.total_qty(rows), 5)

    def test_group_by_period_collapses_days(self):
        issues = [
            _issue(1, date(2026, 9, 1), '1 литр пенного'),
            _issue(1, date(2026, 9, 3), '1 литр пенного'),
            _issue(1, date(2026, 9, 2), 'Свиные уши'),
        ]
        rows = gx.aggregate_issues(issues, self.START, self.END, gx.GROUP_PERIOD)
        self.assertEqual(
            [(r.date_label, r.name, r.qty) for r in rows],
            [('01.09.2026–03.09.2026', '1 литр пенного', 2),
             ('01.09.2026–03.09.2026', 'Свиные уши', 1)],
        )
        self.assertTrue(all(r.is_period for r in rows))
        self.assertEqual((rows[0].date_from, rows[0].date_to), (self.START, self.END))

    def test_issues_outside_period_are_dropped(self):
        issues = [
            _issue(1, date(2026, 8, 31), 'Сыр коса, 100 гр.'),
            _issue(1, date(2026, 9, 4), 'Сыр коса, 100 гр.'),
            _issue(1, date(2026, 9, 3), 'Сыр коса, 100 гр.'),
        ]
        for group in (gx.GROUP_DAY, gx.GROUP_PERIOD):
            with self.subTest(group=group):
                rows = gx.aggregate_issues(issues, self.START, self.END, group)
                self.assertEqual(gx.total_qty(rows), 1)

    def test_no_branch_gets_own_row_last(self):
        issues = [
            _issue(None, date(2026, 9, 1), 'Сыр коса, 100 гр.'),
            _issue(3, date(2026, 9, 1), 'Сыр коса, 100 гр.', 'Ярцева 1'),
            _issue(1, date(2026, 9, 1), 'Сыр коса, 100 гр.', 'Абрикосовая 2'),
        ]
        rows = gx.aggregate_issues(issues, self.START, self.END)
        self.assertEqual([r.bar for r in rows], ['Абрикосовая 2', 'Ярцева 1', gx.NO_BRANCH_LABEL])
        self.assertIsNone(rows[-1].branch_id)
        self.assertEqual(gx.total_qty(rows), 3)

    def test_same_name_from_different_cards_is_one_line(self):
        # В каталоге Birfest две карточки «1 литр пенного» — для 1С это одна номенклатура.
        issues = [_issue(1, date(2026, 9, 1), '1 литр пенного') for _ in range(3)]
        rows = gx.aggregate_issues(issues, self.START, self.END)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].qty, 3)

    def test_yo_sorts_like_e(self):
        issues = [
            _issue(1, date(2026, 9, 1), 'Жёлтый полосатик'),
            _issue(1, date(2026, 9, 1), 'Ёрш'),
            _issue(1, date(2026, 9, 1), 'Арахис'),
        ]
        rows = gx.aggregate_issues(issues, self.START, self.END)
        self.assertEqual([r.name for r in rows], ['Арахис', 'Ёрш', 'Жёлтый полосатик'])

    def test_unknown_group_rejected(self):
        with self.assertRaises(ValueError):
            gx.aggregate_issues([], self.START, self.END, 'week')


# ── Сбор фактов выдачи ───────────────────────────────────────────────────────

class CollectIssuesTest(SimpleTestCase):
    START, END = date(2026, 9, 1), date(2026, 9, 2)

    def _collect(self, inv=(), story=(), rf=None, branch_ids=None):
        with timezone.override(MSK), \
                patch(f'{_GX}._fetch_inventory_rows', return_value=list(inv)), \
                patch(f'{_GX}._fetch_story_rows', return_value=list(story)), \
                patch(f'{_GX}._fetch_rf_activation_branches', return_value=rf or {}) as rf_mock:
            issues = gx.collect_issues(self.START, self.END, branch_ids)
        return issues, rf_mock

    def test_not_issued_items_are_skipped(self):
        issues, _ = self._collect(
            inv=[_inv(activated_at=None, used_at=None), _inv(activated_at=_msk(2026, 9, 1))],
            story=[_story(activated_at=None)],
        )
        self.assertEqual(len(issues), 1)

    def test_activation_outside_window_is_skipped(self):
        issues, _ = self._collect(inv=[
            _inv(activated_at=_msk(2026, 8, 31, 23, 59)),
            _inv(activated_at=_msk(2026, 9, 3, 0, 0)),
            _inv(activated_at=_msk(2026, 9, 2, 23, 59)),
        ])
        self.assertEqual([i.issued_on for i in issues], [date(2026, 9, 2)])

    def test_day_is_local_not_utc(self):
        # 21:30 UTC 1 сентября = 00:30 МСК 2 сентября → день выдачи 02.09.
        utc_dt = datetime(2026, 9, 1, 21, 30, tzinfo=ZoneInfo('UTC'))
        issues, _ = self._collect(inv=[_inv(activated_at=utc_dt)])
        self.assertEqual(issues[0].issued_on, date(2026, 9, 2))

    def test_used_without_activation_counts_by_used_at(self):
        issues, _ = self._collect(inv=[_inv(activated_at=None, used_at=_msk(2026, 9, 2))])
        self.assertEqual(issues[0].issued_on, date(2026, 9, 2))

    def test_story_gift_goes_to_bar_where_it_was_taken(self):
        issues, _ = self._collect(story=[
            _story(activated_at=_msk(2026, 9, 1), activated_branch=(7, 'Мира 57/4'), branch_id=1),
            _story(activated_at=_msk(2026, 9, 1), activated_branch=None, branch_id=1),
        ])
        self.assertEqual([(i.branch_id, i.branch_name) for i in issues],
                         [(7, 'Мира 57/4'), (1, 'Ленина 23')])

    def test_rf_reward_goes_to_bar_of_network_code(self):
        at = _msk(2026, 9, 1, 20)
        issues, rf_mock = self._collect(
            inv=[
                _inv(activated_at=at, source='rfm', cb_id=55, branch_id=1),
                _inv(activated_at=at, source='rf_auto', cb_id=56, branch_id=1),   # события нет
                _inv(activated_at=at, source='purchase', cb_id=57, branch_id=1),
            ],
            rf={(55, at): (9, 'Ленина 83')},
        )
        self.assertEqual([i.branch_id for i in issues], [9, 1, 1])
        self.assertEqual(set(rf_mock.call_args.args[0]), {55, 56})

    def test_rf_lookup_skipped_when_no_rf_rewards(self):
        _, rf_mock = self._collect(inv=[_inv(activated_at=_msk(2026, 9, 1))])
        rf_mock.assert_not_called()

    def test_deleted_product_keeps_its_line(self):
        issues, _ = self._collect(inv=[_inv(activated_at=_msk(2026, 9, 1), product=None)])
        self.assertEqual(issues[0].product_name, gx.DELETED_PRODUCT_LABEL)

    def test_branch_filter_uses_actual_bar_and_drops_no_branch(self):
        rows = dict(
            inv=[_inv(activated_at=_msk(2026, 9, 1), branch_id=1),
                 _inv(activated_at=_msk(2026, 9, 1), branch_id=2, branch_name='Никитина 9')],
            story=[_story(activated_at=_msk(2026, 9, 1), activated_branch=(2, 'Никитина 9'), branch_id=1),
                   _story(activated_at=_msk(2026, 9, 1), branch_id=None, branch_name=None)],
        )
        only_2, _ = self._collect(branch_ids=[2], **rows)
        self.assertEqual([i.branch_id for i in only_2], [2, 2])

        everything, _ = self._collect(branch_ids=None, **rows)
        self.assertEqual(sorted(i.branch_id or 0 for i in everything), [0, 1, 2, 2])

    def test_no_branch_row_survives_to_export(self):
        with patch(f'{_GX}.collect_issues', return_value=[
            _issue(None, date(2026, 9, 1), 'Сыр коса, 100 гр.'),
            _issue(1, date(2026, 9, 1), 'Сыр коса, 100 гр.', 'Ленина 23'),
        ]):
            rows = gx.build_gift_export(self.START, self.END, group=gx.GROUP_PERIOD)
        self.assertEqual([r.bar for r in rows], ['Ленина 23', gx.NO_BRANCH_LABEL])


# ── Файлы ────────────────────────────────────────────────────────────────────

def _rows_day():
    return gx.aggregate_issues([
        _issue(1, date(2026, 9, 1), 'Сыр «Паутинка»; 50 гр.', 'Ленина 23'),
        _issue(1, date(2026, 9, 1), 'Сыр «Паутинка»; 50 гр.', 'Ленина 23'),
        _issue(None, date(2026, 9, 2), 'Гренки & <чесночные>'),
    ], date(2026, 9, 1), date(2026, 9, 2))


class CsvExportTest(SimpleTestCase):

    def test_bom_semicolon_and_headers(self):
        data = gx.rows_to_csv(_rows_day())
        self.assertTrue(data.startswith('﻿'.encode('utf-8')))
        text = data.decode('utf-8-sig')
        self.assertTrue(text.startswith('Бар;Дата;Наименование;Количество\r\n'))
        parsed = list(csv.reader(io.StringIO(text), delimiter=';'))
        self.assertEqual(parsed[0], list(gx.HEADERS))
        self.assertEqual(parsed[1], ['Ленина 23', '01.09.2026', 'Сыр «Паутинка»; 50 гр.', '2'])
        self.assertEqual(parsed[2], [gx.NO_BRANCH_LABEL, '02.09.2026', 'Гренки & <чесночные>', '1'])
        self.assertEqual(len(parsed), 3)  # итоговой строки нет — 1С загрузила бы её как товар

    def test_period_label(self):
        rows = gx.aggregate_issues([_issue(1, date(2026, 9, 5), 'Свиные уши')],
                                   date(2026, 9, 1), date(2026, 9, 24), gx.GROUP_PERIOD)
        text = gx.rows_to_csv(rows).decode('utf-8-sig')
        self.assertIn('Бар 1;01.09.2026–24.09.2026;Свиные уши;1', text)

    def test_empty_export_has_only_headers(self):
        text = gx.rows_to_csv([]).decode('utf-8-sig')
        self.assertEqual(text, 'Бар;Дата;Наименование;Количество\r\n')


class XlsxExportTest(SimpleTestCase):
    NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}

    def _read(self, rows):
        zf = zipfile.ZipFile(io.BytesIO(gx.rows_to_xlsx(rows)))
        self.assertEqual(zf.testzip(), None)
        names = set(zf.namelist())
        for part in ('[Content_Types].xml', '_rels/.rels', 'xl/workbook.xml',
                     'xl/_rels/workbook.xml.rels', 'xl/styles.xml',
                     'xl/sharedStrings.xml', 'xl/worksheets/sheet1.xml'):
            self.assertIn(part, names)
        sst = [si.find('m:t', self.NS).text
               for si in ET.fromstring(zf.read('xl/sharedStrings.xml')).findall('m:si', self.NS)]
        sheet = ET.fromstring(zf.read('xl/worksheets/sheet1.xml'))
        table = []
        for row in sheet.find('m:sheetData', self.NS).findall('m:row', self.NS):
            cells = []
            for c in row.findall('m:c', self.NS):
                v = c.find('m:v', self.NS).text
                cells.append((sst[int(v)] if c.get('t') == 's' else v, c.get('s')))
            table.append(cells)
        return table

    def test_headers_dates_numbers(self):
        table = self._read(_rows_day())
        self.assertEqual([v for v, _ in table[0]], list(gx.HEADERS))
        self.assertTrue(all(s == '1' for _, s in table[0]))             # заголовки жирные
        bar, day, name, qty = table[1]
        self.assertEqual(bar[0], 'Ленина 23')
        serial = (date(2026, 9, 1) - date(1899, 12, 30)).days
        self.assertEqual(day, (str(serial), '2'))                      # ячейка-дата дд.мм.гггг
        self.assertEqual(name[0], 'Сыр «Паутинка»; 50 гр.')
        self.assertEqual(qty[0], '2')
        self.assertEqual(table[2][2][0], 'Гренки & <чесночные>')      # спецсимволы XML не ломают файл
        self.assertEqual(len(table), 3)

    def test_period_is_text(self):
        rows = gx.aggregate_issues([_issue(1, date(2026, 9, 5), 'Свиные уши')],
                                   date(2026, 9, 1), date(2026, 9, 24), gx.GROUP_PERIOD)
        table = self._read(rows)
        self.assertEqual(table[1][1], ('01.09.2026–24.09.2026', None))

    def test_empty_export_is_valid(self):
        table = self._read([])
        self.assertEqual(len(table), 1)


class FilenameTest(SimpleTestCase):

    def test_pattern(self):
        self.assertEqual(
            gx.export_filename('Birfest', date(2026, 9, 1), date(2026, 9, 24), 'xlsx'),
            'podarki_Birfest_2026-09-01_2026-09-24.xlsx',
        )

    def test_schema_is_sanitized(self):
        self.assertEqual(
            gx.export_filename('a b"c', date(2026, 9, 1), date(2026, 9, 1), 'csv'),
            'podarki_abc_2026-09-01_2026-09-01.csv',
        )


# ── Веб-страница и API: доступ ───────────────────────────────────────────────

def _staff(**kw):
    base = dict(is_authenticated=True, is_active=True, is_staff=True, is_superuser=False,
                is_anonymous=False, feature_access=[], pk=1, id=1, username='owner')
    base.update(kw)
    return SimpleNamespace(**base)


class GiftsExportViewAccessTest(SimpleTestCase):
    """Точки — только доступные пользователю; «Без точки» — только без ограничений."""

    def _get(self, params, allowed=None, user=None):
        from apps.tenant.analytics.views import GiftsExport1CView

        request = RequestFactory().get('/analytics/gifts-1c/', params)
        request.user = user or _staff()
        branch_qs = MagicMock()
        branch_qs.filter.return_value = branch_qs
        branch_qs.values.return_value.order_by.return_value = [{'id': 1, 'name': 'Ленина 23'}]
        captured = {}

        def fake_render(req, template, context):
            captured.update(context)
            from django.http import HttpResponse
            return HttpResponse('ok')

        with patch('apps.tenant.analytics.views.Branch') as Branch, \
                patch('apps.tenant.analytics.views.render', side_effect=fake_render), \
                patch('apps.shared.users.access.user_allowed_branches', return_value=allowed), \
                patch('apps.shared.users.access.current_schema_name', return_value='Birfest'), \
                patch(f'{_GX}.build_gift_export', return_value=[]) as build:
            Branch.objects.filter.return_value = branch_qs
            resp = GiftsExport1CView.as_view()(request)
        return resp, build, captured

    def test_unrestricted_all_bars_includes_no_branch(self):
        _, build, ctx = self._get({'start': '2026-09-01', 'end': '2026-09-24'})
        self.assertIsNone(build.call_args.kwargs['branch_ids'])
        self.assertEqual(ctx['error'], '')

    def test_restricted_all_bars_means_only_allowed(self):
        _, build, _ = self._get({'start': '2026-09-01', 'end': '2026-09-24'}, allowed={3, 1})
        self.assertEqual(build.call_args.kwargs['branch_ids'], [1, 3])

    def test_restricted_foreign_bar_is_refused(self):
        _, build, ctx = self._get({'start': '2026-09-01', 'end': '2026-09-24', 'branch': '5'},
                                  allowed={1})
        build.assert_not_called()
        self.assertTrue(ctx['error'])

    def test_start_after_end_is_refused(self):
        _, build, ctx = self._get({'start': '2026-09-24', 'end': '2026-09-01'})
        build.assert_not_called()
        self.assertTrue(ctx['error'])

    def test_first_open_shows_form_only(self):
        _, build, ctx = self._get({})
        build.assert_not_called()
        self.assertFalse(ctx['submitted'])

    def test_download_returns_attachment(self):
        resp, _, _ = self._get({'start': '2026-09-01', 'end': '2026-09-24', 'branch': '1',
                                'group': 'period', 'fmt': 'csv', 'download': '1'})
        self.assertEqual(resp['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('podarki_Birfest_2026-09-01_2026-09-24.csv', resp['Content-Disposition'])

    def test_feature_gate_redirects(self):
        resp, build, _ = self._get({'start': '2026-09-01', 'end': '2026-09-24'},
                                   user=_staff(feature_access=['reviews']))
        self.assertEqual(resp.status_code, 302)
        build.assert_not_called()


class GiftsExportApiAuthTest(SimpleTestCase):

    def test_anonymous_rejected(self):
        from rest_framework.test import APIRequestFactory
        from apps.tenant.analytics.api.views import GiftsExport1CAPIView

        resp = GiftsExport1CAPIView.as_view()(
            APIRequestFactory().get('/api/v1/analytics/gifts-1c/', {'fmt': 'csv'}),
        )
        self.assertIn(resp.status_code, (401, 403))
