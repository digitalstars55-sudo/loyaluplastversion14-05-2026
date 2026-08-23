"""
Tests for apps.tenant.branch.api.

Service tests:  mock ORM managers, test business logic and exceptions.
View tests:     mock service layer, test HTTP status codes and routing.
Serializer tests: mock domain objects, test field presence and output shape.
"""

import json
from unittest.mock import MagicMock, call, patch

from django.test import TestCase
from django.urls import reverse, resolve
from rest_framework import status
from rest_framework.test import APIRequestFactory

from apps.tenant.branch.models import Branch, ClientBranch

from .serializers import (
    BranchInfoSerializer,
    ClientProfileResponseSerializer,
    CoinTransactionSerializer,
    EmployeeSerializer,
    PromotionSerializer,
    VKAuthRequestSerializer,
)
from .services import (
    BranchInactive, BranchNotFound, ClientBlocked, ClientNotFound, VKAuthError,
    get_branch_info, get_client_profile,
    get_employees, get_promotions, get_transactions,
    register_or_get_client, update_client_profile, vk_oauth_exchange, vk_web_auth,
)
from .views import BranchInfoView, ClientView, EmployeeView, PromotionView, TransactionsView, VKAuthView


# ── Helpers ────────────────────────────────────────────────────────────────────

def _urlopen_mock(*response_dicts):
    """
    Builds a mock for urllib.request.urlopen that works as a context manager
    and returns each response_dict as JSON bytes on successive calls.
    """
    side_effects = []
    for d in response_dicts:
        m = MagicMock()
        m.__enter__ = MagicMock(return_value=m)
        m.__exit__ = MagicMock(return_value=False)
        m.read.return_value = json.dumps(d).encode()
        side_effects.append(m)
    mock = MagicMock(side_effect=side_effects)
    return mock


_VK_TOKEN_OK = {
    'access_token': 'tok_abc123',
    'user_id': 123456,
}
_VK_USER_OK = {
    'user': {
        'user_id': '123456',
        'first_name': 'Иван',
        'last_name': 'Иванов',
        'avatar': 'https://vk.com/photo.jpg',
    }
}
_VK_AUTH_PAYLOAD = {
    'code': 'AUTH_CODE',
    'device_id': 'DEV_ID',
    'code_verifier': 'VERIFIER_STRING',
    'redirect_uri': 'https://example.com/callback',
    'state': 'STATE_STRING',
    'branch_id': 42,
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _branch_data(**overrides):
    data = {
        'id': 1, 'branch_id': 42, 'name': 'Бургер Клаб',
        'address': 'ул. Ленина, 1', 'phone': '+7 900 000 0000',
        'yandex_map': '', 'gis_map': '',
        'logotype_url': None, 'coin_icon_url': None,
        'vk_group_id': None, 'vk_group_name': None,
        'story_image_url': None,
    }
    data.update(overrides)
    return data


def _profile_mock(**overrides):
    """Minimal ClientBranch mock compatible with response serializers."""
    client = MagicMock()
    client.vk_id = 111
    client.first_name = 'Иван'
    client.last_name = 'Иванов'
    client.photo_url = ''

    p = MagicMock()
    p.id = 1
    p.birth_date = None
    p.is_employee = False
    p.client = client
    p._coins_balance = 0
    p.vk_status = None  # prevents auto-mock attribute from being truthy
    for k, v in overrides.items():
        setattr(p, k, v)
    return p


# ── Service: get_branch_info ──────────────────────────────────────────────────

class GetBranchInfoTest(TestCase):

    @patch('apps.tenant.branch.api.services.Branch.objects')
    def test_raises_branch_not_found(self, mock_objs):
        mock_objs.select_related.return_value.get.side_effect = Branch.DoesNotExist

        with self.assertRaises(BranchNotFound):
            get_branch_info(999)

    @patch('apps.tenant.branch.api.services.Branch.objects')
    def test_raises_branch_inactive(self, mock_objs):
        mock_objs.select_related.return_value.get.return_value = MagicMock(is_active=False)

        with self.assertRaises(BranchInactive):
            get_branch_info(1)

    @patch('apps.tenant.branch.api.services.Branch.objects')
    def test_returns_null_config_fields_without_tenant(self, mock_objs):
        branch = MagicMock(is_active=True, pk=1, branch_id=42)
        branch.name = 'Бургер Клаб'
        mock_objs.select_related.return_value.get.return_value = branch

        result = get_branch_info(42, tenant=None)

        self.assertIsNone(result['logotype_url'])
        self.assertIsNone(result['coin_icon_url'])
        self.assertIsNone(result['vk_group_id'])
        self.assertIsNone(result['vk_group_name'])

    @patch('apps.shared.config.models.ClientConfig')
    @patch('apps.tenant.branch.api.services.Branch.objects')
    def test_returns_config_fields_with_tenant(self, mock_objs, mock_cc):
        branch = MagicMock(is_active=True, pk=1, branch_id=42)
        branch.name = 'Бургер Клаб'
        mock_objs.select_related.return_value.get.return_value = branch

        config = MagicMock(vk_group_id=777, vk_group_name='burger_club')
        logo = MagicMock()
        logo.name = 'logo.png'
        logo.url = '/media/logo.png'
        config.logotype_image = logo
        coin = MagicMock()
        coin.name = None
        config.coin_image = coin
        mock_cc.objects.get.return_value = config

        result = get_branch_info(42, tenant=MagicMock())

        self.assertEqual(result['vk_group_id'], 777)
        self.assertEqual(result['logotype_url'], '/media/logo.png')
        self.assertIsNone(result['coin_icon_url'])


# ── Service: get_client_profile ───────────────────────────────────────────────

class GetClientProfileTest(TestCase):

    @patch('apps.tenant.branch.api.services._profile_qs')
    def test_raises_client_not_found(self, mock_qs):
        mock_qs.return_value.get.side_effect = ClientBranch.DoesNotExist

        with self.assertRaises(ClientNotFound):
            get_client_profile(vk_id=1, branch_id=99)

    @patch('apps.tenant.branch.api.services._profile_qs')
    def test_raises_client_blocked_for_inactive_guest(self, mock_qs):
        profile = _profile_mock()
        profile.client.is_active = False
        mock_qs.return_value.get.return_value = profile

        with self.assertRaises(ClientBlocked):
            get_client_profile(vk_id=111, branch_id=42)

    @patch('apps.tenant.branch.api.services._profile_qs')
    def test_returns_profile_on_success(self, mock_qs):
        expected = _profile_mock()
        mock_qs.return_value.get.return_value = expected

        result = get_client_profile(vk_id=111, branch_id=42)

        self.assertIs(result, expected)
        mock_qs.return_value.get.assert_called_once_with(
            client__vk_id=111, branch__branch_id=42,
        )


# ── Service: register_or_get_client ───────────────────────────────────────────

class RegisterOrGetClientTest(TestCase):

    @patch('apps.tenant.branch.api.services.Branch.objects')
    def test_raises_branch_not_found(self, mock_objs):
        mock_objs.get.side_effect = Branch.DoesNotExist

        with self.assertRaises(BranchNotFound):
            register_or_get_client(vk_id=1, branch_id=999)

    @patch('apps.tenant.branch.api.services.Branch.objects')
    def test_raises_branch_inactive(self, mock_objs):
        mock_objs.get.return_value = MagicMock(is_active=False)

        with self.assertRaises(BranchInactive):
            register_or_get_client(vk_id=1, branch_id=1)

    @patch('apps.tenant.branch.api.services.Client.objects')
    @patch('apps.tenant.branch.api.services.Branch.objects')
    def test_raises_client_blocked(self, mock_br, mock_cl):
        mock_br.get.return_value = MagicMock(is_active=True)
        mock_cl.get_or_create.return_value = (MagicMock(is_active=False), False)

        with self.assertRaises(ClientBlocked):
            register_or_get_client(vk_id=111, branch_id=1)

    @patch('apps.tenant.branch.api.services._sync_vk_status_on_register')
    @patch('apps.tenant.branch.api.services.ClientBranchVisit.record_visit')
    @patch('apps.tenant.branch.api.services._profile_qs')
    @patch('apps.tenant.branch.api.services.ClientBranch.objects')
    @patch('apps.tenant.branch.api.services.Client.objects')
    @patch('apps.tenant.branch.api.services.Branch.objects')
    def test_returns_created_true_for_new_client(self, mock_br, mock_cl, mock_cb, mock_pqs, mock_visit, mock_sync):
        mock_br.get.return_value = MagicMock(is_active=True)
        mock_cl.get_or_create.return_value = (MagicMock(is_active=True), True)
        raw = MagicMock()
        mock_cb.get_or_create.return_value = (raw, True)
        mock_pqs.return_value.get.return_value = _profile_mock()

        _, created = register_or_get_client(vk_id=111, branch_id=1)

        self.assertTrue(created)

    @patch('apps.tenant.branch.api.services.ClientBranchVisit.record_visit')
    @patch('apps.tenant.branch.api.services._profile_qs')
    @patch('apps.tenant.branch.api.services.ClientBranch.objects')
    @patch('apps.tenant.branch.api.services.Client.objects')
    @patch('apps.tenant.branch.api.services.Branch.objects')
    def test_returns_created_false_for_existing_client(self, mock_br, mock_cl, mock_cb, mock_pqs, mock_visit):
        mock_br.get.return_value = MagicMock(is_active=True)
        existing = MagicMock(is_active=True, first_name='Иван', last_name='Иванов', photo_url='')
        mock_cl.get_or_create.return_value = (existing, False)
        mock_cb.get_or_create.return_value = (MagicMock(), False)
        mock_pqs.return_value.get.return_value = _profile_mock()

        _, created = register_or_get_client(
            vk_id=111, branch_id=1, first_name='Иван', last_name='Иванов', photo_url='',
        )

        self.assertFalse(created)

    @patch('apps.tenant.branch.api.services._sync_vk_status_on_register')
    @patch('apps.tenant.branch.api.services.ClientBranchVisit.record_visit')
    @patch('apps.tenant.branch.api.services._profile_qs')
    @patch('apps.tenant.branch.api.services.ClientBranch.objects')
    @patch('apps.tenant.branch.api.services.Client.objects')
    @patch('apps.tenant.branch.api.services.Branch.objects')
    def test_records_visit(self, mock_br, mock_cl, mock_cb, mock_pqs, mock_visit, mock_sync):
        mock_br.get.return_value = MagicMock(is_active=True)
        mock_cl.get_or_create.return_value = (MagicMock(is_active=True), True)
        raw = MagicMock()
        mock_cb.get_or_create.return_value = (raw, True)
        mock_pqs.return_value.get.return_value = _profile_mock()

        register_or_get_client(vk_id=111, branch_id=1)

        mock_visit.assert_called_once_with(raw)


# ── Service: update_client_profile ────────────────────────────────────────────

class UpdateClientProfileTest(TestCase):

    @patch('apps.tenant.branch.api.services.get_client_profile')
    def test_raises_client_not_found(self, mock_get):
        mock_get.side_effect = ClientNotFound

        with self.assertRaises(ClientNotFound):
            update_client_profile(vk_id=1, branch_id=1)

    @patch('apps.tenant.branch.api.services._profile_qs')
    @patch('apps.tenant.branch.api.services.get_client_profile')
    def test_updates_birth_date(self, mock_get, mock_pqs):
        profile = MagicMock()
        mock_get.return_value = profile
        mock_pqs.return_value.get.return_value = _profile_mock()

        update_client_profile(vk_id=1, branch_id=1, birth_date='1990-01-01')

        self.assertEqual(profile.birth_date, '1990-01-01')
        profile.save.assert_called_once_with(update_fields=['birth_date'])

    @patch('apps.tenant.branch.api.services._profile_qs')
    @patch('apps.tenant.branch.api.services.get_client_profile')
    def test_updates_client_first_name(self, mock_get, mock_pqs):
        profile = MagicMock()
        mock_get.return_value = profile
        mock_pqs.return_value.get.return_value = _profile_mock()

        update_client_profile(vk_id=1, branch_id=1, first_name='Пётр')

        self.assertEqual(profile.client.first_name, 'Пётр')
        profile.client.save.assert_called_once_with(update_fields=['first_name'])

    @patch('apps.tenant.branch.api.services.ClientVKStatus.objects')
    @patch('apps.tenant.branch.api.services._profile_qs')
    @patch('apps.tenant.branch.api.services.get_client_profile')
    def test_marks_community_subscription_via_app(self, mock_get, mock_pqs, mock_vk):
        profile = MagicMock()
        mock_get.return_value = profile
        vk_status = MagicMock()
        mock_vk.get_or_create.return_value = (vk_status, False)
        mock_pqs.return_value.get.return_value = _profile_mock()

        update_client_profile(vk_id=1, branch_id=1, community_via_app=True)

        vk_status.mark_subscribed.assert_called_once_with(community=True, newsletter=False)


# ── Service: get_employees ────────────────────────────────────────────────────

class GetEmployeesTest(TestCase):

    @patch('apps.tenant.branch.api.services.Branch.objects')
    def test_raises_branch_not_found(self, mock_objs):
        mock_objs.get.side_effect = Branch.DoesNotExist

        with self.assertRaises(BranchNotFound):
            get_employees(branch_id=999)

    @patch('apps.tenant.branch.api.services.Branch.objects')
    def test_raises_branch_inactive(self, mock_objs):
        mock_objs.get.return_value = MagicMock(is_active=False)

        with self.assertRaises(BranchInactive):
            get_employees(branch_id=1)

    @patch('apps.tenant.branch.api.services._profile_qs')
    @patch('apps.tenant.branch.api.services.Branch.objects')
    def test_filters_by_branch_and_is_employee(self, mock_objs, mock_pqs):
        branch = MagicMock(is_active=True)
        mock_objs.get.return_value = branch
        employees_qs = MagicMock()
        mock_pqs.return_value.filter.return_value = employees_qs

        result = get_employees(branch_id=1)

        mock_pqs.return_value.filter.assert_called_once_with(branch=branch, is_employee=True)
        self.assertIs(result, employees_qs)


# ── Service: get_promotions ───────────────────────────────────────────────────

class GetPromotionsTest(TestCase):

    @patch('apps.tenant.branch.api.services.Branch.objects')
    def test_raises_branch_not_found(self, mock_objs):
        mock_objs.get.side_effect = Branch.DoesNotExist

        with self.assertRaises(BranchNotFound):
            get_promotions(branch_id=999)

    @patch('apps.tenant.branch.api.services.Branch.objects')
    def test_raises_branch_inactive(self, mock_objs):
        mock_objs.get.return_value = MagicMock(is_active=False)

        with self.assertRaises(BranchInactive):
            get_promotions(branch_id=1)

    @patch('apps.tenant.branch.api.services.Promotions.objects')
    @patch('apps.tenant.branch.api.services.Branch.objects')
    def test_returns_promotions_for_branch(self, mock_objs, mock_promo):
        branch = MagicMock(is_active=True)
        mock_objs.get.return_value = branch
        promo_qs = MagicMock()
        mock_promo.filter.return_value = promo_qs

        result = get_promotions(branch_id=1)

        mock_promo.filter.assert_called_once_with(branch=branch)
        self.assertIs(result, promo_qs)


# ── Service: get_transactions ─────────────────────────────────────────────────

class GetTransactionsTest(TestCase):

    @patch('apps.tenant.branch.api.services.get_client_profile')
    def test_raises_client_not_found(self, mock_get):
        mock_get.side_effect = ClientNotFound

        with self.assertRaises(ClientNotFound):
            get_transactions(vk_id=1, branch_id=1)

    @patch('apps.tenant.branch.api.services.CoinTransaction.objects')
    @patch('apps.tenant.branch.api.services.get_client_profile')
    def test_returns_transactions_for_profile(self, mock_get, mock_tx):
        profile = _profile_mock()
        mock_get.return_value = profile
        tx_qs = MagicMock()
        mock_tx.filter.return_value = tx_qs

        result = get_transactions(vk_id=111, branch_id=1)

        mock_tx.filter.assert_called_once_with(client=profile)
        self.assertIs(result, tx_qs)


# ── View: BranchInfoView ──────────────────────────────────────────────────────

class BranchInfoViewTest(TestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = BranchInfoView.as_view()

    def _get(self, branch_id=42):
        return self.view(self.factory.get('/'), branch_id=branch_id)

    @patch('apps.tenant.branch.api.views.get_branch_info')
    def test_returns_200_on_success(self, mock_svc):
        mock_svc.return_value = _branch_data()
        self.assertEqual(self._get().status_code, status.HTTP_200_OK)

    @patch('apps.tenant.branch.api.views.get_branch_info')
    def test_response_contains_branch_name(self, mock_svc):
        mock_svc.return_value = _branch_data(name='Суши Бар')
        self.assertEqual(self._get().data['name'], 'Суши Бар')

    @patch('apps.tenant.branch.api.views.get_branch_info')
    def test_returns_404_on_branch_not_found(self, mock_svc):
        mock_svc.side_effect = BranchNotFound
        response = self._get()
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('detail', response.data)

    @patch('apps.tenant.branch.api.views.get_branch_info')
    def test_returns_403_on_branch_inactive(self, mock_svc):
        mock_svc.side_effect = BranchInactive
        response = self._get()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('detail', response.data)

    @patch('apps.tenant.branch.api.views.get_branch_info')
    def test_passes_branch_id_to_service(self, mock_svc):
        mock_svc.return_value = _branch_data()
        self._get(branch_id=99)
        self.assertEqual(mock_svc.call_args[0][0], 99)


# ── View: ClientView GET ──────────────────────────────────────────────────────

class ClientViewGetTest(TestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = ClientView.as_view()

    def _get(self, **params):
        return self.view(self.factory.get('/', params))

    @patch('apps.tenant.branch.api.views.get_client_profile')
    def test_returns_200_on_success(self, mock_svc):
        mock_svc.return_value = _profile_mock()
        self.assertEqual(self._get(vk_id=111, branch_id=42).status_code, status.HTTP_200_OK)

    @patch('apps.tenant.branch.api.views.get_client_profile')
    def test_returns_404_when_not_found(self, mock_svc):
        mock_svc.side_effect = ClientNotFound
        self.assertEqual(self._get(vk_id=999, branch_id=42).status_code, status.HTTP_404_NOT_FOUND)

    def test_returns_400_when_missing_vk_id(self):
        self.assertEqual(self._get(branch_id=42).status_code, status.HTTP_400_BAD_REQUEST)

    def test_returns_400_when_missing_branch_id(self):
        self.assertEqual(self._get(vk_id=111).status_code, status.HTTP_400_BAD_REQUEST)

    @patch('apps.tenant.branch.api.views.get_client_profile')
    def test_passes_params_to_service(self, mock_svc):
        mock_svc.return_value = _profile_mock()
        self._get(vk_id=555, branch_id=33)
        mock_svc.assert_called_once_with(vk_id=555, branch_id=33)


# ── View: ClientView POST ─────────────────────────────────────────────────────

class ClientViewPostTest(TestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = ClientView.as_view()

    def _post(self, data):
        return self.view(self.factory.post('/', data, format='json'))

    @patch('apps.tenant.branch.api.views.register_or_get_client')
    def test_returns_201_when_created(self, mock_svc):
        mock_svc.return_value = (_profile_mock(), True)
        self.assertEqual(self._post({'vk_id': 111, 'branch_id': 42}).status_code, status.HTTP_201_CREATED)

    @patch('apps.tenant.branch.api.views.register_or_get_client')
    def test_returns_200_when_existing(self, mock_svc):
        mock_svc.return_value = (_profile_mock(), False)
        self.assertEqual(self._post({'vk_id': 111, 'branch_id': 42}).status_code, status.HTTP_200_OK)

    @patch('apps.tenant.branch.api.views.register_or_get_client')
    def test_returns_404_on_branch_not_found(self, mock_svc):
        mock_svc.side_effect = BranchNotFound
        self.assertEqual(self._post({'vk_id': 111, 'branch_id': 999}).status_code, status.HTTP_404_NOT_FOUND)

    @patch('apps.tenant.branch.api.views.register_or_get_client')
    def test_returns_403_on_branch_inactive(self, mock_svc):
        mock_svc.side_effect = BranchInactive
        self.assertEqual(self._post({'vk_id': 111, 'branch_id': 42}).status_code, status.HTTP_403_FORBIDDEN)

    @patch('apps.tenant.branch.api.views.register_or_get_client')
    def test_returns_403_on_client_blocked(self, mock_svc):
        mock_svc.side_effect = ClientBlocked
        self.assertEqual(self._post({'vk_id': 111, 'branch_id': 42}).status_code, status.HTTP_403_FORBIDDEN)

    def test_returns_400_when_missing_vk_id(self):
        self.assertEqual(self._post({'branch_id': 42}).status_code, status.HTTP_400_BAD_REQUEST)


# ── View: ClientView PATCH ────────────────────────────────────────────────────

class ClientViewPatchTest(TestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = ClientView.as_view()

    def _patch(self, data):
        return self.view(self.factory.patch('/', data, format='json'))

    @patch('apps.tenant.branch.api.views.update_client_profile')
    def test_returns_200_on_success(self, mock_svc):
        mock_svc.return_value = _profile_mock()
        self.assertEqual(self._patch({'vk_id': 111, 'branch_id': 42}).status_code, status.HTTP_200_OK)

    @patch('apps.tenant.branch.api.views.update_client_profile')
    def test_returns_404_when_not_found(self, mock_svc):
        mock_svc.side_effect = ClientNotFound
        response = self._patch({'vk_id': 999, 'branch_id': 42})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('detail', response.data)


# ── View: EmployeeView ────────────────────────────────────────────────────────

class EmployeeViewTest(TestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = EmployeeView.as_view()

    def _get(self, **params):
        return self.view(self.factory.get('/', params))

    @patch('apps.tenant.branch.api.views.get_employees')
    def test_returns_200_on_success(self, mock_svc):
        mock_svc.return_value = []
        self.assertEqual(self._get(branch_id=42).status_code, status.HTTP_200_OK)

    @patch('apps.tenant.branch.api.views.get_employees')
    def test_response_is_list(self, mock_svc):
        mock_svc.return_value = []
        self.assertIsInstance(self._get(branch_id=42).data, list)

    @patch('apps.tenant.branch.api.views.get_employees')
    def test_returns_404_on_branch_not_found(self, mock_svc):
        mock_svc.side_effect = BranchNotFound
        response = self._get(branch_id=999)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('detail', response.data)

    @patch('apps.tenant.branch.api.views.get_employees')
    def test_returns_403_on_branch_inactive(self, mock_svc):
        mock_svc.side_effect = BranchInactive
        self.assertEqual(self._get(branch_id=42).status_code, status.HTTP_403_FORBIDDEN)

    def test_returns_400_when_missing_branch_id(self):
        self.assertEqual(self._get().status_code, status.HTTP_400_BAD_REQUEST)


# ── View: PromotionView ───────────────────────────────────────────────────────

class PromotionViewTest(TestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = PromotionView.as_view()

    def _get(self, **params):
        return self.view(self.factory.get('/', params))

    @patch('apps.tenant.branch.api.views.get_promotions')
    def test_returns_200_on_success(self, mock_svc):
        mock_svc.return_value = []
        self.assertEqual(self._get(branch_id=42).status_code, status.HTTP_200_OK)

    @patch('apps.tenant.branch.api.views.get_promotions')
    def test_returns_404_on_branch_not_found(self, mock_svc):
        mock_svc.side_effect = BranchNotFound
        self.assertEqual(self._get(branch_id=999).status_code, status.HTTP_404_NOT_FOUND)

    @patch('apps.tenant.branch.api.views.get_promotions')
    def test_returns_403_on_branch_inactive(self, mock_svc):
        mock_svc.side_effect = BranchInactive
        self.assertEqual(self._get(branch_id=42).status_code, status.HTTP_403_FORBIDDEN)

    def test_returns_400_when_missing_branch_id(self):
        self.assertEqual(self._get().status_code, status.HTTP_400_BAD_REQUEST)


# ── View: TransactionsView ────────────────────────────────────────────────────

class TransactionsViewTest(TestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = TransactionsView.as_view()

    def _get(self, **params):
        return self.view(self.factory.get('/', params))

    @patch('apps.tenant.branch.api.views.get_transactions')
    def test_returns_200_on_success(self, mock_svc):
        mock_svc.return_value = []
        self.assertEqual(self._get(vk_id=111, branch_id=42).status_code, status.HTTP_200_OK)

    @patch('apps.tenant.branch.api.views.get_transactions')
    def test_response_is_list(self, mock_svc):
        mock_svc.return_value = []
        self.assertIsInstance(self._get(vk_id=111, branch_id=42).data, list)

    @patch('apps.tenant.branch.api.views.get_transactions')
    def test_returns_404_when_not_found(self, mock_svc):
        mock_svc.side_effect = ClientNotFound
        response = self._get(vk_id=999, branch_id=42)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('detail', response.data)

    def test_returns_400_when_missing_vk_id(self):
        self.assertEqual(self._get(branch_id=42).status_code, status.HTTP_400_BAD_REQUEST)

    @patch('apps.tenant.branch.api.views.get_transactions')
    def test_passes_params_to_service(self, mock_svc):
        mock_svc.return_value = []
        self._get(vk_id=555, branch_id=33)
        mock_svc.assert_called_once_with(vk_id=555, branch_id=33)


# ── Serializer: BranchInfoSerializer ─────────────────────────────────────────

class BranchInfoSerializerTest(TestCase):

    def _make(self, **overrides):
        return BranchInfoSerializer(_branch_data(**overrides))

    def test_has_all_required_fields(self):
        fields = BranchInfoSerializer().fields
        for f in ('id', 'branch_id', 'name', 'address', 'phone',
                  'logotype_url', 'coin_icon_url', 'vk_group_id', 'vk_group_name'):
            with self.subTest(field=f):
                self.assertIn(f, fields)

    def test_serializes_name_correctly(self):
        self.assertEqual(self._make(name='Пицца Плюс').data['name'], 'Пицца Плюс')

    def test_null_config_fields_serialized_as_none(self):
        data = self._make().data
        self.assertIsNone(data['logotype_url'])
        self.assertIsNone(data['vk_group_id'])


# ── Serializer: ClientProfileResponseSerializer ───────────────────────────────

class ClientProfileResponseSerializerTest(TestCase):

    def _serialize(self, **overrides):
        return ClientProfileResponseSerializer(_profile_mock(**overrides)).data

    def test_vk_status_booleans_default_to_false_when_none(self):
        data = self._serialize()
        self.assertFalse(data['is_community_member'])
        self.assertFalse(data['is_newsletter_subscriber'])

    def test_via_app_fields_default_to_none_when_no_status(self):
        data = self._serialize()
        self.assertIsNone(data['community_via_app'])
        self.assertIsNone(data['newsletter_via_app'])

    def test_flattens_client_vk_id(self):
        self.assertEqual(self._serialize()['vk_id'], 111)

    def test_uses_annotated_coins_balance(self):
        self.assertEqual(self._serialize(_coins_balance=50)['coins_balance'], 50)

    def test_vk_status_fields_when_subscribed(self):
        vk = MagicMock(
            is_community_member=True,
            community_via_app=True,
            is_newsletter_subscriber=False,
            newsletter_via_app=None,
        )
        data = self._serialize(vk_status=vk)

        self.assertTrue(data['is_community_member'])
        self.assertTrue(data['community_via_app'])
        self.assertFalse(data['is_newsletter_subscriber'])


# ── Serializer: EmployeeSerializer ───────────────────────────────────────────

class EmployeeSerializerTest(TestCase):

    def test_has_coins_balance_and_vk_id(self):
        fields = EmployeeSerializer().fields
        self.assertIn('coins_balance', fields)
        self.assertIn('vk_id', fields)

    def test_uses_annotated_coins_balance(self):
        data = EmployeeSerializer(_profile_mock(_coins_balance=25)).data
        self.assertEqual(data['coins_balance'], 25)


# ── Serializer: PromotionSerializer ──────────────────────────────────────────

class PromotionSerializerTest(TestCase):

    def _promo(self, has_image=True):
        p = MagicMock()
        p.id = 1
        p.title = 'Скидка 20%'
        p.discount = '20%'
        p.dates = '1–31 мая'
        img = MagicMock()
        img.name = 'promotions/test.jpg' if has_image else None
        img.url = '/media/promotions/test.jpg'
        p.images = img
        return p

    def test_has_required_fields(self):
        fields = PromotionSerializer().fields
        for f in ('id', 'title', 'discount', 'dates', 'image_url'):
            with self.subTest(field=f):
                self.assertIn(f, fields)

    def test_returns_image_url_when_present(self):
        data = PromotionSerializer(self._promo(has_image=True)).data
        self.assertEqual(data['image_url'], '/media/promotions/test.jpg')

    def test_returns_null_when_no_image(self):
        data = PromotionSerializer(self._promo(has_image=False)).data
        self.assertIsNone(data['image_url'])


# ── Serializer: CoinTransactionSerializer ─────────────────────────────────────

class BranchInfoStoryImageTest(TestCase):
    """BranchInfoSerializer includes story_image_url field."""

    def test_serializer_includes_story_image_url(self):
        data = _branch_data(story_image_url='/media/branch/stories/bg.jpg')
        s = BranchInfoSerializer(data)
        self.assertIn('story_image_url', s.data)
        self.assertEqual(s.data['story_image_url'], '/media/branch/stories/bg.jpg')

    def test_serializer_story_image_url_accepts_null(self):
        data = _branch_data(story_image_url=None)
        s = BranchInfoSerializer(data)
        self.assertIsNone(s.data['story_image_url'])

    @patch('apps.tenant.branch.api.views.get_branch_info')
    def test_view_includes_story_image_url(self, mock_get_info):
        factory = APIRequestFactory()
        mock_get_info.return_value = _branch_data(
            story_image_url='/media/branch/stories/bg.jpg'
        )
        request = factory.get('/api/v1/branches/42/')
        response = BranchInfoView.as_view()(request, branch_id=42)
        self.assertEqual(response.status_code, 200)
        self.assertIn('story_image_url', response.data)


class CoinTransactionSerializerTest(TestCase):

    def test_has_required_fields(self):
        fields = CoinTransactionSerializer().fields
        for f in ('id', 'type', 'source', 'amount', 'description', 'created_at'):
            with self.subTest(field=f):
                self.assertIn(f, fields)


# ── Service: vk_oauth_exchange ────────────────────────────────────────────────

class VkOauthExchangeTest(TestCase):

    @patch('apps.tenant.branch.api.services.settings')
    def test_raises_when_app_id_not_configured(self, mock_settings):
        mock_settings.VK_WEB_APP_ID = None

        with self.assertRaises(VKAuthError) as ctx:
            vk_oauth_exchange('code', 'dev', 'verifier', 'https://x.com/cb', 'state')

        self.assertIn('VK_WEB_APP_ID', str(ctx.exception))

    @patch('urllib.request.urlopen')
    @patch('apps.tenant.branch.api.services.settings')
    def test_returns_user_data_on_success(self, mock_settings, mock_urlopen):
        mock_settings.VK_WEB_APP_ID = 53418653
        mock_urlopen.side_effect = _urlopen_mock(_VK_TOKEN_OK, _VK_USER_OK).side_effect

        result = vk_oauth_exchange('code', 'dev', 'verifier', 'https://x.com/cb', 'state')

        self.assertEqual(result['user_id'], 123456)
        self.assertEqual(result['first_name'], 'Иван')
        self.assertEqual(result['last_name'], 'Иванов')
        self.assertEqual(result['photo_url'], 'https://vk.com/photo.jpg')

    @patch('urllib.request.urlopen')
    @patch('apps.tenant.branch.api.services.settings')
    def test_calls_vk_auth_then_user_info(self, mock_settings, mock_urlopen):
        mock_settings.VK_WEB_APP_ID = 53418653
        mock_urlopen.side_effect = _urlopen_mock(_VK_TOKEN_OK, _VK_USER_OK).side_effect

        vk_oauth_exchange('code', 'dev', 'verifier', 'https://x.com/cb', 'state')

        self.assertEqual(mock_urlopen.call_count, 2)
        first_url = mock_urlopen.call_args_list[0][0][0].full_url
        second_url = mock_urlopen.call_args_list[1][0][0].full_url
        self.assertIn('id.vk.ru/oauth2/auth', first_url)
        self.assertIn('id.vk.ru/oauth2/user_info', second_url)

    @patch('urllib.request.urlopen')
    @patch('apps.tenant.branch.api.services.settings')
    def test_raises_on_vk_error_response(self, mock_settings, mock_urlopen):
        mock_settings.VK_WEB_APP_ID = 53418653
        error_resp = {'error': 'invalid_client', 'error_description': 'Bad code'}
        mock_urlopen.side_effect = _urlopen_mock(error_resp).side_effect

        with self.assertRaises(VKAuthError) as ctx:
            vk_oauth_exchange('bad_code', 'dev', 'verifier', 'https://x.com/cb', 'state')

        self.assertIn('Bad code', str(ctx.exception))

    @patch('urllib.request.urlopen')
    @patch('apps.tenant.branch.api.services.settings')
    def test_raises_when_access_token_missing(self, mock_settings, mock_urlopen):
        mock_settings.VK_WEB_APP_ID = 53418653
        incomplete = {'user_id': 123456}  # no access_token
        mock_urlopen.side_effect = _urlopen_mock(incomplete).side_effect

        with self.assertRaises(VKAuthError) as ctx:
            vk_oauth_exchange('code', 'dev', 'verifier', 'https://x.com/cb', 'state')

        self.assertIn('access_token', str(ctx.exception))

    @patch('urllib.request.urlopen')
    @patch('apps.tenant.branch.api.services.settings')
    def test_raises_when_user_id_missing(self, mock_settings, mock_urlopen):
        mock_settings.VK_WEB_APP_ID = 53418653
        incomplete = {'access_token': 'tok'}  # no user_id
        mock_urlopen.side_effect = _urlopen_mock(incomplete).side_effect

        with self.assertRaises(VKAuthError) as ctx:
            vk_oauth_exchange('code', 'dev', 'verifier', 'https://x.com/cb', 'state')

        self.assertIn('user_id', str(ctx.exception))

    @patch('urllib.request.urlopen')
    @patch('apps.tenant.branch.api.services.settings')
    def test_raises_on_network_error(self, mock_settings, mock_urlopen):
        mock_settings.VK_WEB_APP_ID = 53418653
        mock_urlopen.side_effect = OSError('Connection refused')

        with self.assertRaises(VKAuthError) as ctx:
            vk_oauth_exchange('code', 'dev', 'verifier', 'https://x.com/cb', 'state')

        self.assertIn('Ошибка обмена кода VK', str(ctx.exception))

    @patch('urllib.request.urlopen')
    @patch('apps.tenant.branch.api.services.settings')
    def test_raises_on_user_info_network_error(self, mock_settings, mock_urlopen):
        mock_settings.VK_WEB_APP_ID = 53418653
        # First call succeeds (token exchange), second fails (user_info)
        token_mock = MagicMock()
        token_mock.__enter__ = MagicMock(return_value=token_mock)
        token_mock.__exit__ = MagicMock(return_value=False)
        token_mock.read.return_value = json.dumps(_VK_TOKEN_OK).encode()
        mock_urlopen.side_effect = [token_mock, OSError('Timeout')]

        with self.assertRaises(VKAuthError) as ctx:
            vk_oauth_exchange('code', 'dev', 'verifier', 'https://x.com/cb', 'state')

        self.assertIn('Ошибка получения профиля VK', str(ctx.exception))

    @patch('urllib.request.urlopen')
    @patch('apps.tenant.branch.api.services.settings')
    def test_missing_user_in_info_response_returns_empty_strings(self, mock_settings, mock_urlopen):
        mock_settings.VK_WEB_APP_ID = 53418653
        # user_info returns empty user dict
        empty_user = {'user': {}}
        mock_urlopen.side_effect = _urlopen_mock(_VK_TOKEN_OK, empty_user).side_effect

        result = vk_oauth_exchange('code', 'dev', 'verifier', 'https://x.com/cb', 'state')

        self.assertEqual(result['first_name'], '')
        self.assertEqual(result['last_name'], '')
        self.assertEqual(result['photo_url'], '')


# ── Serializer: VKAuthRequestSerializer ──────────────────────────────────────

class VKAuthRequestSerializerTest(TestCase):

    def _valid_data(self, **overrides):
        data = dict(_VK_AUTH_PAYLOAD)
        data.update(overrides)
        return data

    def test_valid_data_passes(self):
        s = VKAuthRequestSerializer(data=self._valid_data())
        self.assertTrue(s.is_valid(), s.errors)

    def test_has_all_required_fields(self):
        for field in ('code', 'device_id', 'code_verifier', 'redirect_uri', 'state', 'branch_id'):
            with self.subTest(field=field):
                self.assertIn(field, VKAuthRequestSerializer().fields)

    def test_missing_code_is_invalid(self):
        s = VKAuthRequestSerializer(data=self._valid_data(code=''))
        self.assertFalse(s.is_valid())
        self.assertIn('code', s.errors)

    def test_missing_device_id_is_invalid(self):
        data = self._valid_data()
        del data['device_id']
        s = VKAuthRequestSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn('device_id', s.errors)

    def test_missing_code_verifier_is_invalid(self):
        data = self._valid_data()
        del data['code_verifier']
        s = VKAuthRequestSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn('code_verifier', s.errors)

    def test_missing_redirect_uri_is_invalid(self):
        data = self._valid_data()
        del data['redirect_uri']
        s = VKAuthRequestSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn('redirect_uri', s.errors)

    def test_invalid_redirect_uri_format(self):
        s = VKAuthRequestSerializer(data=self._valid_data(redirect_uri='not-a-url'))
        self.assertFalse(s.is_valid())
        self.assertIn('redirect_uri', s.errors)

    def test_missing_branch_id_is_invalid(self):
        data = self._valid_data()
        del data['branch_id']
        s = VKAuthRequestSerializer(data=data)
        self.assertFalse(s.is_valid())
        self.assertIn('branch_id', s.errors)

    def test_birth_date_is_optional(self):
        data = self._valid_data()
        data.pop('birth_date', None)
        s = VKAuthRequestSerializer(data=data)
        self.assertTrue(s.is_valid(), s.errors)
        self.assertIsNone(s.validated_data['birth_date'])

    def test_birth_date_parsed_when_provided(self):
        s = VKAuthRequestSerializer(data=self._valid_data(birth_date='1990-05-15'))
        self.assertTrue(s.is_valid(), s.errors)
        import datetime
        self.assertEqual(s.validated_data['birth_date'], datetime.date(1990, 5, 15))


# ── View: VKAuthView ──────────────────────────────────────────────────────────

class VKAuthViewTest(TestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.view = VKAuthView.as_view()

    def _post(self, data=None):
        payload = dict(_VK_AUTH_PAYLOAD) if data is None else data
        return self.view(self.factory.post('/', payload, format='json'))

    @patch('apps.tenant.branch.api.views.vk_web_auth')
    def test_returns_201_for_new_guest(self, mock_auth):
        mock_auth.return_value = (_profile_mock(), True)
        self.assertEqual(self._post().status_code, status.HTTP_201_CREATED)

    @patch('apps.tenant.branch.api.views.vk_web_auth')
    def test_returns_200_for_existing_guest(self, mock_auth):
        mock_auth.return_value = (_profile_mock(), False)
        self.assertEqual(self._post().status_code, status.HTTP_200_OK)

    @patch('apps.tenant.branch.api.views.vk_web_auth')
    def test_response_contains_client_profile_fields(self, mock_auth):
        mock_auth.return_value = (_profile_mock(), True)
        data = self._post().data
        for field in ('vk_id', 'first_name', 'last_name', 'coins_balance', 'is_employee'):
            with self.subTest(field=field):
                self.assertIn(field, data)

    @patch('apps.tenant.branch.api.views.vk_web_auth')
    def test_returns_400_on_vk_auth_error(self, mock_auth):
        mock_auth.side_effect = VKAuthError('Неверный code')
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('detail', response.data)
        self.assertIn('Неверный code', response.data['detail'])

    @patch('apps.tenant.branch.api.views.vk_web_auth')
    def test_returns_404_on_branch_not_found(self, mock_auth):
        mock_auth.side_effect = BranchNotFound
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('detail', response.data)

    @patch('apps.tenant.branch.api.views.vk_web_auth')
    def test_returns_403_on_branch_inactive(self, mock_auth):
        mock_auth.side_effect = BranchInactive
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('detail', response.data)

    @patch('apps.tenant.branch.api.views.vk_web_auth')
    def test_returns_403_on_client_blocked(self, mock_auth):
        mock_auth.side_effect = ClientBlocked
        response = self._post()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('detail', response.data)

    def test_returns_400_when_code_missing(self):
        data = dict(_VK_AUTH_PAYLOAD)
        del data['code']
        self.assertEqual(self._post(data).status_code, status.HTTP_400_BAD_REQUEST)

    def test_returns_400_when_device_id_missing(self):
        data = dict(_VK_AUTH_PAYLOAD)
        del data['device_id']
        self.assertEqual(self._post(data).status_code, status.HTTP_400_BAD_REQUEST)

    def test_returns_400_when_code_verifier_missing(self):
        data = dict(_VK_AUTH_PAYLOAD)
        del data['code_verifier']
        self.assertEqual(self._post(data).status_code, status.HTTP_400_BAD_REQUEST)

    def test_returns_400_when_redirect_uri_missing(self):
        data = dict(_VK_AUTH_PAYLOAD)
        del data['redirect_uri']
        self.assertEqual(self._post(data).status_code, status.HTTP_400_BAD_REQUEST)

    def test_returns_400_when_redirect_uri_invalid(self):
        data = dict(_VK_AUTH_PAYLOAD, redirect_uri='not-a-url')
        self.assertEqual(self._post(data).status_code, status.HTTP_400_BAD_REQUEST)

    def test_returns_400_when_branch_id_missing(self):
        data = dict(_VK_AUTH_PAYLOAD)
        del data['branch_id']
        self.assertEqual(self._post(data).status_code, status.HTTP_400_BAD_REQUEST)

    @patch('apps.tenant.branch.api.views.vk_web_auth')
    def test_passes_all_params_to_vk_web_auth(self, mock_auth):
        mock_auth.return_value = (_profile_mock(), True)
        self._post()
        mock_auth.assert_called_once_with(
            code='AUTH_CODE',
            device_id='DEV_ID',
            code_verifier='VERIFIER_STRING',
            redirect_uri='https://example.com/callback',
            state='STATE_STRING',
            branch_id=42,
            birth_date=None,
        )


# ── URL routing: vk-auth ──────────────────────────────────────────────────────

class VKAuthUrlTest(TestCase):

    def test_vk_auth_url_resolves_to_view(self):
        url = reverse('vk-auth')
        resolved = resolve(url)
        self.assertEqual(resolved.func.view_class, VKAuthView)

    def test_vk_auth_url_pattern(self):
        url = reverse('vk-auth')
        self.assertEqual(url, '/api/v1/vk/auth/')
