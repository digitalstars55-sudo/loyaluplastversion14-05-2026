"""
Тесты проверки подписи запуска мини-аппа ВКонтакте (vk_sign + middleware).

Без БД: RequestFactory + SimpleTestCase. Подпись в тестах считается тем же
алгоритмом (`calc_sign`) на тестовом секрете через override_settings.
"""

import json
from urllib.parse import urlencode

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from .middleware import VKLaunchParamsMiddleware
from .vk_sign import calc_sign, extract_vk_user_id, verify_launch_params

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
