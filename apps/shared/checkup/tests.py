"""
Обмен токена CheckUp → LoyalUP.

Три слоя: чистая валидация тела (без БД), цепочка проверок ручки (RequestFactory,
без БД — сам обмен подменён) и полный обмен на настоящих User/Company (TestCase;
схема тенанта не создаётся, перевод branch_id → Branch.id подменён, потому что
он единственный ходит в тенантную схему).
"""
import datetime
import json
from datetime import date
from unittest import mock

from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from apps.shared.clients.models import Company
from apps.shared.users.auth import JWTAuthentication, decode_token
from apps.shared.users.models import User

from .models import CheckUpIdentity
from .services import (
    ExchangeError, allowed_tenants, branches_payload, issue_exchange_token,
    perform_exchange, username_for, validate_payload,
)
from .views import TokenExchangeView

URL = '/api/v1/internal/auth/exchange/'
SECRET = 'test-exchange-secret'


def _good(**over):
    body = {'checkup_user_id': 42, 'tenant_schema': 'sandbox_t', 'role': 'network_admin',
            'display_name': 'Алина Петрова', 'email': 'alina@example.com'}
    body.update(over)
    return body


# ── validate_payload ─────────────────────────────────────────────────────────

class ValidatePayloadTest(SimpleTestCase):

    def test_ok_network_admin_int_id_becomes_str(self):
        p = validate_payload(_good())
        self.assertEqual(p['checkup_user_id'], '42')
        self.assertEqual(p['tenant_schema'], 'sandbox_t')
        self.assertEqual(p['role'], 'network_admin')
        self.assertEqual(p['branch_ids'], [])
        self.assertEqual(p['display_name'], 'Алина Петрова')

    def test_network_admin_ignores_branch_list(self):
        p = validate_payload(_good(branch_ids=[1, 2]))
        self.assertEqual(p['branch_ids'], [])

    def test_client_requires_branches(self):
        with self.assertRaises(ExchangeError) as cm:
            validate_payload(_good(role='client'))
        self.assertEqual((cm.exception.status, cm.exception.code), (422, 'invalid_payload'))

    def test_client_branches_dedup_keep_order(self):
        p = validate_payload(_good(role='client', branch_ids=[5, 3, 5, 9]))
        self.assertEqual(p['branch_ids'], [5, 3, 9])

    def test_client_branches_must_be_positive_ints(self):
        for bad in ([0], [-1], ['1'], [True], 'all', 7):
            with self.assertRaises(ExchangeError, msg=repr(bad)) as cm:
                validate_payload(_good(role='client', branch_ids=bad))
            self.assertEqual(cm.exception.status, 422)

    def test_unknown_role_is_403(self):
        for role in ('staff', 'owner', 'admin', 'manager', 'superadmin', ''):
            with self.assertRaises(ExchangeError, msg=role) as cm:
                validate_payload(_good(role=role))
            self.assertEqual((cm.exception.status, cm.exception.code), (403, 'role_not_allowed'))

    def test_schema_shape(self):
        for bad in ('public', '1abc', 'a b', '', 'Public'):
            with self.assertRaises(ExchangeError, msg=bad):
                validate_payload(_good(tenant_schema=bad))
        self.assertEqual(validate_payload(_good(tenant_schema='asap-arzamas'))['tenant_schema'], 'asap-arzamas')
        # Регистр нормализуем, а не отвергаем: schema_name у нас всегда в нижнем.
        self.assertEqual(validate_payload(_good(tenant_schema=' Levone '))['tenant_schema'], 'levone')

    def test_user_id_shape(self):
        for bad in ('', 'a:b', 'x' * 65, None, True, 3.5):
            with self.assertRaises(ExchangeError, msg=repr(bad)):
                validate_payload(_good(checkup_user_id=bad))
        self.assertEqual(validate_payload(_good(checkup_user_id='u_12.3-x'))['checkup_user_id'], 'u_12.3-x')

    def test_not_a_dict(self):
        with self.assertRaises(ExchangeError):
            validate_payload([1, 2])

    def test_username_shape(self):
        self.assertEqual(username_for('42', 'levone'), 'checkup-42-levone')


class SettingsHelpersTest(SimpleTestCase):

    @override_settings(CHECKUP_TOKEN_EXCHANGE_TENANTS=['dev', ' Levone '])
    def test_allowed_tenants_list(self):
        self.assertEqual(allowed_tenants(), ['dev', 'levone'])

    @override_settings(CHECKUP_TOKEN_EXCHANGE_TENANTS='dev,asap_orel')
    def test_allowed_tenants_string(self):
        self.assertEqual(allowed_tenants(), ['dev', 'asap_orel'])

    @override_settings(CHECKUP_TOKEN_EXCHANGE_TENANTS=[])
    def test_allowed_tenants_empty_means_all(self):
        self.assertEqual(allowed_tenants(), [])


class IssueTokenTest(SimpleTestCase):

    @override_settings(CHECKUP_TOKEN_EXCHANGE_MINUTES=60)
    def test_claims_and_lifetime(self):
        fake = mock.Mock(pk=5, username='checkup-1-dev', role='client')
        token, expires_at = issue_exchange_token(fake, 'dev')
        payload = decode_token(token)
        self.assertEqual(payload['sub'], '5')
        self.assertEqual(payload['typ'], 'access')
        self.assertEqual(payload['via'], 'checkup')
        self.assertEqual(payload['tenant'], 'dev')
        self.assertEqual(payload['exp'] - payload['iat'], 60 * 60)
        self.assertEqual(payload['exp'], int(expires_at.timestamp()))


# ── цепочка проверок ручки (без БД) ──────────────────────────────────────────

def _post(body, secret=None, remote='127.0.0.1', xff=None, raw=None):
    extra = {'REMOTE_ADDR': remote}
    if secret is not None:
        extra['HTTP_X_LOYALUP_EXCHANGE_SECRET'] = secret
    if xff:
        extra['HTTP_X_FORWARDED_FOR'] = xff
    data = raw if raw is not None else json.dumps(body)
    request = RequestFactory().post(URL, data=data, content_type='application/json', **extra)
    return TokenExchangeView.as_view()(request)


@override_settings(CHECKUP_TOKEN_EXCHANGE_SECRET=SECRET)
class ExchangeViewGuardTest(SimpleTestCase):

    def test_forwarded_request_is_forbidden(self):
        r = _post(_good(), secret=SECRET, xff='1.2.3.4')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(json.loads(r.content)['code'], 'forbidden')

    def test_public_remote_is_forbidden(self):
        self.assertEqual(_post(_good(), secret=SECRET, remote='8.8.8.8').status_code, 403)

    def test_docker_bridge_remote_passes_ip_guard(self):
        # Контейнер видит источник как адрес моста 172.x — это внутренний запрос (замечание CheckUp).
        with mock.patch('apps.shared.checkup.views.perform_exchange', side_effect=ExchangeError(404, 'tenant_not_found')):
            r = _post(_good(), secret=SECRET, remote='172.18.0.1')
        self.assertEqual(r.status_code, 404)

    @override_settings(CHECKUP_TOKEN_EXCHANGE_SECRET='')
    def test_disabled_is_503(self):
        r = _post(_good(), secret='anything')
        self.assertEqual(r.status_code, 503)
        self.assertEqual(json.loads(r.content)['code'], 'exchange_disabled')

    def test_wrong_secret_is_401(self):
        r = _post(_good(), secret='nope')
        self.assertEqual(r.status_code, 401)
        self.assertEqual(json.loads(r.content)['code'], 'bad_secret')

    def test_missing_secret_is_401(self):
        self.assertEqual(_post(_good()).status_code, 401)

    def test_bad_json_is_400(self):
        r = _post(None, secret=SECRET, raw='{not json')
        self.assertEqual(r.status_code, 400)

    def test_exchange_error_passthrough(self):
        with mock.patch('apps.shared.checkup.views.perform_exchange',
                        side_effect=ExchangeError(422, 'unknown_branch', 'нет', unknown_branch_ids=[9])):
            r = _post(_good(), secret=SECRET)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(json.loads(r.content), {'code': 'unknown_branch', 'detail': 'нет', 'unknown_branch_ids': [9]})

    def test_get_not_allowed(self):
        request = RequestFactory().get(URL, REMOTE_ADDR='127.0.0.1')
        self.assertEqual(TokenExchangeView.as_view()(request).status_code, 405)


# ── полный обмен на настоящих User/Company ───────────────────────────────────

def _tenant(schema='sandbox_t', client_id=7701, active=True):
    company = Company(schema_name=schema, client_id=client_id, name='Песочница',
                      paid_until=date(2030, 1, 1), is_active=active)
    company.auto_create_schema = False   # схему не создаём: тесты не ходят в тенантные таблицы
    company.save()
    return company


@override_settings(CHECKUP_TOKEN_EXCHANGE_SECRET=SECRET, CHECKUP_TOKEN_EXCHANGE_TENANTS=[], CHECKUP_TOKEN_EXCHANGE_MINUTES=60)
class PerformExchangeTest(TestCase):

    def setUp(self):
        self.tenant = _tenant()

    def test_first_exchange_creates_user_identity_and_token(self):
        result = perform_exchange(_good())
        user = result['user']
        self.assertTrue(result['created'])
        self.assertEqual(user.username, 'checkup-42-sandbox_t')
        self.assertEqual(user.role, 'network_admin')
        self.assertEqual(user.first_name, 'Алина Петрова')
        self.assertEqual(user.email, '', 'email CheckUp в User не пишем')
        self.assertFalse(user.has_usable_password())
        self.assertEqual(list(user.companies.values_list('schema_name', flat=True)), ['sandbox_t'])
        self.assertEqual(user.branch_access, {'sandbox_t': 'all'})

        identity = CheckUpIdentity.objects.get(checkup_user_id='42', tenant_schema='sandbox_t')
        self.assertEqual(identity.user_id, user.pk)
        self.assertEqual(identity.email, 'alina@example.com')
        self.assertEqual(identity.exchanges_count, 1)

        payload = decode_token(result['token'])
        self.assertEqual(payload['sub'], str(user.pk))
        self.assertEqual(payload['via'], 'checkup')

    def test_second_exchange_reuses_user(self):
        first = perform_exchange(_good())
        second = perform_exchange(_good(display_name='Алина П.'))
        self.assertFalse(second['created'])
        self.assertEqual(first['user'].pk, second['user'].pk)
        self.assertEqual(User.objects.filter(username__startswith='checkup-42-').count(), 1)
        second['identity'].refresh_from_db()
        self.assertEqual(second['identity'].exchanges_count, 2)
        self.assertEqual(second['user'].first_name, 'Алина П.')

    @mock.patch('apps.shared.checkup.services.resolve_branch_pks', return_value=[11, 12])
    def test_client_exchange_returns_branch_pairs(self, _resolve):
        # Ответ обмена несёт мэппинг, иначе BFF CheckUp не знает внутренних id.
        result = perform_exchange(_good(role='client', branch_ids=[101, 102]))
        self.assertEqual(result['branches'],
                         [{'branch_id': 101, 'id': 11}, {'branch_id': 102, 'id': 12}])

    def test_network_admin_exchange_returns_none_branches(self):
        self.assertIsNone(perform_exchange(_good())['branches'],
                          'у админа сети точки не перечисляем — их берут из analytics/branches')

    @mock.patch('apps.shared.checkup.services.resolve_branch_pks', return_value=[11, 12])
    def test_client_gets_branch_pks_not_public_ids(self, resolve):
        result = perform_exchange(_good(role='client', branch_ids=[101, 102]))
        resolve.assert_called_once_with('sandbox_t', [101, 102])
        self.assertEqual(result['user'].role, 'client')
        self.assertEqual(result['user'].branch_access, {'sandbox_t': [11, 12]})
        self.assertEqual(result['identity'].last_branch_ids, [101, 102])

    @mock.patch('apps.shared.checkup.services.resolve_branch_pks', return_value=[11])
    def test_role_change_is_applied_each_exchange(self, _resolve):
        perform_exchange(_good())
        result = perform_exchange(_good(role='client', branch_ids=[101]))
        self.assertEqual(result['user'].role, 'client')
        self.assertEqual(result['user'].branch_access, {'sandbox_t': [11]})
        result = perform_exchange(_good())
        self.assertEqual(result['user'].role, 'network_admin')
        self.assertEqual(result['user'].branch_access, {'sandbox_t': 'all'})

    def test_other_tenant_keys_in_branch_access_untouched(self):
        result = perform_exchange(_good())
        user = result['user']
        user.branch_access = {'other': [1]}
        user.save(update_fields=['branch_access'])
        result = perform_exchange(_good())
        self.assertEqual(result['user'].branch_access, {'other': [1], 'sandbox_t': 'all'})

    def test_existing_native_user_with_same_username_is_conflict(self):
        User.objects.create_user(username='checkup-7-sandbox_t', password='x')
        with self.assertRaises(ExchangeError) as cm:
            perform_exchange(_good(checkup_user_id=7))
        self.assertEqual((cm.exception.status, cm.exception.code), (409, 'identity_conflict'))

    def test_disabled_user_is_not_revived(self):
        user = perform_exchange(_good())['user']
        User.objects.filter(pk=user.pk).update(is_active=False)
        with self.assertRaises(ExchangeError) as cm:
            perform_exchange(_good())
        self.assertEqual((cm.exception.status, cm.exception.code), (403, 'user_disabled'))

    def test_unknown_tenant_404(self):
        with self.assertRaises(ExchangeError) as cm:
            perform_exchange(_good(tenant_schema='nope'))
        self.assertEqual((cm.exception.status, cm.exception.code), (404, 'tenant_not_found'))

    def test_inactive_tenant_404(self):
        _tenant(schema='off_t', client_id=7702, active=False)
        with self.assertRaises(ExchangeError) as cm:
            perform_exchange(_good(tenant_schema='off_t'))
        self.assertEqual((cm.exception.status, cm.exception.code), (404, 'tenant_inactive'))

    @override_settings(CHECKUP_TOKEN_EXCHANGE_TENANTS=['dev'])
    def test_tenant_not_in_allowlist_403(self):
        with self.assertRaises(ExchangeError) as cm:
            perform_exchange(_good())
        self.assertEqual((cm.exception.status, cm.exception.code), (403, 'tenant_not_allowed'))
        self.assertFalse(User.objects.filter(username__startswith='checkup-').exists())

    def test_same_checkup_user_two_tenants_two_identities(self):
        _tenant(schema='second_t', client_id=7703)
        a = perform_exchange(_good())['user']
        b = perform_exchange(_good(tenant_schema='second_t'))['user']
        self.assertNotEqual(a.pk, b.pk)
        self.assertEqual(list(b.companies.values_list('schema_name', flat=True)), ['second_t'])

    def test_token_is_accepted_by_jwt_authentication(self):
        result = perform_exchange(_good())
        request = RequestFactory().get('/api/v1/mobile/reviews/', HTTP_AUTHORIZATION=f'Bearer {result["token"]}')
        user, _ = JWTAuthentication().authenticate(request)
        self.assertEqual(user.pk, result['user'].pk)

    def test_view_end_to_end(self):
        r = _post(_good(), secret=SECRET, remote='172.18.0.1')
        self.assertEqual(r.status_code, 200, r.content)
        body = json.loads(r.content)
        self.assertTrue(body['created'])
        self.assertEqual(body['tenant_schema'], 'sandbox_t')
        self.assertEqual(body['profile']['username'], 'checkup-42-sandbox_t')
        self.assertEqual(body['profile']['role'], 'network_admin')
        self.assertTrue(3500 <= body['expires_in'] <= 3600)
        expires = datetime.datetime.fromisoformat(body['expires_at'])
        self.assertIsNotNone(expires.tzinfo)


# ── branches_payload: пары «публичный branch_id → внутренний id» ─────────────

class BranchesPayloadTest(SimpleTestCase):
    """CheckUp знает точку по публичному branch_id, фильтры наших ручек — по id."""

    def test_pairs_keep_the_order_of_requested_ids(self):
        self.assertEqual(
            branches_payload([101, 102], [11, 12]),
            [{'branch_id': 101, 'id': 11}, {'branch_id': 102, 'id': 12}],
        )

    def test_network_admin_gets_none_not_empty_list(self):
        # None = «все точки сети», пустой список значил бы «точек нет».
        self.assertIsNone(branches_payload([], None))

    def test_no_query_to_the_tenant(self):
        # Чистая функция: если она полезет в базу, тест упадёт на SimpleTestCase.
        self.assertEqual(branches_payload([7], [70]), [{'branch_id': 7, 'id': 70}])


# ── волна 3: платформенный доступ (контракт 3в) ──────────────────────────────

from .services import is_platform_admin, list_platform_tenants, platform_admins, tenant_open  # noqa: E402
from .views import InternalTenantsView  # noqa: E402


class PlatformSettingsTest(SimpleTestCase):

    @override_settings(CHECKUP_PLATFORM_ADMINS='1, 42 ,')
    def test_platform_admins_parsed(self):
        self.assertEqual(platform_admins(), {'1', '42'})
        self.assertTrue(is_platform_admin(42))
        self.assertTrue(is_platform_admin('1'))
        self.assertFalse(is_platform_admin('7'))

    @override_settings(CHECKUP_PLATFORM_ADMINS='')
    def test_empty_whitelist(self):
        self.assertEqual(platform_admins(), set())
        self.assertFalse(is_platform_admin('1'))

    @override_settings(CHECKUP_TOKEN_EXCHANGE_TENANTS=['dev'])
    def test_tenant_open(self):
        self.assertTrue(tenant_open('dev'))
        self.assertFalse(tenant_open('levone'))

    @override_settings(CHECKUP_TOKEN_EXCHANGE_TENANTS=[])
    def test_tenant_open_when_list_empty(self):
        self.assertTrue(tenant_open('levone'))

    @override_settings(CHECKUP_TOKEN_EXCHANGE_MINUTES=60)
    def test_platform_claim_only_when_asked(self):
        fake = mock.Mock(pk=5, username='checkup-1-dev', role='network_admin')
        plain, _ = issue_exchange_token(fake, 'dev')
        platform, _ = issue_exchange_token(fake, 'dev', platform=True)
        self.assertNotIn('platform', decode_token(plain))
        self.assertIs(decode_token(platform)['platform'], True)


@override_settings(CHECKUP_TOKEN_EXCHANGE_SECRET=SECRET, CHECKUP_TOKEN_EXCHANGE_TENANTS=['dev'],
                   CHECKUP_TOKEN_EXCHANGE_MINUTES=60)
class PlatformExchangeTest(TestCase):

    def setUp(self):
        self.tenant = _tenant()   # sandbox_t — НЕ в списке открытых сетей

    @override_settings(CHECKUP_PLATFORM_ADMINS='42')
    def test_whitelisted_user_gets_any_live_tenant_and_platform_claim(self):
        result = perform_exchange(_good())
        self.assertTrue(result['platform'])
        self.assertFalse(result['tenant_open'], 'sandbox_t не в CHECKUP_TOKEN_EXCHANGE_TENANTS')
        self.assertIs(decode_token(result['token'])['platform'], True)
        self.assertEqual(result['user'].username, 'checkup-42-sandbox_t')

    @override_settings(CHECKUP_PLATFORM_ADMINS='1')
    def test_other_user_still_403(self):
        with self.assertRaises(ExchangeError) as cm:
            perform_exchange(_good())
        self.assertEqual((cm.exception.status, cm.exception.code), (403, 'tenant_not_allowed'))

    @override_settings(CHECKUP_PLATFORM_ADMINS='42')
    def test_inactive_tenant_still_404_for_platform(self):
        _tenant(schema='off_t', client_id=7702, active=False)
        with self.assertRaises(ExchangeError) as cm:
            perform_exchange(_good(tenant_schema='off_t'))
        self.assertEqual(cm.exception.status, 404)

    @override_settings(CHECKUP_PLATFORM_ADMINS='42')
    def test_exchange_view_returns_platform_and_tenant_open(self):
        resp = _post(_good(), secret=SECRET)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = json.loads(resp.content)
        self.assertIs(body['platform'], True)
        self.assertIs(body['tenant_open'], False)

    def test_tenants_list_view(self):
        _tenant(schema='off_t', client_id=7702, active=False)
        request = RequestFactory().get('/api/v1/internal/tenants/', REMOTE_ADDR='127.0.0.1',
                                       HTTP_X_LOYALUP_EXCHANGE_SECRET=SECRET)
        resp = InternalTenantsView.as_view()(request)
        self.assertEqual(resp.status_code, 200, resp.content)
        rows = json.loads(resp.content)['tenants']
        schemas = [r['schema'] for r in rows]
        self.assertIn('sandbox_t', schemas)
        self.assertNotIn('off_t', schemas, 'выключенные сети не отдаём')
        row = next(r for r in rows if r['schema'] == 'sandbox_t')
        self.assertEqual(set(row), {'schema', 'name', 'client_id', 'domain', 'is_active', 'paid_until', 'exchange_open'})
        self.assertFalse(row['exchange_open'])
        self.assertEqual(row['paid_until'], '2030-01-01')

    def test_tenants_list_requires_secret(self):
        request = RequestFactory().get('/api/v1/internal/tenants/', REMOTE_ADDR='127.0.0.1',
                                       HTTP_X_LOYALUP_EXCHANGE_SECRET='wrong')
        self.assertEqual(InternalTenantsView.as_view()(request).status_code, 401)

    def test_tenants_list_only_internal(self):
        request = RequestFactory().get('/api/v1/internal/tenants/', REMOTE_ADDR='8.8.8.8',
                                       HTTP_X_LOYALUP_EXCHANGE_SECRET=SECRET)
        self.assertEqual(InternalTenantsView.as_view()(request).status_code, 403)
