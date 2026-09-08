from datetime import date
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIRequestFactory

from apps.shared.clients.models import Company

from .serializers import TenantDomainResponseSerializer
from .services import (
    CompanyExpired,
    CompanyInactive,
    CompanyNotFound,
    get_tenant_domain,
)
from .views import TenantDomainView


# ── Service ───────────────────────────────────────────────────────────────────

class GetTenantDomainServiceTest(TestCase):

    def _active_company(self, name='Ресторан', is_active=True, paid_until=date(2099, 12, 31)):
        # MagicMock(name=...) sets internal mock name, not the .name attribute.
        # Assign .name separately to get the actual string value.
        company = MagicMock(is_active=is_active, paid_until=paid_until)
        company.name = name
        return company

    @patch('apps.shared.clients.api.services.Domain.objects')
    @patch('apps.shared.clients.api.services.Company.objects')
    def test_returns_domain_and_name_on_success(self, mock_co, mock_dom):
        company = self._active_company(name='Бургер Клаб')
        mock_co.get.return_value = company
        domain = MagicMock(domain='burger.localhost')
        mock_dom.filter.return_value.first.return_value = domain

        result = get_tenant_domain(1)

        self.assertEqual(result['domain'], 'burger.localhost')
        self.assertEqual(result['name'], 'Бургер Клаб')

    @patch('apps.shared.clients.api.services.Company.objects')
    def test_raises_company_not_found(self, mock_co):
        mock_co.get.side_effect = Company.DoesNotExist

        with self.assertRaises(CompanyNotFound):
            get_tenant_domain(999)

    @patch('apps.shared.clients.api.services.Company.objects')
    def test_raises_company_inactive(self, mock_co):
        mock_co.get.return_value = self._active_company(is_active=False)

        with self.assertRaises(CompanyInactive):
            get_tenant_domain(1)

    @patch('apps.shared.clients.api.services.Company.objects')
    def test_raises_company_expired(self, mock_co):
        mock_co.get.return_value = self._active_company(paid_until=date(2000, 1, 1))

        with self.assertRaises(CompanyExpired):
            get_tenant_domain(1)

    @patch('apps.shared.clients.api.services.Company.objects')
    def test_paid_until_today_is_still_active(self, mock_co):
        """paid_until == today: последний день подписки, сервис ещё работает."""
        from django.utils import timezone
        today = timezone.localdate()
        mock_co.get.return_value = self._active_company(paid_until=today)

        with patch('apps.shared.clients.api.services.Domain.objects') as mock_dom:
            mock_dom.filter.return_value.first.return_value = MagicMock(domain='x.localhost')
            result = get_tenant_domain(1)

        self.assertIsNotNone(result)

    @patch('apps.shared.clients.api.services.Domain.objects')
    @patch('apps.shared.clients.api.services.Company.objects')
    def test_falls_back_to_any_domain_when_no_primary(self, mock_co, mock_dom):
        mock_co.get.return_value = self._active_company()
        fallback = MagicMock(domain='fallback.localhost')
        # Первый вызов (is_primary=True) → None, второй (любой) → fallback
        mock_dom.filter.return_value.first.side_effect = [None, fallback]

        result = get_tenant_domain(1)

        self.assertEqual(result['domain'], 'fallback.localhost')

    @patch('apps.shared.clients.api.services.Domain.objects')
    @patch('apps.shared.clients.api.services.Company.objects')
    def test_domain_is_none_when_no_domains_configured(self, mock_co, mock_dom):
        mock_co.get.return_value = self._active_company()
        mock_dom.filter.return_value.first.return_value = None

        result = get_tenant_domain(1)

        self.assertIsNone(result['domain'])

    @patch('apps.shared.clients.api.services.Domain.objects')
    @patch('apps.shared.clients.api.services.Company.objects')
    def test_calls_company_get_with_correct_client_id(self, mock_co, mock_dom):
        mock_co.get.return_value = self._active_company()
        mock_dom.filter.return_value.first.return_value = MagicMock(domain='x.localhost')

        get_tenant_domain(42)

        mock_co.get.assert_called_once_with(client_id=42)

    # ── Флаги входа (этап 1 «Антихрупкость входа») ────────────────────────────

    @patch('apps.shared.clients.api.services.Domain.objects')
    @patch('apps.shared.clients.api.services.Company.objects')
    def test_entry_flags_come_from_client_config(self, mock_co, mock_dom):
        company = self._active_company()
        company.config = MagicMock(web_entry_enabled=True, degrade_enabled=True)
        mock_co.get.return_value = company
        mock_dom.filter.return_value.first.return_value = MagicMock(domain='x.localhost')

        result = get_tenant_domain(1)

        self.assertTrue(result['web_entry_enabled'])
        self.assertTrue(result['degrade_enabled'])

    @patch('apps.shared.clients.api.services.Domain.objects')
    @patch('apps.shared.clients.api.services.Company.objects')
    def test_entry_flags_are_false_by_default(self, mock_co, mock_dom):
        company = self._active_company()
        company.config = MagicMock(web_entry_enabled=False, degrade_enabled=False)
        mock_co.get.return_value = company
        mock_dom.filter.return_value.first.return_value = MagicMock(domain='x.localhost')

        result = get_tenant_domain(1)

        self.assertFalse(result['web_entry_enabled'])
        self.assertFalse(result['degrade_enabled'])

    @patch('apps.shared.clients.api.services.Domain.objects')
    @patch('apps.shared.clients.api.services.Company.objects')
    def test_entry_flags_are_false_without_client_config(self, mock_co, mock_dom):
        """
        У компании нет ClientConfig: обратный OneToOne кидает
        RelatedObjectDoesNotExist (подкласс AttributeError). Эмулируем моком со
        spec — у него обращение к незаявленному атрибуту тоже AttributeError.
        """
        company = MagicMock(spec=['is_active', 'paid_until', 'name'])
        company.is_active = True
        company.paid_until = date(2099, 12, 31)
        company.name = 'Без конфига'
        mock_co.get.return_value = company
        mock_dom.filter.return_value.first.return_value = MagicMock(domain='x.localhost')

        result = get_tenant_domain(1)

        self.assertFalse(result['web_entry_enabled'])
        self.assertFalse(result['degrade_enabled'])

    @patch('apps.shared.clients.api.services.Domain.objects')
    @patch('apps.shared.clients.api.services.Company.objects')
    def test_flags_are_independent(self, mock_co, mock_dom):
        company = self._active_company()
        company.config = MagicMock(web_entry_enabled=True, degrade_enabled=False)
        mock_co.get.return_value = company
        mock_dom.filter.return_value.first.return_value = MagicMock(domain='x.localhost')

        result = get_tenant_domain(1)

        self.assertTrue(result['web_entry_enabled'])
        self.assertFalse(result['degrade_enabled'])


# ── View ──────────────────────────────────────────────────────────────────────

class TenantDomainViewTest(TestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = TenantDomainView.as_view()

    def _get(self, client_id=1):
        request = self.factory.get('/')
        return self.view(request, client_id=client_id)

    @patch('apps.shared.clients.api.views.get_tenant_domain')
    def test_returns_200_on_success(self, mock_service):
        mock_service.return_value = {'domain': 'rest.localhost', 'name': 'Ресторан'}

        response = self._get(client_id=1)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @patch('apps.shared.clients.api.views.get_tenant_domain')
    def test_response_contains_domain_and_name(self, mock_service):
        mock_service.return_value = {'domain': 'rest.localhost', 'name': 'Ресторан'}

        response = self._get(client_id=1)

        self.assertEqual(response.data['domain'], 'rest.localhost')
        self.assertEqual(response.data['name'], 'Ресторан')

    @patch('apps.shared.clients.api.views.get_tenant_domain')
    def test_returns_404_when_company_not_found(self, mock_service):
        mock_service.side_effect = CompanyNotFound

        response = self._get(client_id=999)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('detail', response.data)

    @patch('apps.shared.clients.api.views.get_tenant_domain')
    def test_returns_403_when_company_inactive(self, mock_service):
        mock_service.side_effect = CompanyInactive

        response = self._get()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('detail', response.data)

    @patch('apps.shared.clients.api.views.get_tenant_domain')
    def test_returns_403_when_company_expired(self, mock_service):
        mock_service.side_effect = CompanyExpired

        response = self._get()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('detail', response.data)

    @patch('apps.shared.clients.api.views.get_tenant_domain')
    def test_passes_client_id_to_service(self, mock_service):
        mock_service.return_value = {'domain': 'x.localhost', 'name': 'X'}

        self._get(client_id=42)

        mock_service.assert_called_once_with(42)

    @patch('apps.shared.clients.api.views.get_tenant_domain')
    def test_response_contains_entry_flags(self, mock_service):
        """Флаги входа фронт читает ДО резолва домена — они в этом же ответе."""
        mock_service.return_value = {
            'domain': 'rest.localhost', 'name': 'Ресторан',
            'web_entry_enabled': True, 'degrade_enabled': True,
        }

        response = self._get(client_id=1)

        self.assertIs(response.data['web_entry_enabled'], True)
        self.assertIs(response.data['degrade_enabled'], True)

    @patch('apps.shared.clients.api.views.get_tenant_domain')
    def test_entry_flags_off_by_default_in_response(self, mock_service):
        mock_service.return_value = {
            'domain': 'rest.localhost', 'name': 'Ресторан',
            'web_entry_enabled': False, 'degrade_enabled': False,
        }

        response = self._get(client_id=1)

        self.assertIs(response.data['web_entry_enabled'], False)
        self.assertIs(response.data['degrade_enabled'], False)
        # Старые поля на месте — ответ только дополнен.
        self.assertEqual(response.data['domain'], 'rest.localhost')
        self.assertEqual(response.data['name'], 'Ресторан')

    @patch('apps.shared.clients.api.views.get_tenant_domain')
    def test_inactive_and_expired_return_different_messages(self, mock_service):
        messages = {}
        for exc, key in [(CompanyInactive, 'inactive'), (CompanyExpired, 'expired')]:
            mock_service.side_effect = exc
            response = self._get()
            messages[key] = response.data['detail']

        self.assertNotEqual(messages['inactive'], messages['expired'])


# ── Serializer ────────────────────────────────────────────────────────────────

class TenantDomainResponseSerializerTest(TestCase):

    FIELDS = {'domain', 'name', 'web_entry_enabled', 'degrade_enabled'}

    def _make(self, domain='rest.localhost', name='Ресторан', **flags):
        return TenantDomainResponseSerializer({'domain': domain, 'name': name, **flags})

    def test_has_domain_field(self):
        self.assertIn('domain', TenantDomainResponseSerializer().fields)

    def test_has_name_field(self):
        self.assertIn('name', TenantDomainResponseSerializer().fields)

    def test_has_entry_flag_fields(self):
        fields = TenantDomainResponseSerializer().fields
        self.assertIn('web_entry_enabled', fields)
        self.assertIn('degrade_enabled', fields)

    def test_serializes_domain_correctly(self):
        self.assertEqual(self._make(domain='burger.localhost').data['domain'], 'burger.localhost')

    def test_serializes_name_correctly(self):
        self.assertEqual(self._make(name='Бургер Клаб').data['name'], 'Бургер Клаб')

    def test_serializes_entry_flags(self):
        data = self._make(web_entry_enabled=True, degrade_enabled=False).data
        self.assertIs(data['web_entry_enabled'], True)
        self.assertIs(data['degrade_enabled'], False)

    def test_missing_flags_serialize_as_false(self):
        """Ключей нет во входном словаре → отдаём False, а не падаем."""
        data = self._make().data
        self.assertIs(data['web_entry_enabled'], False)
        self.assertIs(data['degrade_enabled'], False)

    def test_no_extra_fields(self):
        self.assertEqual(set(self._make().data.keys()), self.FIELDS)
