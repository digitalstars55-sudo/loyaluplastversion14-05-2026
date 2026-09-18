"""
Тесты ручек точек контакта (QR) для кабинета CheckUp — контракт №26/№27.

Стратегия патчей — как в senler/tests_broadcasts_api.py: модели тенантные, в
тестовой БД их таблиц нет, поэтому в ORM не ходим совсем. Вьюхи импортируют
модели и сервисы на уровне модуля, значит подменяем их прямо в
apps.tenant.branch.api.contact_points:
  QRCode / QRScan / ContactPointEvent / Branch / GuestClient — стабы и моки;
  effective_branch_ids — RBAC (None = все точки);
  get_contact_point_funnel — воронка;
  current_company_id / current_schema_name — тенант.

Главный тест здесь — таблица ссылок (`QrLinkTableTest`). По этим ссылкам
напечатаны десятки тысяч QR: если формат поедет, гость уйдёт в чужую точку
или размещение выпадет из воронки. Таблица прибита гвоздями намеренно.
"""
import json
from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant.branch.api import contact_points as CP
from apps.tenant.branch.api.qr_links import build_branch_link, build_qr_link

CPP = 'apps.tenant.branch.api.contact_points.'
APP_ID = 53418653
COMPANY = '7'


# ── стабы ────────────────────────────────────────────────────────────────────

class _Mode:
    CAFE = 'cafe'
    DELIVERY = 'delivery'
    DELIVERY_NETWORK = 'delivery_network'
    WEBSITE = 'website'
    REVIEW = 'review'
    values = ['cafe', 'delivery', 'delivery_network', 'website', 'review']


class _QRCodeStub:
    """Класс-заместитель модели: настоящие значения Mode + мок менеджера."""
    Mode = _Mode
    objects = MagicMock()


def _branch(pk=3, branch_id=202, name='Институтская'):
    return SimpleNamespace(pk=pk, id=pk, branch_id=branch_id, name=name)


def _qr(pk=11, mode='cafe', key='abc123', table_number=None, branch=None, name='Флаер у кассы'):
    br = branch or _branch()
    return SimpleNamespace(
        pk=pk, id=pk, name=name, mode=mode, key=key, table_number=table_number,
        branch=br, branch_id=br.pk, is_active=True,
        created_at=datetime(2026, 9, 18, 10, 0, tzinfo=dt_timezone.utc),
        get_mode_display=lambda: {'cafe': 'В кафе (на месте)', 'review': 'Отзыв со стола'}.get(mode, mode),
        save=MagicMock(), delete=MagicMock(),
    )


def _user(is_superuser=True):
    return SimpleNamespace(is_authenticated=True, is_active=True, is_superuser=is_superuser,
                           username='t', pk=1, is_staff=True)


def _call(view_cls, method, path, *, data=None, params=None, **kwargs):
    factory = APIRequestFactory()
    if method == 'get':
        request = factory.get(path, params or {})
    else:
        request = getattr(factory, method)(path, data or {}, format='json')
    force_authenticate(request, user=_user())
    return view_cls.as_view()(request, **kwargs)


# ── ссылка: таблица форматов ─────────────────────────────────────────────────

class QrLinkTableTest(SimpleTestCase):
    """Формат ссылки = формат админки (apps/tenant/branch/admin.py:1245)."""

    def test_cafe(self):
        link = build_qr_link(_qr(mode='cafe', key='k1'), COMPANY, APP_ID)
        self.assertEqual(link, f'https://vk.com/app{APP_ID}/#/?company=7&branch=202&src=k1')

    def test_delivery(self):
        link = build_qr_link(_qr(mode='delivery', key='k2'), COMPANY, APP_ID)
        self.assertEqual(link,
                         f'https://vk.com/app{APP_ID}/#/?company=7&branch=202&delivery=true&src=k2')

    def test_delivery_network_has_no_branch(self):
        # Сетевой QR доставки: точку решает код, branch в ссылке быть НЕ должно.
        link = build_qr_link(_qr(mode='delivery_network', key='k3'), COMPANY, APP_ID)
        self.assertEqual(link, f'https://vk.com/app{APP_ID}/#/?company=7&delivery=true&src=k3')
        self.assertNotIn('branch=', link)

    def test_website_carries_web_and_src(self):
        link = build_qr_link(_qr(mode='website', key='k4'), COMPANY, APP_ID)
        self.assertEqual(link,
                         f'https://vk.com/app{APP_ID}/#/?company=7&branch=202&web=k4&src=k4')

    def test_review_opens_review_form_with_table(self):
        link = build_qr_link(_qr(mode='review', key='k5', table_number=7), COMPANY, APP_ID)
        self.assertEqual(link,
                         f'https://vk.com/app{APP_ID}/#/review?company=7&branch=202&table=7&src=k5')

    def test_branch_link_without_src(self):
        self.assertEqual(build_branch_link(_branch(), COMPANY, vk_app_id=APP_ID),
                         f'https://vk.com/app{APP_ID}/#/?company=7&branch=202')
        self.assertEqual(build_branch_link(_branch(), COMPANY, delivery=True, vk_app_id=APP_ID),
                         f'https://vk.com/app{APP_ID}/#/?company=7&branch=202&delivery=true')

    def test_stub_modes_match_the_model(self):
        # Появится шестой режим — тест упадёт здесь, а не в кабинете CheckUp.
        from apps.tenant.branch.models import QRCode as RealQRCode
        self.assertEqual(list(RealQRCode.Mode.values), _Mode.values)


# ── доступ ───────────────────────────────────────────────────────────────────

class ContactPointsAuthTest(SimpleTestCase):
    """Каждая вьюха модуля закрыта IsAuthenticated (правило из backend/CLAUDE.md)."""

    def test_every_view_requires_auth(self):
        views = [
            CP.ContactPointListCreateAPIView,
            CP.ContactPointDetailAPIView,
            CP.ContactPointGuestsAPIView,
            CP.ContactPointBatchTablesAPIView,
            CP.BranchMaterialsAPIView,
        ]
        for view in views:
            with self.subTest(view=view.__name__):
                self.assertIn(IsAuthenticated, view.permission_classes)

    def test_anonymous_gets_401_or_403(self):
        factory = APIRequestFactory()
        resp = CP.ContactPointListCreateAPIView.as_view()(factory.get('/api/v1/contact-points/'))
        self.assertIn(resp.status_code, (401, 403))


# ── создание ─────────────────────────────────────────────────────────────────

@patch(CPP + 'current_schema_name', return_value='dev')
@patch(CPP + 'current_company_id', return_value=COMPANY)
@patch(CPP + 'effective_branch_ids', return_value=None)
@patch(CPP + 'QRCode', _QRCodeStub)
class ContactPointCreateTest(SimpleTestCase):

    def setUp(self):
        _QRCodeStub.objects.reset_mock()

    def _post(self, payload):
        return _call(CP.ContactPointListCreateAPIView, 'post', '/api/v1/contact-points/', data=payload)

    def test_src_is_not_accepted(self, *_):
        resp = self._post({'branch_id': 3, 'name': 'X', 'mode': 'cafe', 'src': 'mine'})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data['code'], 'invalid_payload')

    def test_name_required(self, *_):
        resp = self._post({'branch_id': 3, 'name': '  ', 'mode': 'cafe'})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))

    def test_unknown_mode(self, *_):
        resp = self._post({'branch_id': 3, 'name': 'X', 'mode': 'story'})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))
        self.assertIn('review', resp.data['detail'])

    def test_review_without_table(self, *_):
        resp = self._post({'branch_id': 3, 'name': 'Стол', 'mode': 'review'})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'table_required'))

    def test_table_forbidden_for_other_modes(self, *_):
        resp = self._post({'branch_id': 3, 'name': 'X', 'mode': 'cafe', 'table_number': 5})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))

    @patch(CPP + '_branch_or_none', return_value=None)
    def test_foreign_branch_is_404_not_403(self, *_):
        resp = self._post({'branch_id': 999, 'name': 'X', 'mode': 'cafe'})
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))

    @patch(CPP + '_branch_or_none')
    def test_created_row_carries_src_and_url(self, branch_or_none, *_):
        branch_or_none.return_value = _branch()
        _QRCodeStub.objects.create.return_value = _qr(mode='review', key='k5', table_number=7)
        resp = self._post({'branch_id': 3, 'name': 'Стол 7', 'mode': 'review', 'table_number': 7})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['src'], 'k5')
        self.assertEqual(resp.data['table_number'], 7)
        self.assertEqual(resp.data['branch'], {'id': 3, 'branch_id': 202, 'name': 'Институтская'})
        self.assertIn('table=7', resp.data['url'])
        self.assertEqual(resp.data['funnel'], CP.EMPTY_FUNNEL)


# ── правка и удаление ────────────────────────────────────────────────────────

@patch(CPP + 'current_schema_name', return_value='dev')
@patch(CPP + 'current_company_id', return_value=COMPANY)
@patch(CPP + 'effective_branch_ids', return_value=None)
@patch(CPP + 'QRCode', _QRCodeStub)
class ContactPointPatchDeleteTest(SimpleTestCase):

    def _patch_call(self, payload, qr, has_scans):
        with patch(CPP + '_get_qr', return_value=qr), \
             patch(CPP + '_has_scans', return_value=has_scans):
            return _call(CP.ContactPointDetailAPIView, 'patch',
                         '/api/v1/contact-points/11/', data=payload, pk=11)

    def test_missing_is_404(self, *_):
        with patch(CPP + '_get_qr', return_value=None):
            resp = _call(CP.ContactPointDetailAPIView, 'patch',
                         '/api/v1/contact-points/11/', data={'name': 'X'}, pk=11)
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))

    def test_mode_change_after_scans_is_409(self, *_):
        resp = self._patch_call({'mode': 'cafe'}, _qr(mode='review', table_number=7), True)
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'has_scans'))
        self.assertEqual(resp.data['fields'], ['mode'])

    def test_rename_after_scans_is_allowed(self, *_):
        qr = _qr()
        resp = self._patch_call({'name': 'Новое место'}, qr, True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(qr.name, 'Новое место')

    def test_deactivate_after_scans_is_allowed(self, *_):
        qr = _qr()
        resp = self._patch_call({'is_active': False}, qr, True)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(qr.is_active)

    def test_mode_away_from_review_clears_table(self, *_):
        qr = _qr(mode='review', table_number=7)
        resp = self._patch_call({'mode': 'cafe'}, qr, False)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(qr.table_number)
        self.assertNotIn('table=', resp.data['url'])

    def test_switch_to_review_without_table_is_table_required(self, *_):
        resp = self._patch_call({'mode': 'review'}, _qr(mode='cafe'), False)
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'table_required'))

    def test_patch_always_returns_fresh_url(self, *_):
        resp = self._patch_call({'name': 'X'}, _qr(key='k1'), True)
        self.assertIn('src=k1', resp.data['url'])

    def test_delete_with_scans_is_409(self, *_):
        qr = _qr()
        with patch(CPP + '_get_qr', return_value=qr), patch(CPP + '_has_scans', return_value=True):
            resp = _call(CP.ContactPointDetailAPIView, 'delete', '/api/v1/contact-points/11/', pk=11)
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'has_scans'))
        qr.delete.assert_not_called()

    def test_delete_without_scans(self, *_):
        qr = _qr()
        with patch(CPP + '_get_qr', return_value=qr), patch(CPP + '_has_scans', return_value=False):
            resp = _call(CP.ContactPointDetailAPIView, 'delete', '/api/v1/contact-points/11/', pk=11)
        self.assertEqual(resp.status_code, 204)
        qr.delete.assert_called_once()


# ── гости стадии ─────────────────────────────────────────────────────────────

@patch(CPP + 'current_schema_name', return_value='dev')
@patch(CPP + 'effective_branch_ids', return_value=None)
class ContactPointGuestsTest(SimpleTestCase):

    def test_unknown_stage_is_400(self, *_):
        with patch(CPP + '_get_qr', return_value=_qr()):
            resp = _call(CP.ContactPointGuestsAPIView, 'get',
                         '/api/v1/contact-points/11/guests/', params={'stage': 'bought'}, pk=11)
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))

    def test_foreign_point_is_404(self, *_):
        with patch(CPP + '_get_qr', return_value=None):
            resp = _call(CP.ContactPointGuestsAPIView, 'get',
                         '/api/v1/contact-points/11/guests/', pk=11)
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))

    def test_stage_labels_cover_all_stages(self, *_):
        self.assertEqual(set(CP.STAGES), set(CP.STAGE_LABELS))


# ── пакет столов ─────────────────────────────────────────────────────────────

@patch(CPP + 'current_schema_name', return_value='dev')
@patch(CPP + 'current_company_id', return_value=COMPANY)
@patch(CPP + 'effective_branch_ids', return_value=None)
@patch(CPP + 'QRCode', _QRCodeStub)
class BatchTablesTest(SimpleTestCase):

    def setUp(self):
        _QRCodeStub.objects.reset_mock()

    def _post(self, payload):
        return _call(CP.ContactPointBatchTablesAPIView, 'post',
                     '/api/v1/contact-points/batch-tables/', data=payload)

    def test_bad_range(self, *_):
        for payload in ({'branch_id': 3, 'from': 0, 'to': 5},
                        {'branch_id': 3, 'from': 5, 'to': 1},
                        {'branch_id': 3, 'from': 'a', 'to': 5}):
            with self.subTest(payload=payload):
                resp = self._post(payload)
                self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))

    def test_cap(self, *_):
        resp = self._post({'branch_id': 3, 'from': 1, 'to': CP.BATCH_TABLES_MAX + 1})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))
        self.assertIn(str(CP.BATCH_TABLES_MAX), resp.data['detail'])

    @patch(CPP + '_branch_or_none', return_value=None)
    def test_foreign_branch_is_404(self, *_):
        resp = self._post({'branch_id': 999, 'from': 1, 'to': 2})
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))

    @patch(CPP + '_branch_or_none')
    def test_existing_tables_are_skipped(self, branch_or_none, *_):
        branch_or_none.return_value = _branch()
        # Стол 2 уже занят активным QR «отзыв со стола».
        (_QRCodeStub.objects.filter.return_value
         .values_list.return_value) = [2]
        created = []

        def _create(**kw):
            qr = _qr(pk=100 + kw['table_number'], mode='review',
                     key=f"k{kw['table_number']}", table_number=kw['table_number'],
                     name=kw['name'])
            created.append(qr)
            return qr

        _QRCodeStub.objects.create.side_effect = _create
        resp = self._post({'branch_id': 3, 'from': 1, 'to': 3})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual([r['table_number'] for r in resp.data['created']], [1, 3])
        self.assertEqual(resp.data['skipped'], [{'table_number': 2, 'reason': 'already_exists'}])
        self.assertEqual(created[0].name, 'Отзыв со стола 1')


# ── мелкие помощники ─────────────────────────────────────────────────────────

class HelpersTest(SimpleTestCase):

    def test_page_params_caps_limit(self):
        factory = APIRequestFactory()
        req = factory.get('/', {'limit': '9999', 'offset': '-3'})
        req.query_params = req.GET
        self.assertEqual(CP._page_params(req), (CP.MAX_LIMIT, 0))

    def test_page_params_defaults(self):
        factory = APIRequestFactory()
        req = factory.get('/')
        req.query_params = req.GET
        self.assertEqual(CP._page_params(req), (CP.DEFAULT_LIMIT, 0))

    def test_scan_windows_empty_without_ids(self):
        self.assertEqual(CP._scan_windows([]), {})

    def test_scan_windows_three_queries(self):
        with patch(CPP + 'QRScan') as scan:
            base = scan.objects.filter.return_value
            base.values.return_value.annotate.return_value = [{'qr_id': 11, 'n': 5}]
            base.filter.return_value.values.return_value.annotate.return_value = [
                {'qr_id': 11, 'n': 2}]
            out = CP._scan_windows([11])
        self.assertEqual(out[11], {'d7': 2, 'd30': 2, 'all': 5})

    def test_error_shape(self):
        resp = CP._error('has_scans', 'есть сканы', 409, fields=['mode'])
        self.assertEqual(resp.data, {'code': 'has_scans', 'detail': 'есть сканы', 'fields': ['mode']})


class ScopeTest(SimpleTestCase):
    """
    Пустой список точек = «все точки», а не «ни одной».

    `effective_branch_ids` отвечает неограниченному сотруднику ровно тем, что
    он прислал: без `?branch_ids` это пустой список. Если фильтровать по нему,
    суперадмин увидит ноль точек контакта — этот тест держит границу.
    """

    def _list(self, scope):
        qs = MagicMock()
        stub = SimpleNamespace(Mode=_Mode, objects=MagicMock())
        stub.objects.select_related.return_value.order_by.return_value = qs
        qs.count.return_value = 0
        qs.__getitem__ = lambda self_, item: []
        with patch(CPP + 'QRCode', stub), \
             patch(CPP + 'current_schema_name', return_value='dev'), \
             patch(CPP + 'effective_branch_ids', return_value=scope), \
             patch(CPP + 'current_company_id', return_value=COMPANY), \
             patch(CPP + 'get_contact_point_funnel', return_value=[]), \
             patch(CPP + '_scan_windows', return_value={}):
            resp = _call(CP.ContactPointListCreateAPIView, 'get', '/api/v1/contact-points/')
        return resp, qs

    def test_empty_scope_does_not_filter(self):
        resp, qs = self._list([])
        self.assertEqual(resp.status_code, 200)
        qs.filter.assert_not_called()

    def test_no_access_scope_filters_to_nothing(self):
        resp, qs = self._list([-1])
        self.assertEqual(resp.status_code, 200)
        qs.filter.assert_called_once_with(branch_id__in=[-1])


# ── доделка по ревью CheckUp (3б.8) ──────────────────────────────────────────

class TotalsFilteredTest(SimpleTestCase):
    """`totals` — по показанной странице, `totals_filtered` — по всему фильтру."""

    def test_two_sums_differ(self):
        qr11, qr12 = _qr(pk=11, key='k11'), _qr(pk=12, key='k12')
        stub = SimpleNamespace(Mode=_Mode, objects=MagicMock())
        qs = MagicMock()
        stub.objects.select_related.return_value.order_by.return_value = qs
        qs.values_list.return_value = [11, 12]
        qs.__getitem__ = lambda self_, item: [qr11]   # на странице только первый
        funnel = {
            11: {'scans': 10, 'guests': 5, 'subscribed': 2, 'played': 1, 'activated': 1,
                 'conversion': 40},
            12: {'scans': 4, 'guests': 4, 'subscribed': 2, 'played': 0, 'activated': 0,
                 'conversion': 50},
        }
        with patch(CPP + 'QRCode', stub), \
             patch(CPP + 'current_schema_name', return_value='dev'), \
             patch(CPP + 'current_company_id', return_value=COMPANY), \
             patch(CPP + 'effective_branch_ids', return_value=None), \
             patch(CPP + '_funnel_map', return_value=funnel), \
             patch(CPP + '_scan_windows', return_value={11: {'d7': 1, 'd30': 2, 'all': 10}}):
            resp = _call(CP.ContactPointListCreateAPIView, 'get', '/api/v1/contact-points/')

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['total'], 2, 'total — по всему фильтру')
        self.assertEqual(len(resp.data['results']), 1, 'на странице одна строка')
        self.assertEqual(resp.data['totals']['guests'], 5)
        self.assertEqual(resp.data['totals_filtered']['guests'], 9)
        self.assertEqual(resp.data['totals_filtered']['subscribed'], 4)
        # Конверсия считается по сумме, а не как среднее по строкам (м4).
        self.assertEqual(resp.data['totals_filtered']['conversion'], 44)

    def test_sum_funnel_conversion_is_integer_percent(self):
        out = CP._sum_funnel([{'scans': 1, 'guests': 3, 'subscribed': 1, 'played': 0,
                               'activated': 0}])
        self.assertEqual(out['conversion'], 33)

    def test_sum_funnel_without_guests_is_zero(self):
        self.assertEqual(CP._sum_funnel([])['conversion'], 0)
        self.assertEqual(CP._sum_funnel([None])['guests'], 0)


class HostIndependenceTest(SimpleTestCase):
    """
    Ссылки не зависят от Host и схемы запроса (★5 ревью).

    CheckUp ходит к нам через loopback по http с подменённым Host, поэтому
    ссылка, собранная из запроса, увела бы гостя на внутренний адрес. Формат
    собирается из настроек, `client_id` сети и публичного `branch_id`.
    """

    def test_materials_links_ignore_request_host(self):
        branch = _branch()
        stub = SimpleNamespace(Mode=_Mode, objects=MagicMock())
        (stub.objects.select_related.return_value
         .filter.return_value.order_by.return_value) = [_qr(mode='cafe', key='k1')]
        with patch(CPP + 'QRCode', stub), patch(CPP + 'Branch') as branch_model, \
             patch(CPP + 'effective_branch_ids', return_value=None), \
             patch(CPP + 'current_schema_name', return_value='dev'), \
             patch(CPP + 'current_company_id', return_value=COMPANY):
            branch_model.objects.filter.return_value.first.return_value = branch
            factory = APIRequestFactory()
            request = factory.get('/api/v1/mobile/branches/3/materials/',
                                  HTTP_HOST='127.0.0.1:7000')
            force_authenticate(request, user=_user())
            resp = CP.BranchMaterialsAPIView.as_view()(request, pk=3)

        body = json.dumps(resp.data, ensure_ascii=False)
        self.assertNotIn('127.0.0.1', body)
        self.assertNotIn('http://', body, 'ссылки только https на vk.com')
        self.assertIn('https://vk.com/app', body)

    def test_link_builder_takes_no_request(self):
        import inspect
        from apps.tenant.branch.api.qr_links import build_branch_link, build_qr_link
        for func in (build_qr_link, build_branch_link):
            with self.subTest(func=func.__name__):
                self.assertNotIn('request', inspect.signature(func).parameters)


class GuestVkIdTypeTest(SimpleTestCase):
    """`vk_id` гостя — строкой: у CheckUp поле строковое (м6 ревью)."""

    def test_vk_id_is_string(self):
        qr = _qr()
        guest = SimpleNamespace(pk=77, vk_id=887316623, first_name='Пётр', last_name='Иванов',
                                rf_score=None)
        grouped = MagicMock()
        grouped.order_by.return_value = grouped
        grouped.count.return_value = 1
        grouped.__getitem__ = lambda self_, item: [{'client__client_id': 77,
                                                    'at': datetime(2026, 9, 18, tzinfo=dt_timezone.utc)}]
        with patch(CPP + '_get_qr', return_value=qr), \
             patch(CPP + 'effective_branch_ids', return_value=None), \
             patch(CPP + 'current_schema_name', return_value='dev'), \
             patch(CPP + 'QRScan') as scan, \
             patch(CPP + 'GuestClient') as guests:
            scan.objects.filter.return_value.values.return_value.annotate.return_value = grouped
            guests.objects.filter.return_value.select_related.return_value = [guest]
            resp = _call(CP.ContactPointGuestsAPIView, 'get',
                         '/api/v1/contact-points/11/guests/', params={'stage': 'scan'}, pk=11)

        self.assertEqual(resp.status_code, 200)
        row = resp.data['results'][0]
        self.assertEqual(row['vk_id'], '887316623')
        self.assertIsInstance(row['vk_id'], str)
        self.assertEqual(row['guest_id'], 77)
        self.assertIsNone(row['segment'])
