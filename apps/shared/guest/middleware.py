"""
VKLaunchParamsMiddleware — серверная проверка подписи запуска мини-аппа ВКонтакте.

Гостевое API (`/api/v1/client/`, game, inventory, quest, catalog, delivery,
discovery) — `AllowAny` и исторически верит `vk_id` из тела/query: любой мог
представиться чужим гостем. Фронт мини-аппа шлёт полную query-строку запуска
(с `sign`) заголовком `X-VK-Launch-Params`; здесь она проверяется, и в
`request.vk_user_id` кладётся ДОКАЗАННЫЙ VK ID.

Статусы (`request.vk_sign_status`):
  valid        — подпись сошлась, `request.vk_user_id` заполнен;
  invalid      — заголовок есть, но подпись/`vk_app_id` не сошлись;
  missing      — заголовка нет (старый кэш фронта или подделка);
  telegram     — запуск из Телеграм-мини-аппа (loyalupp.ru), подписи ВК там нет;
  unconfigured — не задан `VK_SECRET`, проверять нечем (никогда не блокируем);
  skipped      — путь не гостевой (мобилка, аналитика, вебхуки, админка).

Переходный режим: `VK_SIGN_ENFORCE=off` (дефолт) — только строка в лог
(`grep -c 'vk_sign '` даёт счётчик), `on` — 403 `{"code": "vk_sign_invalid"}`.
Включать `on` только когда лог покажет ~0 легитимных запросов без подписи:
у гостей висит кэш старого фронта, а веб-версия на VK ID OAuth
(`settings.VK_WEB_APP_ID`) launch-параметров не имеет в принципе.
"""

import json
import logging
from urllib.parse import urlparse

from django.conf import settings
from django.http import JsonResponse

from .vk_sign import extract_vk_user_id, verify_launch_params

logger = logging.getLogger(__name__)

LAUNCH_PARAMS_HEADER = 'HTTP_X_VK_LAUNCH_PARAMS'
TELEGRAM_INIT_DATA_HEADER = 'HTTP_X_TELEGRAM_INIT_DATA'

# Гостевые (мини-апп) префиксы. Всё остальное под /api/v1/ ходит со своей
# аутентификацией — JWT мобилки, сервис-ключ лояльности, вебхуки VK/POS — и
# launch-параметров не носит: такие пути не трогаем вовсе.
_GUEST_PREFIXES = (
    '/api/v1/birthday/',
    '/api/v1/branches/',
    '/api/v1/catalog/',
    '/api/v1/client/',
    '/api/v1/code/',
    '/api/v1/discovery/',
    '/api/v1/employees/',
    '/api/v1/game/',
    '/api/v1/inventory/',
    '/api/v1/promotions/',
    '/api/v1/quest/',
    '/api/v1/story/',
    '/api/v1/super-prize/',
    '/api/v1/testimonials/',
    '/api/v1/transactions/',
    '/api/v1/vk/story/',
)

# Исключения внутри гостевых префиксов:
#   catalog/categories|products — CRUD мобилки (JWT), а не витрина гостя;
#   vk/auth|vk/callback         — VK ID OAuth веба и Callback API самого ВК.
_NOT_GUEST_PREFIXES = (
    '/api/v1/catalog/categories',
    '/api/v1/catalog/products',
    '/api/v1/vk/auth',
    '/api/v1/vk/callback',
)

# Тело читаем только у JSON-запросов и только небольшое: multipart не трогаем
# вовсе (иначе сломается парсинг файлов во вьюхе).
_MAX_BODY_BYTES = 64 * 1024


def _hostname(raw: str) -> str:
    try:
        return (urlparse(raw).hostname or '').lower()
    except ValueError:
        return ''


class VKLaunchParamsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.vk_user_id = None
        request.vk_sign_status = 'skipped'

        if request.method == 'OPTIONS' or not self._is_guest_api(request.path or ''):
            return self.get_response(request)

        try:
            status, vk_user_id, violation, claimed = self._inspect(request)
        except Exception:
            # Проверка подписи не имеет права ронять гостевое API.
            logger.exception('VKLaunchParamsMiddleware failed path=%s', request.path)
            return self.get_response(request)

        request.vk_sign_status = status
        request.vk_user_id = vk_user_id

        if violation is None:
            return self.get_response(request)

        enforce = str(getattr(settings, 'VK_SIGN_ENFORCE', 'off')).strip().lower() == 'on'
        logger.warning(
            'vk_sign %s path=%s method=%s status=%s vk_user_id=%s claimed_vk_id=%s '
            'origin=%s enforce=%s',
            violation, request.path, request.method, status, vk_user_id,
            claimed[0] if claimed else '-',
            _hostname(request.META.get('HTTP_ORIGIN', '')) or '-',
            'on' if enforce else 'off',
        )
        if not enforce:
            return self.get_response(request)

        return JsonResponse(
            {'detail': _VIOLATION_DETAIL[violation], 'code': 'vk_sign_invalid'},
            status=403,
        )

    # ── внутреннее ────────────────────────────────────────────────────────────

    def _is_guest_api(self, path: str) -> bool:
        if path.startswith(_NOT_GUEST_PREFIXES):
            return False
        exempt = tuple(getattr(settings, 'VK_SIGN_EXEMPT_PATHS', ()) or ())
        if exempt and path.startswith(exempt):
            return False
        return path.startswith(_GUEST_PREFIXES)

    def _inspect(self, request) -> tuple[str, int | None, str | None, list[int]]:
        """(статус, доказанный vk_user_id, нарушение|None, заявленные vk_id)."""
        raw = request.META.get(LAUNCH_PARAMS_HEADER, '')

        if not raw and self._is_telegram(request):
            # TODO: валидировать Telegram initData (HMAC от токена бота) и
            # включать ТГ в enforce отдельным флагом.
            return 'telegram', None, None, []

        if not (getattr(settings, 'VK_SECRET', '') or ''):
            if raw:
                logger.warning('vk_sign unconfigured path=%s (VK_SECRET пуст)', request.path)
            return 'unconfigured', None, None, []

        # vk_id из query и JSON-тела нужен и в enforce, и в счётчике переходного режима.
        claimed = self._claimed_vk_ids(request)
        if not raw:
            return 'missing', None, 'missing', claimed

        params = verify_launch_params(raw)
        vk_user_id = extract_vk_user_id(params)
        if vk_user_id is None:
            return 'invalid', None, 'invalid', claimed

        if any(value != vk_user_id for value in claimed):
            return 'valid', vk_user_id, 'vk_id_mismatch', claimed
        return 'valid', vk_user_id, None, claimed

    def _is_telegram(self, request) -> bool:
        """
        Телеграм-мини-апп (loyalupp.ru) крутит тот же бандл, подписи ВК у него нет.
        Признаки: заголовок initData либо origin/referer/host из списка ТГ-доменов
        (API живёт на домене тенанта, поэтому Host сам по себе ТГ не выдаёт).
        """
        if request.META.get(TELEGRAM_INIT_DATA_HEADER):
            return True
        hosts = tuple(getattr(settings, 'TELEGRAM_MINI_APP_HOSTS', ()) or ())
        if not hosts:
            return False
        for header in ('HTTP_ORIGIN', 'HTTP_REFERER'):
            if _hostname(request.META.get(header, '')) in hosts:
                return True
        try:
            return request.get_host().split(':')[0].lower() in hosts
        except Exception:
            return False

    def _claimed_vk_ids(self, request) -> list[int]:
        """Все `vk_id`, которые запрос выдаёт за свои — из query и из JSON-тела."""
        raw_values = [request.GET.get('vk_id')]
        body = self._json_body(request)
        if body is not None:
            raw_values.append(body.get('vk_id'))

        claimed = []
        for raw in raw_values:
            if raw is None:
                continue
            try:
                claimed.append(int(str(raw).strip()))
            except (TypeError, ValueError):
                continue
        return claimed

    def _json_body(self, request) -> dict | None:
        if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return None
        if not (request.content_type or '').startswith('application/json'):
            return None
        try:
            if int(request.META.get('CONTENT_LENGTH') or 0) > _MAX_BODY_BYTES:
                return None
        except (TypeError, ValueError):
            return None
        try:
            # request.body кэшируется в request._body — DRF ниже по стеку читает
            # тело из этого кэша, повторного чтения потока не происходит.
            data = json.loads(request.body or b'{}')
        except Exception:
            return None
        return data if isinstance(data, dict) else None


_VIOLATION_DETAIL = {
    'missing': 'Запуск без подписи ВКонтакте.',
    'invalid': 'Подпись запуска ВКонтакте не прошла проверку.',
    'vk_id_mismatch': 'vk_id не совпадает с подписью запуска.',
}
