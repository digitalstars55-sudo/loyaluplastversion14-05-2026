"""
Токен веб-сессии гостя — доказательство личности ВНЕ ВКонтакте.

Внутри мини-аппа ВК личность гостя доказывает подпись launch-параметров
(`vk_sign.py` + `VKLaunchParamsMiddleware`). Вне ВК (браузер, Телеграм)
launch-параметров нет в принципе: гость логинится через VK ID OAuth
(`POST /api/v1/vk/auth/`), и дальше ему нужно СВОЁ доказательство — иначе
гостевое API снова верит `vk_id` из тела запроса на слово.

Токен — подписанная строка (`django.core.signing`, ключ = `SECRET_KEY`,
salt = `guest-web-session`), внутри только `vk_id` и версия формата. Состояния
на сервере нет: проверка = проверка подписи и срока, ни одного запроса в БД.

Срок жизни — `settings.WEB_SESSION_TTL_DAYS` дней (по умолчанию 30). Отзыв
одного конкретного токена невозможен: «отзыв» всей выдачи = смена `SECRET_KEY`.
Это осознанно — токен доказывает личность (как подпись запуска ВК), а не несёт
прав; бан гостя по-прежнему проверяется вьюхами по `guest.Client.is_active`.

Фронт кладёт токен в заголовок `X-Web-Session`; middleware проверяет его и
ставит `request.vk_user_id` со статусом `web` (см. `middleware.py`).
"""

from datetime import datetime, timedelta

from django.conf import settings
from django.core import signing
from django.utils import timezone

# Salt разводит подпись этого токена с любой другой подписью на том же SECRET_KEY.
SALT = 'guest-web-session'

# Дефолт живёт здесь же: модуль обязан работать и без переменной в settings.
DEFAULT_TTL_DAYS = 30

# Версия формата полезной нагрузки — на случай будущих полей (device, scope).
TOKEN_VERSION = 1


def ttl_days() -> int:
    """Срок жизни токена в днях: `settings.WEB_SESSION_TTL_DAYS` либо 30."""
    raw = getattr(settings, 'WEB_SESSION_TTL_DAYS', DEFAULT_TTL_DAYS)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_TTL_DAYS


def issue_web_token(vk_id: int) -> tuple[str, datetime]:
    """
    Выдаёт токен веб-сессии гостя.

    Returns:
        (token, expires_at) — строка токена и момент протухания (aware datetime).

    Raises:
        ValueError — vk_id не положительное целое.
    """
    try:
        value = int(vk_id)
    except (TypeError, ValueError):
        raise ValueError('vk_id должен быть целым числом')
    if value <= 0:
        raise ValueError('vk_id должен быть положительным')

    token = signing.dumps({'vk_id': value, 'v': TOKEN_VERSION}, salt=SALT)
    return token, timezone.now() + timedelta(days=ttl_days())


def verify_web_token(token: str) -> int | None:
    """
    Проверяет токен веб-сессии.

    Returns:
        `vk_id` — подпись сошлась и срок не вышел;
        None    — токена нет, подпись не сошлась, формат битый или токен протух.
    """
    if not token or not isinstance(token, str):
        return None

    try:
        data = signing.loads(
            token.strip(),
            salt=SALT,
            max_age=timedelta(days=ttl_days()),
        )
    except signing.BadSignature:      # включая SignatureExpired
        return None
    except Exception:                 # битый base64 / не-JSON — тоже «нет токена»
        return None

    if not isinstance(data, dict):
        return None
    try:
        vk_id = int(data.get('vk_id'))
    except (TypeError, ValueError):
        return None
    return vk_id if vk_id > 0 else None
