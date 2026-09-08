"""
Тесты доказательств личности гостя: подпись запуска мини-аппа ВК (vk_sign),
токен веб-сессии вне ВК (web_session) и общий middleware над ними.

Без БД: RequestFactory + SimpleTestCase. Подпись в тестах считается тем же
алгоритмом (`calc_sign`) на тестовом секрете через override_settings; веб-токен
подписывается настоящим `SECRET_KEY` тестовых настроек.
"""

import json
import time
from datetime import timedelta
from urllib.parse import urlencode

from django.core import signing
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils import timezone

from . import web_session
from .middleware import VKLaunchParamsMiddleware
from .vk_sign import calc_sign, extract_vk_user_id, verify_launch_params
from .web_session import issue_web_token, verify_web_token

SECRET = 'test-secret-71-chars-not-the-real-one'
APP_ID = 53418653

# Реальный набор запуска: vk_testing_group_id участвует в подписи наравне с прочими.
BASE_PARAMS = {
    'vk_access_token_settings': '',
    'vk_app_id': str(APP_ID),
    'vk_are_notifications_enabled': '0',
    'vk_is_app_user': '1',
    'vk_is_favorite': '0',
    'vk_language': 'ru',
    'vk_platform': 'mobile_android',
    'vk_ref': 'other',
    'vk_testing_group_id': '17',
    'vk_ts': '1755950000',
    'vk_user_id': '200002444631',
}


def launch_query(params: dict | None = None, secret: str = SECRET,
                 extra: dict | None = None, sign: str | None = None) -> str:
    """Строка запуска: vk_-параметры + подпись (+ посторонние параметры фронта)."""
    params = dict(BASE_PARAMS if params is None else params)
    pairs = sorted(params.items())
    if extra:
        pairs += list(extra.items())
    pairs.append(('sign', sign if sign is not None else calc_sign(params, secret)))
    return urlencode(pairs)


@override_settings(VK_SECRET=SECRET, VK_MINI_APP_ID=APP_ID)
class VerifyLaunchParamsTest(SimpleTestCase):
    def test_valid_sign_returns_params(self):
        params = verify_launch_params(launch_query())
        self.assertIsNotNone(params)
        self.assertEqual(extract_vk_user_id(params), 200002444631)
        self.assertEqual(params['vk_testing_group_id'], '17')
        self.assertNotIn('sign', params)

    def test_leading_question_mark_is_accepted(self):
        self.assertIsNotNone(verify_launch_params('?' + launch_query()))

    def test_foreign_params_do_not_break_sign(self):
        """Не-vk параметры (utm, company, branch) в подписи не участвуют."""
        query = launch_query(extra={'company': '7', 'branch': '42', 'utm_source': 'qr'})
        self.assertIsNotNone(verify_launch_params(query))

    def test_tampered_value_is_rejected(self):
        tampered = dict(BASE_PARAMS, vk_user_id='111')
        query = launch_query(params=tampered, sign=calc_sign(BASE_PARAMS, SECRET))
        self.assertIsNone(verify_launch_params(query))

    def test_broken_sign_is_rejected(self):
        self.assertIsNone(verify_launch_params(launch_query(sign='deadbeef')))

    def test_foreign_secret_is_rejected(self):
        self.assertIsNone(verify_launch_params(launch_query(secret='другой-секрет')))

    def test_missing_sign_is_rejected(self):
        self.assertIsNone(verify_launch_params(urlencode(sorted(BASE_PARAMS.items()))))

    def test_empty_query_is_rejected(self):
        self.assertIsNone(verify_launch_params(''))

    def test_dropped_testing_group_id_is_rejected(self):
        """Подпись покрывает весь набор vk_*: выкинуть vk_testing_group_id нельзя."""
        without = {k: v for k, v in BASE_PARAMS.items() if k != 'vk_testing_group_id'}
        query = launch_query(params=BASE_PARAMS, sign=calc_sign(without, SECRET))
        self.assertIsNone(verify_launch_params(query))

    def test_foreign_app_id_is_rejected(self):
        """Подпись валидна, но выдана другому приложению (например веб-VK ID)."""
        other = dict(BASE_PARAMS, vk_app_id='54473505')
        self.assertIsNone(verify_launch_params(launch_query(params=other)))

    @override_settings(VK_SECRET='')
    def test_without_secret_returns_none(self):
        self.assertIsNone(verify_launch_params(launch_query()))


@override_settings(
    VK_SECRET=SECRET,
    VK_MINI_APP_ID=APP_ID,
    TELEGRAM_MINI_APP_HOSTS=('loyalupp.ru',),
    VK_SIGN_EXEMPT_PATHS=(),
)
class VKLaunchParamsMiddlewareTest(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.seen = {}

        def get_response(request):
            self.seen['status'] = request.vk_sign_status
            self.seen['vk_user_id'] = request.vk_user_id
            self.seen['body'] = self._downstream_body(request)
            return HttpResponse('ok')

        self.middleware = VKLaunchParamsMiddleware(get_response)

    @staticmethod
    def _downstream_body(request):
        """Эмулирует DRF: тело должно читаться и ПОСЛЕ middleware."""
        if (request.content_type or '').startswith('application/json'):
            try:
                return json.loads(request.body or b'{}')
            except Exception:
                return None
        if (request.content_type or '').startswith('multipart/form-data'):
            return request.POST.dict()
        return None

    def _get(self, path='/api/v1/client/', header=None, **params):
        kwargs = {'HTTP_X_VK_LAUNCH_PARAMS': header} if header else {}
        return self.middleware(self.factory.get(path, params, **kwargs))

    def _post_json(self, data, path='/api/v1/client/', header=None, **extra):
        kwargs = {'HTTP_X_VK_LAUNCH_PARAMS': header} if header else {}
        kwargs.update(extra)
        request = self.factory.post(
            path, data=json.dumps(data), content_type='application/json', **kwargs
        )
        return self.middleware(request)

    # ── подпись ───────────────────────────────────────────────────────────────

    def test_valid_sign_sets_vk_user_id(self):
        response = self._get(header=launch_query(), vk_id='200002444631')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen['status'], 'valid')
        self.assertEqual(self.seen['vk_user_id'], 200002444631)

    def test_missing_header_passes_when_enforce_off(self):
        response = self._get(vk_id='111')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen['status'], 'missing')
        self.assertIsNone(self.seen['vk_user_id'])

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_missing_header_is_403_when_enforce_on(self):
        response = self._get(vk_id='111')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content)['code'], 'vk_sign_invalid')

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_broken_sign_is_403_when_enforce_on(self):
        response = self._get(header=launch_query(sign='deadbeef'), vk_id='200002444631')
        self.assertEqual(response.status_code, 403)

    def test_broken_sign_passes_when_enforce_off(self):
        response = self._get(header=launch_query(sign='deadbeef'), vk_id='200002444631')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen['status'], 'invalid')

    # ── подмена vk_id ─────────────────────────────────────────────────────────

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_foreign_vk_id_in_query_is_403(self):
        response = self._get(header=launch_query(), vk_id='111')
        self.assertEqual(response.status_code, 403)

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_foreign_vk_id_in_json_body_is_403(self):
        response = self._post_json({'vk_id': 111, 'branch_id': 42}, header=launch_query())
        self.assertEqual(response.status_code, 403)

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_own_vk_id_in_json_body_passes(self):
        response = self._post_json(
            {'vk_id': 200002444631, 'branch_id': 42}, header=launch_query()
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen['status'], 'valid')

    def test_json_body_is_still_readable_downstream(self):
        self._post_json({'vk_id': 200002444631, 'branch_id': 42}, header=launch_query())
        self.assertEqual(self.seen['body'], {'vk_id': 200002444631, 'branch_id': 42})

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_multipart_body_is_not_parsed(self):
        """multipart не трогаем: проверяем только query, поток остаётся вью."""
        request = self.factory.post(
            '/api/v1/testimonials/', data={'vk_id': '111', 'text': 'ok'},
            HTTP_X_VK_LAUNCH_PARAMS=launch_query(),
        )
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen['body'], {'vk_id': '111', 'text': 'ok'})

    # ── Телеграм и не-гостевые пути ───────────────────────────────────────────

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_telegram_launch_is_not_enforced(self):
        # Без ТГ-признака тот же запрос блокируется — сравниваем поведение.
        self.assertEqual(self._get(vk_id='111').status_code, 403)

        request = self.factory.get(
            '/api/v1/client/', {'vk_id': '111'}, HTTP_ORIGIN='https://loyalupp.ru'
        )
        response = self.middleware(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen['status'], 'telegram')

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_telegram_init_data_header_is_not_enforced(self):
        request = self.factory.get(
            '/api/v1/client/', {'vk_id': '111'}, HTTP_X_TELEGRAM_INIT_DATA='user=%7B%7D&hash=x'
        )
        self.assertEqual(self.middleware(request).status_code, 200)
        self.assertEqual(self.seen['status'], 'telegram')

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_non_guest_paths_are_skipped(self):
        for path in (
            '/api/v1/mobile/branches/',        # мобилка сотрудника (JWT)
            '/api/v1/catalog/products/',       # CRUD мобилки, не витрина гостя
            '/api/v1/vk/auth/',                # VK ID OAuth веб-версии
            '/api/v1/vk/callback/',            # Callback API самого ВК
            '/api/v1/loyalty/balance',         # сервис-API ordering-BFF
            '/admin/branch/branch/',
        ):
            with self.subTest(path=path):
                response = self.middleware(self.factory.get(path))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.seen['status'], 'skipped')

    @override_settings(VK_SIGN_ENFORCE='on', VK_SIGN_EXEMPT_PATHS=('/api/v1/client/',))
    def test_exempt_path_is_skipped(self):
        self.assertEqual(self._get(vk_id='111').status_code, 200)
        self.assertEqual(self.seen['status'], 'skipped')

    @override_settings(VK_SIGN_ENFORCE='on', VK_SECRET='')
    def test_unconfigured_secret_never_blocks(self):
        self.assertEqual(self._get(vk_id='111').status_code, 200)
        self.assertEqual(self.seen['status'], 'unconfigured')


# ── Веб-сессия вне ВК (web_session.py) ────────────────────────────────────────

def expired_web_token(vk_id: int = 200002444631, days_ago: int = 31) -> str:
    """
    Токен, подписанный «в прошлом».

    freezegun в requirements нет, глобальное время не трогаем: `TimestampSigner`
    берёт метку из собственного метода `timestamp()` — подменяем его на одном
    инстансе. Всё остальное (ключ, salt, сериализация) — как у `signing.dumps`.
    """
    signer = signing.TimestampSigner(salt=web_session.SALT)
    signer.timestamp = lambda: signing.b62_encode(int(time.time()) - days_ago * 86400)
    return signer.sign_object({'vk_id': vk_id, 'v': web_session.TOKEN_VERSION})


class WebSessionTokenTest(SimpleTestCase):
    VK_ID = 200002444631

    def test_issue_then_verify_returns_same_vk_id(self):
        token, _ = issue_web_token(self.VK_ID)
        self.assertEqual(verify_web_token(token), self.VK_ID)

    def test_issued_token_is_a_non_empty_string(self):
        token, _ = issue_web_token(self.VK_ID)
        self.assertIsInstance(token, str)
        self.assertTrue(token)
        # Токен подписан, а не зашифрован: смысл не в скрытности vk_id, а в том,
        # что подделать его нельзя (см. test_tampered_token_is_rejected).

    @override_settings(WEB_SESSION_TTL_DAYS=30)
    def test_expires_at_is_now_plus_ttl(self):
        _, expires_at = issue_web_token(self.VK_ID)
        delta = expires_at - timezone.now()
        self.assertGreater(delta, timedelta(days=29, hours=23))
        self.assertLess(delta, timedelta(days=30, minutes=1))

    @override_settings(WEB_SESSION_TTL_DAYS=7)
    def test_expires_at_follows_settings_ttl(self):
        _, expires_at = issue_web_token(self.VK_ID)
        delta = expires_at - timezone.now()
        self.assertGreater(delta, timedelta(days=6, hours=23))
        self.assertLess(delta, timedelta(days=7, minutes=1))

    @override_settings(WEB_SESSION_TTL_DAYS=30)
    def test_expired_token_is_rejected(self):
        """Токен старше TTL (30 дней по умолчанию) не принимается."""
        self.assertIsNone(verify_web_token(expired_web_token(days_ago=31)))

    @override_settings(WEB_SESSION_TTL_DAYS=60)
    def test_ttl_setting_extends_lifetime(self):
        """Тот же 31-дневный токен при TTL=60 ещё живой — срок берётся из settings."""
        self.assertEqual(verify_web_token(expired_web_token(days_ago=31)), self.VK_ID)

    @override_settings(WEB_SESSION_TTL_DAYS=0)
    def test_zero_ttl_expires_immediately(self):
        token, _ = issue_web_token(self.VK_ID)
        self.assertIsNone(verify_web_token(token))

    def test_tampered_token_is_rejected(self):
        token, _ = issue_web_token(self.VK_ID)
        self.assertIsNone(verify_web_token(token[:-1] + ('a' if token[-1] != 'a' else 'b')))

    def test_garbage_token_is_rejected(self):
        for value in ('', '   ', 'deadbeef', 'a:b:c', None, 12345):
            with self.subTest(value=value):
                self.assertIsNone(verify_web_token(value))

    def test_token_from_another_secret_key_is_rejected(self):
        token, _ = issue_web_token(self.VK_ID)
        with override_settings(SECRET_KEY='совсем-другой-ключ', SECRET_KEY_FALLBACKS=[]):
            self.assertIsNone(verify_web_token(token))

    def test_token_with_foreign_salt_is_rejected(self):
        """Подпись на том же SECRET_KEY, но с чужим salt — не наша сессия."""
        alien = signing.dumps({'vk_id': self.VK_ID}, salt='другая-соль')
        self.assertIsNone(verify_web_token(alien))

    def test_payload_without_vk_id_is_rejected(self):
        self.assertIsNone(verify_web_token(signing.dumps({'v': 1}, salt=web_session.SALT)))

    def test_non_positive_vk_id_is_refused_on_issue(self):
        for value in (0, -5):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    issue_web_token(value)


@override_settings(
    VK_SECRET=SECRET,
    VK_MINI_APP_ID=APP_ID,
    TELEGRAM_MINI_APP_HOSTS=('loyalupp.ru',),
    VK_SIGN_EXEMPT_PATHS=(),
)
class WebSessionMiddlewareTest(SimpleTestCase):
    """`X-Web-Session` в VKLaunchParamsMiddleware: веб-гость вне ВК."""

    VK_ID = 200002444631
    OTHER_VK_ID = 111

    def setUp(self):
        self.factory = RequestFactory()
        self.seen = {}

        def get_response(request):
            self.seen['status'] = request.vk_sign_status
            self.seen['vk_user_id'] = request.vk_user_id
            return HttpResponse('ok')

        self.middleware = VKLaunchParamsMiddleware(get_response)
        self.token, _ = issue_web_token(self.VK_ID)

    @staticmethod
    def _headers(web=None, launch=None):
        headers = {}
        if web is not None:
            headers['HTTP_X_WEB_SESSION'] = web
        if launch is not None:
            headers['HTTP_X_VK_LAUNCH_PARAMS'] = launch
        return headers

    def _get(self, path='/api/v1/client/', web=None, launch=None, params=None, **extra):
        kwargs = self._headers(web, launch)
        kwargs.update(extra)
        return self.middleware(self.factory.get(path, params or {}, **kwargs))

    def _post_json(self, data, path='/api/v1/client/', web=None, launch=None):
        request = self.factory.post(
            path, data=json.dumps(data), content_type='application/json',
            **self._headers(web, launch),
        )
        return self.middleware(request)

    # ── валидный токен ────────────────────────────────────────────────────────

    def test_valid_web_token_sets_vk_user_id(self):
        response = self._get(web=self.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen['status'], 'web')
        self.assertEqual(self.seen['vk_user_id'], self.VK_ID)

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_valid_web_token_passes_enforce(self):
        response = self._get(web=self.token, params={'vk_id': str(self.VK_ID)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen['status'], 'web')

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_own_vk_id_in_json_body_passes(self):
        response = self._post_json({'vk_id': self.VK_ID, 'branch_id': 42}, web=self.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen['vk_user_id'], self.VK_ID)

    @override_settings(VK_SIGN_ENFORCE='on', VK_SECRET='')
    def test_web_token_does_not_depend_on_vk_secret(self):
        """Токен подписан SECRET_KEY — пустой VK_SECRET веб-гостю не мешает."""
        response = self._get(web=self.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen['status'], 'web')
        self.assertEqual(self.seen['vk_user_id'], self.VK_ID)

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_web_token_wins_over_telegram_origin(self):
        """Тот же бандл в Телеграме, но с доказанной личностью — это уже `web`."""
        response = self._get(web=self.token, HTTP_ORIGIN='https://loyalupp.ru')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen['status'], 'web')
        self.assertEqual(self.seen['vk_user_id'], self.VK_ID)

    # ── подмена vk_id ─────────────────────────────────────────────────────────

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_foreign_vk_id_in_json_body_is_403(self):
        response = self._post_json(
            {'vk_id': self.OTHER_VK_ID, 'branch_id': 42}, web=self.token
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content)['code'], 'vk_sign_invalid')

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_foreign_vk_id_in_query_is_403(self):
        response = self._get(web=self.token, params={'vk_id': str(self.OTHER_VK_ID)})
        self.assertEqual(response.status_code, 403)

    def test_foreign_vk_id_only_warns_when_enforce_off(self):
        with self.assertLogs('apps.shared.guest.middleware', level='WARNING') as logs:
            response = self._get(web=self.token, params={'vk_id': str(self.OTHER_VK_ID)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen['status'], 'web')
        self.assertIn('vk_id_mismatch', logs.output[0])

    # ── битый / протухший токен ───────────────────────────────────────────────

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_broken_web_token_is_403_when_enforce_on(self):
        response = self._get(web='deadbeef', params={'vk_id': str(self.VK_ID)})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.content)['code'], 'vk_sign_invalid')

    def test_broken_web_token_only_warns_when_enforce_off(self):
        with self.assertLogs('apps.shared.guest.middleware', level='WARNING') as logs:
            response = self._get(web='deadbeef', params={'vk_id': str(self.VK_ID)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen['status'], 'web_invalid')
        self.assertIsNone(self.seen['vk_user_id'])
        self.assertIn('status=web_invalid', logs.output[0])

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_expired_web_token_is_403(self):
        response = self._get(web=expired_web_token(self.VK_ID, days_ago=31))
        self.assertEqual(response.status_code, 403)

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_blank_web_token_header_falls_back_to_missing(self):
        """Пустой заголовок = его нет: прежнее поведение (`missing`)."""
        response = self._get(web='   ', params={'vk_id': str(self.VK_ID)})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            json.loads(response.content)['detail'], 'Запуск без подписи ВКонтакте.'
        )

    # ── приоритет подписи ВК ──────────────────────────────────────────────────

    def test_vk_sign_wins_over_web_token(self):
        """Оба заголовка: решает подпись запуска, vk_user_id берётся из неё."""
        other_token, _ = issue_web_token(self.OTHER_VK_ID)
        response = self._get(web=other_token, launch=launch_query())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen['status'], 'valid')
        self.assertEqual(self.seen['vk_user_id'], self.VK_ID)

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_broken_vk_sign_is_not_rescued_by_web_token(self):
        response = self._get(web=self.token, launch=launch_query(sign='deadbeef'))
        self.assertEqual(response.status_code, 403)

    # ── ничего не изменилось там, где токена нет ──────────────────────────────

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_without_any_header_behaviour_is_unchanged(self):
        self.assertEqual(self._get(params={'vk_id': '111'}).status_code, 403)

    @override_settings(VK_SIGN_ENFORCE='on')
    def test_login_path_is_not_checked(self):
        """Сам вход (vk/auth) идёт без токена — путь не гостевой."""
        response = self._get(path='/api/v1/vk/auth/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.seen['status'], 'skipped')
