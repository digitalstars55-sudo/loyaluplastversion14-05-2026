"""
Проверка подписи launch-параметров мини-приложения ВКонтакте.

Официальный алгоритм VK Mini Apps: из query-строки запуска берутся ВСЕ параметры
с префиксом `vk_` (в том числе `vk_testing_group_id` — выкидывать нельзя, подпись
покрывает весь набор), сортируются по ключу, склеиваются `urlencode`, подписываются
HMAC-SHA256 на защищённом ключе приложения (`settings.VK_SECRET`). Результат —
base64 urlsafe без padding — сравнивается с параметром `sign`.

Свежесть `vk_ts` не проверяется: ВКонтакте этого не требует, параметры запуска
живут столько же, сколько открытая вкладка мини-аппа.

`settings.VK_WEB_APP_ID` (VK ID OAuth веб-версии) к этому модулю отношения не имеет —
подпись выдаётся только мини-аппу `settings.VK_MINI_APP_ID`.
"""

import base64
import hashlib
import hmac
from urllib.parse import parse_qsl, urlencode

from django.conf import settings

VK_PREFIX = 'vk_'


def calc_sign(vk_params, secret: str) -> str:
    """Подпись набора `vk_`-параметров: отсортировали → urlencode → HMAC-SHA256 → base64url."""
    pairs = list(vk_params.items()) if isinstance(vk_params, dict) else list(vk_params)
    pairs = [(key, value) for key, value in pairs if key.startswith(VK_PREFIX)]
    pairs.sort(key=lambda kv: kv[0])
    payload = urlencode(pairs, doseq=True)
    digest = hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode('utf-8').rstrip('=')


def app_id_matches(raw_app_id) -> bool:
    """`vk_app_id` из подписи == наш мини-апп. Не настроенный ID проверку не валит."""
    expected = getattr(settings, 'VK_MINI_APP_ID', None)
    try:
        expected_int = int(str(expected).strip())
    except (TypeError, ValueError):
        return True
    try:
        return int(str(raw_app_id).strip()) == expected_int
    except (TypeError, ValueError):
        return False


def verify_launch_params(query_string: str) -> dict | None:
    """
    Проверяет query-строку запуска мини-аппа.

    Возвращает словарь валидных `vk_`-параметров либо None (нет подписи,
    подпись не сошлась, чужой `vk_app_id`, не задан `VK_SECRET`).
    """
    if not query_string:
        return None

    secret = getattr(settings, 'VK_SECRET', '') or ''
    if not secret:
        return None

    pairs = parse_qsl(query_string.lstrip('?'), keep_blank_values=True)
    if not pairs:
        return None

    sign = None
    vk_pairs: list[tuple[str, str]] = []
    for key, value in pairs:
        if key == 'sign':
            sign = value
        elif key.startswith(VK_PREFIX):
            vk_pairs.append((key, value))

    if not sign or not vk_pairs:
        return None

    if not hmac.compare_digest(calc_sign(vk_pairs, secret), sign):
        return None

    params = dict(vk_pairs)
    if not app_id_matches(params.get('vk_app_id')):
        return None
    return params


def extract_vk_user_id(params: dict | None) -> int | None:
    """`vk_user_id` из проверенных параметров запуска — целым числом."""
    if not params:
        return None
    try:
        value = int(str(params.get('vk_user_id')).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None
