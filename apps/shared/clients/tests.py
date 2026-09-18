from datetime import date
from unittest.mock import MagicMock, patch

from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase

from apps.shared.config.admin_sites import public_admin, tenant_admin
from .admin import (
    DomainForm,
    DomainInline,
    SubdomainField,
    SubdomainWidget,
    _get_root_domain,
)
from .models import Company, Domain


# ---------------------------------------------------------------------------
# Company model
# ---------------------------------------------------------------------------

class CompanyModelTest(TestCase):

    def _make_company(self, **kwargs):
        defaults = {
            'schema_name': 'test_company',
            'client_id': 1,
            'name': 'Тест Ресторан',
            'paid_until': date(2026, 12, 31),
        }
        defaults.update(kwargs)
        return Company(**defaults)

    def test_str_returns_name(self):
        company = self._make_company(name='Бургер Кинг')
        self.assertEqual(str(company), 'Бургер Кинг')

    def test_is_active_defaults_to_false(self):
        company = self._make_company()
        self.assertFalse(company.is_active)

    def test_auto_create_schema_is_true(self):
        self.assertTrue(Company.auto_create_schema)

    def test_description_is_optional(self):
        company = self._make_company(description=None)
        self.assertIsNone(company.description)

    def test_verbose_name(self):
        self.assertEqual(Company._meta.verbose_name, 'Клиент')
        self.assertEqual(Company._meta.verbose_name_plural, 'Клиенты')


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------

class DomainModelTest(TestCase):

    def test_verbose_name(self):
        self.assertEqual(Domain._meta.verbose_name, 'Домен')
        self.assertEqual(Domain._meta.verbose_name_plural, 'Домены')

    def test_is_primary_defaults_to_true(self):
        domain = Domain(domain='test.localhost')
        self.assertTrue(domain.is_primary)


# ---------------------------------------------------------------------------
# SubdomainWidget
# ---------------------------------------------------------------------------

class SubdomainWidgetTest(TestCase):

    def setUp(self):
        self.widget = SubdomainWidget(root_domain='example.com')

    # format_value

    def test_format_value_strips_root_domain_suffix(self):
        self.assertEqual(self.widget.format_value('dev.example.com'), 'dev')

    def test_format_value_no_strip_when_no_match(self):
        # Value doesn't end with .example.com — returned as-is
        self.assertEqual(self.widget.format_value('dev'), 'dev')

    def test_format_value_none_returns_empty_string(self):
        self.assertEqual(self.widget.format_value(None), '')

    def test_format_value_empty_string_returns_empty_string(self):
        self.assertEqual(self.widget.format_value(''), '')

    def test_format_value_does_not_strip_partial_match(self):
        # 'example.com' itself should not be stripped (no leading dot+prefix)
        self.assertEqual(self.widget.format_value('example.com'), 'example.com')

    # render

    def test_render_contains_subdomain_wrapper(self):
        html = self.widget.render('domain', 'dev.example.com')
        self.assertIn('subdomain-wrapper', html)

    def test_render_contains_subdomain_suffix_span(self):
        html = self.widget.render('domain', 'dev.example.com')
        self.assertIn('subdomain-suffix', html)

    def test_render_suffix_displays_root_domain(self):
        html = self.widget.render('domain', 'dev.example.com')
        self.assertIn('.example.com', html)

    def test_render_input_shows_only_subdomain(self):
        html = self.widget.render('domain', 'dev.example.com')
        self.assertIn('value="dev"', html)

    def test_render_input_has_subdomain_input_class(self):
        html = self.widget.render('domain', 'dev.example.com')
        self.assertIn('subdomain-input', html)

    def test_render_input_has_placeholder(self):
        html = self.widget.render('domain', 'dev.example.com')
        self.assertIn('placeholder', html)


# ---------------------------------------------------------------------------
# SubdomainField
# ---------------------------------------------------------------------------

class SubdomainFieldTest(TestCase):

    def setUp(self):
        self.field = SubdomainField(root_domain='example.com')

    def test_clean_appends_root_domain(self):
        self.assertEqual(self.field.clean('mysite'), 'mysite.example.com')

    def test_clean_normalizes_to_lowercase(self):
        self.assertEqual(self.field.clean('MyRestaurant'), 'myrestaurant.example.com')

    def test_clean_strips_whitespace(self):
        self.assertEqual(self.field.clean('  dev  '), 'dev.example.com')

    def test_clean_valid_hyphen_in_middle(self):
        self.assertEqual(self.field.clean('my-restaurant'), 'my-restaurant.example.com')

    def test_clean_single_char_subdomain(self):
        self.assertEqual(self.field.clean('a'), 'a.example.com')

    def test_clean_underscore_raises(self):
        with self.assertRaises(ValidationError):
            self.field.clean('my_restaurant')

    def test_clean_leading_hyphen_raises(self):
        with self.assertRaises(ValidationError):
            self.field.clean('-dev')

    def test_clean_trailing_hyphen_raises(self):
        with self.assertRaises(ValidationError):
            self.field.clean('dev-')

    def test_clean_dot_in_subdomain_raises(self):
        with self.assertRaises(ValidationError):
            self.field.clean('dev.sub')

    def test_clean_special_chars_raise(self):
        with self.assertRaises(ValidationError):
            self.field.clean('dev!')

    def test_clean_empty_required_raises(self):
        with self.assertRaises(ValidationError):
            self.field.clean('')

    def test_clean_none_required_raises(self):
        with self.assertRaises(ValidationError):
            self.field.clean(None)

    def test_default_widget_is_subdomain_widget(self):
        self.assertIsInstance(self.field.widget, SubdomainWidget)

    def test_widget_root_domain_matches_field(self):
        self.assertEqual(self.field.widget.root_domain, 'example.com')


# ---------------------------------------------------------------------------
# _get_root_domain
# ---------------------------------------------------------------------------

class GetRootDomainTest(TestCase):

    @patch('apps.shared.clients.admin.Domain.objects')
    @patch('apps.shared.clients.admin.Company.objects')
    def test_returns_primary_domain_of_public_company(self, mock_co, mock_dom):
        mock_company = MagicMock()
        mock_co.filter.return_value.first.return_value = mock_company
        mock_domain = MagicMock()
        mock_domain.domain = 'levelupapp.ru'
        mock_dom.filter.return_value.first.return_value = mock_domain

        self.assertEqual(_get_root_domain(), 'levelupapp.ru')

    @patch('apps.shared.clients.admin.Company.objects')
    def test_returns_localhost_when_no_public_company(self, mock_co):
        mock_co.filter.return_value.first.return_value = None
        self.assertEqual(_get_root_domain(), 'localhost')

    @patch('apps.shared.clients.admin.Domain.objects')
    @patch('apps.shared.clients.admin.Company.objects')
    def test_returns_localhost_when_no_primary_domain(self, mock_co, mock_dom):
        mock_co.filter.return_value.first.return_value = MagicMock()
        mock_dom.filter.return_value.first.return_value = None
        self.assertEqual(_get_root_domain(), 'localhost')

    @patch('apps.shared.clients.admin.Company.objects')
    def test_returns_localhost_on_db_exception(self, mock_co):
        mock_co.filter.side_effect = Exception('DB unavailable')
        self.assertEqual(_get_root_domain(), 'localhost')


# ---------------------------------------------------------------------------
# DomainForm
# ---------------------------------------------------------------------------

class DomainFormTest(TestCase):

    @patch('apps.shared.clients.admin._get_root_domain', return_value='example.com')
    def test_domain_field_is_subdomain_field_instance(self, _mock):
        form = DomainForm()
        self.assertIsInstance(form.fields['domain'], SubdomainField)

    @patch('apps.shared.clients.admin._get_root_domain', return_value='example.com')
    def test_domain_field_uses_root_domain_from_helper(self, _mock):
        form = DomainForm()
        self.assertEqual(form.fields['domain'].root_domain, 'example.com')

    @patch('apps.shared.clients.admin._get_root_domain', return_value='example.com')
    def test_domain_field_label(self, _mock):
        form = DomainForm()
        self.assertEqual(form.fields['domain'].label, 'Поддомен')


# ---------------------------------------------------------------------------
# DomainInline
# ---------------------------------------------------------------------------

class DomainInlineTest(TestCase):

    def setUp(self):
        self.inline = DomainInline(Company, public_admin)

    def test_model_is_domain(self):
        self.assertIs(DomainInline.model, Domain)

    def test_form_is_domain_form(self):
        self.assertIs(DomainInline.form, DomainForm)

    def test_max_num(self):
        self.assertEqual(DomainInline.max_num, 5)

    def test_can_delete(self):
        self.assertTrue(DomainInline.can_delete)

    def test_verbose_name(self):
        self.assertEqual(DomainInline.verbose_name, 'Домен')
        self.assertEqual(DomainInline.verbose_name_plural, 'Домены')

    def test_css_media_includes_company_admin(self):
        all_css = DomainInline.Media.css.get('all', ())
        self.assertIn('admin/clients/css/company_admin.css', all_css)

    # get_extra

    def test_get_extra_returns_1_when_no_obj(self):
        self.assertEqual(self.inline.get_extra(None, obj=None), 1)

    @patch('apps.shared.clients.admin.Domain.objects')
    def test_get_extra_returns_0_when_domains_exist(self, mock_objects):
        mock_objects.filter.return_value.exists.return_value = True
        company = MagicMock(pk=1)
        self.assertEqual(self.inline.get_extra(None, obj=company), 0)

    @patch('apps.shared.clients.admin.Domain.objects')
    def test_get_extra_returns_1_when_no_domains(self, mock_objects):
        mock_objects.filter.return_value.exists.return_value = False
        company = MagicMock(pk=1)
        self.assertEqual(self.inline.get_extra(None, obj=company), 1)

    def test_get_extra_returns_1_when_obj_has_no_pk(self):
        company = MagicMock(pk=None)
        self.assertEqual(self.inline.get_extra(None, obj=company), 1)


# ---------------------------------------------------------------------------
# Admin registration
# ---------------------------------------------------------------------------

class AdminRegistrationTest(TestCase):

    def test_company_registered_in_public_admin(self):
        self.assertIn(Company, public_admin._registry)

    def test_domain_not_registered_directly_in_public_admin(self):
        # Domain is managed exclusively through DomainInline inside CompanyAdmin
        self.assertNotIn(Domain, public_admin._registry)

    def test_company_not_registered_in_tenant_admin(self):
        self.assertNotIn(Company, tenant_admin._registry)

    def test_domain_not_registered_in_tenant_admin(self):
        self.assertNotIn(Domain, tenant_admin._registry)

    def test_company_admin_has_domain_inline(self):
        company_admin = public_admin._registry[Company]
        inline_models = [i.model for i in company_admin.inlines]
        self.assertIn(Domain, inline_models)


# ---------------------------------------------------------------------------
# CompanyAdmin config
# ---------------------------------------------------------------------------

class CompanyAdminConfigTest(TestCase):

    def setUp(self):
        self.admin = public_admin._registry[Company]
        self.factory = RequestFactory()

    def test_list_display(self):
        # payment_badge (цветной срок оплаты) заменил голый paid_until,
        # admin_link ведёт в админку сети — тест догнал текущий список.
        self.assertEqual(
            self.admin.list_display,
            ('name', 'client_id', 'schema_name', 'primary_domain', 'is_active',
             'payment_badge', 'config_link', 'admin_link'),
        )

    def test_list_filter(self):
        from .admin import PaymentStatusFilter
        self.assertEqual(self.admin.list_filter, ('is_active', PaymentStatusFilter))

    def test_search_fields(self):
        self.assertEqual(self.admin.search_fields, ('name', 'schema_name'))

    def test_schema_name_readonly_when_editing_existing_obj(self):
        obj = MagicMock()
        readonly = self.admin.get_readonly_fields(self.factory.get('/'), obj=obj)
        self.assertIn('schema_name', readonly)

    def test_schema_name_not_readonly_when_creating_new_obj(self):
        readonly = self.admin.get_readonly_fields(self.factory.get('/'), obj=None)
        self.assertNotIn('schema_name', readonly)

    def test_primary_domain_returns_primary_domain_name(self):
        domain = MagicMock(is_primary=True, domain='dev.example.com')
        obj = MagicMock()
        obj.domains.all.return_value = [domain]
        self.assertEqual(self.admin.primary_domain(obj), 'dev.example.com')

    def test_primary_domain_skips_non_primary(self):
        secondary = MagicMock(is_primary=False, domain='old.example.com')
        obj = MagicMock()
        obj.domains.all.return_value = [secondary]
        self.assertEqual(self.admin.primary_domain(obj), '—')

    def test_primary_domain_no_domains_returns_dash(self):
        obj = MagicMock()
        obj.domains.all.return_value = []
        self.assertEqual(self.admin.primary_domain(obj), '—')


# ---------------------------------------------------------------------------
# PublicAdminSite.has_permission
# ---------------------------------------------------------------------------

class PublicAdminPermissionTest(TestCase):

    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, is_active=True, is_authenticated=True, is_superuser=False, role=None):
        request = self.factory.get('/')
        user = MagicMock()
        user.is_active = is_active
        user.is_authenticated = is_authenticated
        user.is_superuser = is_superuser
        user.role = role
        request.user = user
        return request

    def test_superuser_flag_grants_access(self):
        request = self._request(is_superuser=True, role='branch_admin')
        self.assertTrue(public_admin.has_permission(request))

    def test_superadmin_role_grants_access(self):
        request = self._request(is_superuser=False, role='superadmin')
        self.assertTrue(public_admin.has_permission(request))

    def test_network_admin_denied(self):
        request = self._request(role='network_admin')
        self.assertFalse(public_admin.has_permission(request))

    def test_branch_admin_denied(self):
        request = self._request(role='branch_admin')
        self.assertFalse(public_admin.has_permission(request))

    def test_inactive_user_denied(self):
        request = self._request(is_active=False, is_superuser=True)
        self.assertFalse(public_admin.has_permission(request))

    def test_unauthenticated_user_denied(self):
        request = self._request(is_authenticated=False)
        self.assertFalse(public_admin.has_permission(request))


# ---------------------------------------------------------------------------
# TenantAdminSite.has_permission
# ---------------------------------------------------------------------------

class TenantAdminPermissionTest(TestCase):

    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, role, is_superuser=False, company_id=None, tenant_pk=None):
        request = self.factory.get('/')
        user = MagicMock()
        user.is_active = True
        user.is_authenticated = True
        user.is_superuser = is_superuser
        user.role = role
        user.company_id = company_id
        request.user = user
        if tenant_pk is not None:
            tenant = MagicMock()
            tenant.pk = tenant_pk
            setattr(request, 'tenant', tenant)
        return request

    def test_superuser_can_access_any_tenant(self):
        request = self._request(role='superadmin', is_superuser=True, tenant_pk=99)
        self.assertTrue(tenant_admin.has_permission(request))

    def test_network_admin_own_tenant(self):
        request = self._request(role='network_admin', company_id=1, tenant_pk=1)
        request.user.companies.filter.return_value.exists.return_value = True
        self.assertTrue(tenant_admin.has_permission(request))

    def test_network_admin_foreign_tenant_denied(self):
        request = self._request(role='network_admin', company_id=1, tenant_pk=2)
        request.user.companies.filter.return_value.exists.return_value = False
        self.assertFalse(tenant_admin.has_permission(request))

    def test_branch_admin_own_tenant(self):
        # v5 has no branch_admin role — network_admin is the equivalent
        request = self._request(role='network_admin', company_id=5, tenant_pk=5)
        request.user.companies.filter.return_value.exists.return_value = True
        self.assertTrue(tenant_admin.has_permission(request))

    def test_no_tenant_on_request_denied(self):
        request = self._request(role='network_admin', company_id=1, tenant_pk=None)
        self.assertFalse(tenant_admin.has_permission(request))

    def test_superadmin_role_without_is_superuser_flag(self):
        request = self._request(role='superadmin', is_superuser=False, tenant_pk=1)
        self.assertTrue(tenant_admin.has_permission(request))


# ---------------------------------------------------------------------------
# beat_guard — гард фоновых задач по оплате/активности (волна 0, 16.09.2026)
# ---------------------------------------------------------------------------

from django.test import SimpleTestCase, override_settings  # noqa: E402

from .beat_guard import beat_tenants, grace_days, guard_mode, skip_reason  # noqa: E402


class BeatGuardSkipReasonTest(SimpleTestCase):
    """Чистая логика «пропускать ли сеть» — без БД и настроек."""

    today = date(2026, 9, 16)

    def test_inactive_is_skipped_regardless_of_payment(self):
        self.assertEqual(skip_reason(False, date(2030, 1, 1), self.today), 'inactive')

    def test_empty_paid_until_means_paid(self):
        self.assertEqual(skip_reason(True, None, self.today), '')

    def test_paid_in_future_is_fine(self):
        self.assertEqual(skip_reason(True, date(2026, 9, 18), self.today), '')

    def test_expired_inside_grace_is_still_fine(self):
        # истекло 5 дней назад, грейс 7 — ещё обслуживаем
        self.assertEqual(skip_reason(True, date(2026, 9, 11), self.today, grace=7), '')

    def test_expired_on_grace_boundary_is_still_fine(self):
        # ровно 7 дней назад — последний день грейса
        self.assertEqual(skip_reason(True, date(2026, 9, 9), self.today, grace=7), '')

    def test_expired_past_grace_is_skipped(self):
        reason = skip_reason(True, date(2026, 9, 8), self.today, grace=7)
        self.assertTrue(reason.startswith('paid_until=2026-09-08'))

    def test_zero_grace_skips_next_day(self):
        self.assertEqual(skip_reason(True, date(2026, 9, 16), self.today, grace=0), '')
        self.assertNotEqual(skip_reason(True, date(2026, 9, 15), self.today, grace=0), '')


class BeatGuardSettingsTest(SimpleTestCase):
    """Разбор режима и грейса: незнакомое → безопасное."""

    def test_default_mode_is_log(self):
        with override_settings(BEAT_TENANT_GUARD='log'):
            self.assertEqual(guard_mode(), 'log')

    def test_mode_is_normalised(self):
        with override_settings(BEAT_TENANT_GUARD='  ON '):
            self.assertEqual(guard_mode(), 'on')

    def test_unknown_mode_falls_back_to_log(self):
        with override_settings(BEAT_TENANT_GUARD='strict'):
            self.assertEqual(guard_mode(), 'log')
        with override_settings(BEAT_TENANT_GUARD=''):
            self.assertEqual(guard_mode(), 'log')

    def test_grace_days_parsing(self):
        with override_settings(BEAT_PAID_UNTIL_GRACE_DAYS='3'):
            self.assertEqual(grace_days(), 3)
        with override_settings(BEAT_PAID_UNTIL_GRACE_DAYS=-5):
            self.assertEqual(grace_days(), 0)
        with override_settings(BEAT_PAID_UNTIL_GRACE_DAYS='мусор'):
            self.assertEqual(grace_days(), 7)


class BeatTenantsQuerysetTest(TestCase):
    """Что именно попадает в WHERE в каждом режиме (без записей в Company).

    Смотрим только на условие: в списке колонок SELECT `is_active` и
    `paid_until` есть всегда, по ним судить нельзя.
    """

    @staticmethod
    def _where(qs) -> str:
        sql = str(qs.query)
        return sql.split('WHERE', 1)[1] if 'WHERE' in sql else ''

    def test_off_mode_only_excludes_public(self):
        with override_settings(BEAT_TENANT_GUARD='off'):
            where = self._where(beat_tenants())
        self.assertIn('public', where)
        self.assertNotIn('is_active', where)
        self.assertNotIn('paid_until', where)

    def test_log_mode_keeps_full_list(self):
        with override_settings(BEAT_TENANT_GUARD='log'):
            qs = beat_tenants()
            where = self._where(qs)
            self.assertEqual(list(qs), [])  # пустая таблица — просто не падает
        self.assertNotIn('is_active', where)
        self.assertNotIn('paid_until', where)

    def test_on_mode_filters_inactive_and_expired(self):
        with override_settings(BEAT_TENANT_GUARD='on', BEAT_PAID_UNTIL_GRACE_DAYS=7):
            where = self._where(beat_tenants())
        self.assertIn('is_active', where)
        self.assertIn('paid_until', where)
        self.assertIn('public', where)


# ── волна 3: сводная под платформенным JWT (контракт 3в.3) ───────────────────

from types import SimpleNamespace as _NS  # noqa: E402
from unittest import mock as _mock  # noqa: E402
from django.test import SimpleTestCase as _SimpleTestCase, override_settings as _override  # noqa: E402


class PlatformGateTest(_SimpleTestCase):

    def _request(self, token, superuser=False, role='client'):
        return _NS(auth=token, user=_NS(is_superuser=superuser, role=role))

    @_override(CHECKUP_TOKEN_EXCHANGE_MINUTES=60)
    def test_platform_token_passes_gate(self):
        from apps.shared.checkup.services import issue_exchange_token
        from apps.shared.clients.api.views import _is_platform, _may_see_platform
        fake = _mock.Mock(pk=5, username='checkup-1-dev', role='network_admin')
        platform_token, _ = issue_exchange_token(fake, 'dev', platform=True)
        plain_token, _ = issue_exchange_token(fake, 'dev')
        self.assertTrue(_is_platform(self._request(platform_token)))
        self.assertFalse(_is_platform(self._request(plain_token)))
        self.assertTrue(_may_see_platform(self._request(platform_token)))
        self.assertFalse(_may_see_platform(self._request(plain_token)))
        self.assertTrue(_may_see_platform(self._request(plain_token, superuser=True)))
        self.assertTrue(_may_see_platform(self._request(plain_token, role='superadmin')))

    def test_garbage_token_is_not_platform(self):
        from apps.shared.clients.api.views import _is_platform
        self.assertFalse(_is_platform(self._request('not-a-jwt')))
        self.assertFalse(_is_platform(self._request(None)))



class OverviewExportTest(_SimpleTestCase):

    def test_csv_has_bom_header_rows_and_totals(self):
        from apps.shared.clients.api.views import overview_rows_to_csv, EXPORT_COLUMNS
        rows = [{'name': 'LevOne', 'schema': 'levone', 'client_id': 1, 'domain': 'levone.levelupapp.ru',
                 'total_scans': 10, 'qr_scans': 8, 'pos_guests': 100, 'scan_index': 8.0, 'new_community': 2,
                 'new_newsletter': 1, 'stories': 0, 'reviews': 3, 'gift_cost': 150.5, 'service_cost': 1000.0,
                 'total_cost': 1150.5, 'sub_contacts': 3, 'unique_digitized': 2, 'cost_per_contact': 383.5,
                 'cost_per_unique': 575.25, 'no_cost_products': 0, 'gift_breakdown_title': 'Чизкейк ×2',
                 'gift_breakdown': [{'name': 'Чизкейк', 'qty': 2}], 'ok': True}]
        totals = {'total_scans': 10, 'gift_cost': 150.5}
        csv_text = overview_rows_to_csv(rows, totals, '2026-09-01', '2026-09-30')
        self.assertTrue(csv_text.startswith('\ufeff'))
        lines = csv_text.lstrip('\ufeff').split('\r\n')
        self.assertEqual(lines[1].split(';')[0], 'Клиент')
        self.assertEqual(len(lines[1].split(';')), len(EXPORT_COLUMNS))
        self.assertIn('LevOne;levone;1;levone.levelupapp.ru;10;8;100;8,00;', lines[2])
        self.assertIn('150,50', lines[2])
        self.assertTrue(lines[3].startswith('Итого;;;;10;'))

    @_override(CHECKUP_TOKEN_EXCHANGE_MINUTES=60)
    def test_view_requires_platform_and_returns_csv(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from apps.shared.checkup.services import issue_exchange_token
        from apps.shared.clients.api.views import CrossTenantOverviewExportView
        user = _NS(pk=5, username='checkup-1-dev', role='network_admin', is_superuser=False, is_authenticated=True, is_active=True)
        token, _ = issue_exchange_token(user, 'dev', platform=True)
        payload = {'rows': [{'name': 'A', 'schema': 'a', 'client_id': 1, 'domain': '', 'total_scans': 1}], 'totals': {'total_scans': 1}}
        with _mock.patch('apps.shared.clients.cross_stats.get_cross_tenant_overview', return_value=payload), \
             _mock.patch('django.core.cache.cache.get', return_value=None), \
             _mock.patch('django.core.cache.cache.set'):
            request = APIRequestFactory().get('/api/v1/overview/export/', {'period': '30d'})
            force_authenticate(request, user=user, token=token)
            resp = CrossTenantOverviewExportView.as_view()(request)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp['Content-Type'].startswith('text/csv'))
        self.assertIn('attachment; filename="loyalup-overview-', resp['Content-Disposition'])
        self.assertIn('Клиент;Сеть', resp.content.decode('utf-8'))

    def test_view_403_for_plain_user(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from apps.shared.clients.api.views import CrossTenantOverviewExportView
        user = _NS(pk=6, username='u', role='client', is_superuser=False, is_authenticated=True, is_active=True)
        request = APIRequestFactory().get('/api/v1/overview/export/', {'period': '30d'})
        force_authenticate(request, user=user, token='not-a-jwt')
        resp = CrossTenantOverviewExportView.as_view()(request)
        self.assertEqual((resp.status_code, resp.data['code']), (403, 'role_not_allowed'))



class SyncGiftCostsTest(_SimpleTestCase):

    def _platform_user(self):
        return _NS(pk=5, username='checkup-1-dev', role='network_admin', is_superuser=True, is_authenticated=True, is_active=True)

    def _post(self, data, user=None):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from apps.shared.clients.api.views import SyncGiftCostsView
        request = APIRequestFactory().post('/api/v1/overview/sync-gift-costs/', data, format='json')
        force_authenticate(request, user=user or self._platform_user(), token='t')
        return SyncGiftCostsView.as_view()(request)

    def test_parse_command_output(self):
        from apps.tenant.inventory import tasks as gift_tasks
        def fake_call(name, **kw):
            kw['stdout'].write('  levone: inventory +3, story +1, refill-zeros ~2\nЗАПИСАНО: всего inventory +3, story +1, обновлено нулевых 2\n')
        with _mock.patch('django.core.management.call_command', side_effect=fake_call):
            result = gift_tasks.run_gift_costs_sync('levone', commit=True)
        self.assertEqual((result['inventory'], result['story'], result['refilled_zeros'], result['schema']), (3, 1, 2, 'levone'))

    def test_plain_user_403(self):
        user = _NS(pk=6, username='u', role='client', is_superuser=False, is_authenticated=True, is_active=True)
        resp = self._post({'confirm': True}, user=user)
        self.assertEqual((resp.status_code, resp.data['code']), (403, 'role_not_allowed'))

    def test_confirm_required(self):
        resp = self._post({})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'confirm_required'))

    def test_queue_and_lock(self):
        from apps.tenant.inventory import tasks as gift_tasks
        store = {}
        def add(key, value, ttl=None):
            if key in store: return False
            store[key] = value; return True
        with _mock.patch('django.core.cache.cache.add', side_effect=add), \
             _mock.patch('django.core.cache.cache.get', side_effect=lambda k, d=None: store.get(k, d)), \
             _mock.patch('django.core.cache.cache.delete', side_effect=lambda k: store.pop(k, None)), \
             _mock.patch.object(gift_tasks.sync_gift_costs_task, 'delay', return_value=_NS(id='task-1')) as delay:
            resp = self._post({'confirm': True, 'schema': 'levone'})
            self.assertEqual(resp.status_code, 202, resp.data)
            self.assertEqual((resp.data['task_id'], resp.data['scope']), ('task-1', 'levone'))
            delay.assert_called_once_with('levone', 'checkup-1-dev')
            resp2 = self._post({'confirm': True})
            self.assertEqual((resp2.status_code, resp2.data['code']), (409, 'already_running'))

    def test_broker_down_releases_lock(self):
        from apps.tenant.inventory import tasks as gift_tasks
        with _mock.patch('django.core.cache.cache.add', return_value=True), \
             _mock.patch('django.core.cache.cache.delete') as delete, \
             _mock.patch.object(gift_tasks.sync_gift_costs_task, 'delay', side_effect=RuntimeError('broker down')):
            resp = self._post({'confirm': True})
        self.assertEqual((resp.status_code, resp.data['code']), (503, 'queue_unavailable'))
        delete.assert_called_once_with(gift_tasks.LOCK_KEY)

    def test_dry_run_is_sync(self):
        from apps.tenant.inventory import tasks as gift_tasks
        with _mock.patch.object(gift_tasks, 'run_gift_costs_sync', return_value={'inventory': 1, 'story': 0, 'refilled_zeros': 0, 'commit': False, 'schema': 'all', 'output_tail': ''}) as run:
            resp = self._post({'dry_run': True})
        self.assertEqual((resp.status_code, resp.data['dry_run'], resp.data['inventory']), (200, True, 1))
        run.assert_called_once_with(None, commit=False)

    def test_status_view(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from apps.shared.clients.api.views import SyncGiftCostsStatusView
        with _mock.patch('django.core.cache.cache.get', side_effect=lambda k, d=None: {'gift_costs:sync:lock': None, 'gift_costs:sync:last': {'ok': True}}.get(k, d)):
            request = APIRequestFactory().get('/api/v1/overview/sync-gift-costs/status/')
            force_authenticate(request, user=self._platform_user(), token='t')
            resp = SyncGiftCostsStatusView.as_view()(request)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((resp.data['running'], resp.data['last_run']), (False, {'ok': True}))

    def test_task_records_result_and_releases_lock(self):
        from apps.tenant.inventory import tasks as gift_tasks
        store = {}
        with _mock.patch.object(gift_tasks, 'run_gift_costs_sync', return_value={'inventory': 2, 'story': 0, 'refilled_zeros': 0}), \
             _mock.patch('django.core.cache.cache.set', side_effect=lambda k, v, t=None: store.__setitem__(k, v)), \
             _mock.patch('django.core.cache.cache.delete', side_effect=lambda k: store.pop(k, None)), \
             _mock.patch('apps.shared.clients.cross_stats.invalidate_overview_cache') as inv:
            record = gift_tasks.sync_gift_costs_task.run('levone', 'owner')
        self.assertTrue(record['ok'])
        self.assertEqual(store[gift_tasks.LAST_KEY]['result']['inventory'], 2)
        inv.assert_called_once()
