"""
Выгрузка сканов QR для CheckUp («скан со стола → официант стола», 24.09.2026).

Как в tests.py обмена: БД не трогаем. Разбор запроса и сборка строки — чистые
функции; цепочка проверок ручки — RequestFactory с подменой того, что ходит
в тенантную схему (её в тестовой базе нет).

Что заперто:
  • порядок отказов: внешний адрес → 403, выключено → 503, секрет → 401,
    сеть не в белом списке → 403, сети нет → 404;
  • секрет СВОЙ: секрет обмена токена эту ручку не открывает (контракт 4.15);
  • пустой белый список = ни одной сети;
  • в строке нет данных гостя, кроме внутреннего id; время — с зоной;
  • действия считаются только ПОСЛЕ скана и внутри окна;
  • официант, выбранный гостем, берётся из игры внутри окна;
  • пагинация по id: `next_after_id` = последний отданный скан.
"""
import datetime
import zoneinfo
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

from django.test import RequestFactory, SimpleTestCase, override_settings

from . import scans_export as SE

URL = '/api/v1/internal/checkup/scans/'
SECRET = 'test-export-secret'
MSK = zoneinfo.ZoneInfo('Europe/Moscow')


def _at(h, m=0, day=20):
    return datetime.datetime(2026, 9, day, h, m, tzinfo=MSK)


def _scan(pk=1, at=None, mode='cafe', table=7, ext='5f1c-7', hall='Зал', guest=500,
          profile=900, is_employee=False):
    branch = SimpleNamespace(branch_id=1, name='Levone | Ленина')
    qr = SimpleNamespace(pk=11, key='k11', mode=mode, name='Зал · стол 7', table_number=table,
                         table_external_id=ext, table_hall=hall, branch_id=3, branch=branch)
    client = SimpleNamespace(pk=profile, client_id=guest, is_employee=is_employee)
    return SimpleNamespace(pk=pk, scanned_at=at or _at(12), qr=qr, client=client,
                           client_id=profile)


def _game(at, served_by_id=None, updated_at=None, vk=None, first='', last=''):
    return (at, {'served_by_id': served_by_id, 'served_vk_id': vk, 'served_first_name': first,
                 'served_last_name': last, 'updated_at': updated_at or at})


# ── разбор запроса ───────────────────────────────────────────────────────────

@override_settings(TIME_ZONE='Europe/Moscow')
class ParseQueryTest(SimpleTestCase):

    def _q(self, **over):
        params = {'tenant_schema': 'levone', 'from': '2026-09-20', 'to': '2026-09-21'}
        params.update(over)
        return SE.parse_query(params)

    def test_dates_are_local_midnight_with_zone(self):
        q = self._q()
        self.assertEqual(q['start'].isoformat(), '2026-09-20T00:00:00+03:00')
        self.assertEqual(q['end'].isoformat(), '2026-09-21T00:00:00+03:00')
        self.assertEqual(q['modes'], ('cafe', 'review'))
        self.assertEqual((q['after_id'], q['limit'], q['window']),
                         (0, SE.DEFAULT_LIMIT, SE.DEFAULT_WINDOW_MIN))

    def test_datetime_keeps_its_offset(self):
        q = self._q(**{'from': '2026-09-20T09:00:00+00:00', 'to': '2026-09-20T12:00:00+00:00'})
        self.assertEqual(q['start'], datetime.datetime(2026, 9, 20, 12, 0, tzinfo=MSK))

    def test_bad_periods(self):
        for over in ({'to': '2026-09-20'}, {'to': '2026-09-19'}, {'to': '2026-11-01'},
                     {'from': ''}, {'from': 'вчера'}):
            with self.subTest(over=over), self.assertRaises(SE.ExportError) as cm:
                self._q(**over)
            self.assertEqual((cm.exception.status, cm.exception.code), (400, 'invalid_query'))

    def test_caps(self):
        q = self._q(limit='99999', window='100000', after_id='-5')
        self.assertEqual((q['limit'], q['window'], q['after_id']),
                         (SE.MAX_LIMIT, SE.MAX_WINDOW_MIN, 0))

    def test_bad_numbers_and_schema(self):
        for over in ({'limit': 'много'}, {'tenant_schema': 'public'}, {'tenant_schema': ''},
                     {'tenant_schema': '1abc'}, {'modes': 'story'}):
            with self.subTest(over=over), self.assertRaises(SE.ExportError):
                self._q(**over)

    def test_modes(self):
        self.assertEqual(self._q(modes='cafe,cafe,delivery')['modes'], ('cafe', 'delivery'))


# ── строка выгрузки ──────────────────────────────────────────────────────────

@override_settings(TIME_ZONE='Europe/Moscow')
class AssembleRowTest(SimpleTestCase):

    def test_shape_has_no_guest_personal_data(self):
        row = SE.assemble_row(_scan(), {}, set(), 60)
        self.assertEqual(set(row), {'scan_id', 'scanned_at', 'qr', 'branch', 'guest_id',
                                    'is_employee', 'actions', 'first_action', 'served_by'})
        self.assertEqual(row['guest_id'], 500)
        self.assertEqual(row['scanned_at'], '2026-09-20T12:00:00+03:00')
        self.assertEqual(row['qr'], {'id': 11, 'src': 'k11', 'mode': 'cafe', 'name': 'Зал · стол 7',
                                     'table_number': 7, 'table_external_id': '5f1c-7',
                                     'table_hall': 'Зал'})
        self.assertEqual(row['branch'], {'id': 3, 'branch_id': 1, 'name': 'Levone | Ленина'})
        self.assertEqual(row['actions'], [])
        self.assertIsNone(row['first_action'])
        self.assertIsNone(row['served_by'])
        self.assertFalse(row['is_employee'])

    def test_only_actions_after_scan_and_inside_window(self):
        facts = {
            'games': [_game(_at(12, 10)), _game(_at(11, 30))],
            'subscribes': [_at(11, 50)],           # до скана — не заслуга стола
            'reviews': [_at(12, 40), _at(13, 5)],  # вторая — за окном
        }
        row = SE.assemble_row(_scan(), facts, set(), 60)
        self.assertEqual(row['actions'], [
            {'kind': 'game', 'at': '2026-09-20T12:10:00+03:00'},
            {'kind': 'review', 'at': '2026-09-20T12:40:00+03:00'},
        ])
        self.assertEqual(row['first_action'], {'kind': 'game', 'at': '2026-09-20T12:10:00+03:00'})

    def test_action_exactly_at_window_end_is_out(self):
        row = SE.assemble_row(_scan(), {'reviews': [_at(13, 0)]}, set(), 60)
        self.assertEqual(row['actions'], [])

    def test_served_by_from_game_in_window(self):
        facts = {'games': [_game(_at(12, 5), served_by_id=77, updated_at=_at(12, 7),
                                 vk=12345, first='Александра', last='Бутусова')]}
        row = SE.assemble_row(_scan(), facts, set(), 60)
        self.assertEqual(row['served_by'], {'profile_id': 77, 'vk_id': '12345',
                                            'name': 'Александра Бутусова',
                                            'at': '2026-09-20T12:07:00+03:00'})

    def test_served_by_outside_window_is_ignored(self):
        facts = {'games': [_game(_at(14, 0), served_by_id=77, vk=1)]}
        self.assertIsNone(SE.assemble_row(_scan(), facts, set(), 60)['served_by'])

    def test_employee_by_profile_or_any_branch(self):
        self.assertTrue(SE.assemble_row(_scan(is_employee=True), {}, set(), 60)['is_employee'])
        self.assertTrue(SE.assemble_row(_scan(guest=500), {}, {500}, 60)['is_employee'])
        self.assertFalse(SE.assemble_row(_scan(guest=500), {}, {501}, 60)['is_employee'])

    def test_qr_without_table(self):
        row = SE.assemble_row(_scan(table=None, ext='', hall=''), {}, set(), 60)
        self.assertEqual((row['qr']['table_number'], row['qr']['table_external_id'],
                          row['qr']['table_hall']), (None, '', ''))


@override_settings(TIME_ZONE='Europe/Moscow')
class BuildExportTest(SimpleTestCase):

    def test_page_and_cursor(self):
        scans = [_scan(pk=1, at=_at(12)), _scan(pk=2, at=_at(13), profile=901),
                 _scan(pk=3, at=_at(14))]
        q = SE.parse_query({'tenant_schema': 'levone', 'from': '2026-09-20',
                            'to': '2026-09-21', 'limit': '2', 'window': '30'})
        with mock.patch.object(SE, 'fetch_scans', return_value=scans), \
             mock.patch.object(SE, 'collect_facts', return_value={}) as facts, \
             mock.patch.object(SE, 'employee_guest_ids', return_value=set()):
            out = SE.build_export(q)
        self.assertEqual((out['count'], out['has_more'], out['next_after_id']), (2, True, 2))
        self.assertEqual([r['scan_id'] for r in out['results']], [1, 2])
        ids, since, until = facts.call_args.args
        self.assertEqual(ids, [900, 901])
        self.assertEqual((since, until), (_at(12), _at(13, 30)))
        self.assertEqual(out['window_minutes'], 30)
        self.assertEqual(out['timezone'], 'Europe/Moscow')

    def test_last_page_has_no_cursor(self):
        q = SE.parse_query({'tenant_schema': 'levone', 'from': '2026-09-20', 'to': '2026-09-21'})
        with mock.patch.object(SE, 'fetch_scans', return_value=[_scan(pk=5)]), \
             mock.patch.object(SE, 'collect_facts', return_value={}), \
             mock.patch.object(SE, 'employee_guest_ids', return_value=set()):
            out = SE.build_export(q)
        self.assertEqual((out['has_more'], out['next_after_id']), (False, None))

    def test_empty_page_does_not_touch_facts(self):
        q = SE.parse_query({'tenant_schema': 'levone', 'from': '2026-09-20', 'to': '2026-09-21'})
        with mock.patch.object(SE, 'fetch_scans', return_value=[]), \
             mock.patch.object(SE, 'collect_facts') as facts:
            out = SE.build_export(q)
        facts.assert_not_called()
        self.assertEqual((out['count'], out['results']), (0, []))


# ── ручка: порядок проверок ──────────────────────────────────────────────────

@override_settings(CHECKUP_SCANS_EXPORT_SECRET=SECRET, CHECKUP_SCANS_EXPORT_TENANTS=['levone'],
                   CHECKUP_TOKEN_EXCHANGE_SECRET='exchange-secret', TIME_ZONE='Europe/Moscow')
class ViewTest(SimpleTestCase):

    def _get(self, params=None, secret=SECRET, **extra):
        rf = RequestFactory()
        headers = {}
        if secret is not None:
            headers['HTTP_X_LOYALUP_EXPORT_SECRET'] = secret
        headers.update(extra)
        request = rf.get(URL, params if params is not None else
                         {'tenant_schema': 'levone', 'from': '2026-09-20', 'to': '2026-09-21'},
                         **headers)
        return SE.ScansExportView.as_view()(request)

    def test_external_address_is_403(self):
        self.assertEqual(self._get(REMOTE_ADDR='8.8.8.8').status_code, 403)
        self.assertEqual(self._get(HTTP_X_FORWARDED_FOR='8.8.8.8').status_code, 403)

    @override_settings(CHECKUP_SCANS_EXPORT_SECRET='')
    def test_empty_secret_is_503_even_with_exchange_secret(self):
        resp = self._get(secret='exchange-secret')
        self.assertEqual(resp.status_code, 503)
        self.assertIn(b'export_disabled', resp.content)

    def test_wrong_or_foreign_secret_is_401(self):
        for secret in ('nope', 'exchange-secret', None):
            with self.subTest(secret=secret):
                self.assertEqual(self._get(secret=secret).status_code, 401)

    @override_settings(CHECKUP_SCANS_EXPORT_TENANTS=[])
    def test_empty_allowlist_means_no_tenant(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 403)
        self.assertIn(b'tenant_not_allowed', resp.content)

    def test_other_tenant_is_403(self):
        resp = self._get({'tenant_schema': 'asap_orel', 'from': '2026-09-20', 'to': '2026-09-21'})
        self.assertEqual(resp.status_code, 403)

    def test_missing_tenant_is_404(self):
        tenant_model = mock.MagicMock()
        tenant_model.objects.filter.return_value.first.return_value = None
        with mock.patch.object(SE, 'get_tenant_model', return_value=tenant_model):
            resp = self._get()
        self.assertEqual(resp.status_code, 404)

    def test_bad_query_is_400(self):
        resp = self._get({'tenant_schema': 'levone', 'from': '2026-09-21', 'to': '2026-09-20'})
        self.assertEqual(resp.status_code, 400)

    def test_ok(self):
        payload = {'count': 0, 'results': [], 'start': 's', 'end': 'e', 'has_more': False}
        with mock.patch.object(SE, 'resolve_tenant'), \
             mock.patch.object(SE, 'schema_context', return_value=nullcontext()) as ctx, \
             mock.patch.object(SE, 'build_export', return_value=payload) as build:
            resp = self._get()
        self.assertEqual(resp.status_code, 200)
        ctx.assert_called_once_with('levone')
        self.assertEqual(build.call_args.args[0]['tenant_schema'], 'levone')
