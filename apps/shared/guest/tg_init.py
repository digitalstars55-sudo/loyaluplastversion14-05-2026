"""
Проверка `initData` Телеграм-мини-аппа гостя (бот `TELEGRAM_GUEST_BOT_TOKEN`).

Алгоритм Телеграма (core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app):
  secret = HMAC_SHA256(key="WebAppData", msg=<токен бота>)
  hash   = hex(HMAC_SHA256(key=secret, msg=<поля кроме hash, k=v, по алфавиту, через \\n>))

Доказывает только «это пользователь Телеграма N, запуск из нашего бота». VK ID
гостя initData НЕ доказывает: личность гостя в LoyalUP — по-прежнему подпись ВК
или токен веб-сессии (вход через VK ID).
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from django.conf import settings

# Строка initData небольшая; длиннее — не наша.
_MAX_LEN = 4096


def _bot_token() -> str:
    return (getattr(settings, 'TELEGRAM_GUEST_BOT_TOKEN', '') or '').strip()


def is_configured() -> bool:
    return bool(_bot_token())


def verify_init_data(raw: str, now: float | None = None) -> dict | None:
    """
    Поля initData, если подпись сошлась и запуск не старше
    `TELEGRAM_INIT_DATA_MAX_AGE` секунд; иначе None. `user` раскодирован в dict.
    """
    token = _bot_token()
    if not token or not raw or len(raw) > _MAX_LEN:
        return None
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    fields = dict(pairs)
    if len(fields) != len(pairs):
        return None  # повтор ключа — не от Телеграма
    received = fields.pop('hash', '')
    if not received:
        return None

    check = '\n'.join(f'{k}={fields[k]}' for k in sorted(fields))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received.lower()):
        return None

    try:
        auth_date = int(fields.get('auth_date', ''))
    except ValueError:
        return None
    max_age = int(getattr(settings, 'TELEGRAM_INIT_DATA_MAX_AGE', 86400) or 0)
    current = time.time() if now is None else now
    if max_age and current - auth_date > max_age:
        return None

    if 'user' in fields:
        try:
            fields['user'] = json.loads(fields['user'])
        except ValueError:
            return None
    return fields


def telegram_user_id(fields: dict | None) -> int | None:
    user = (fields or {}).get('user')
    if not isinstance(user, dict):
        return None
    try:
        return int(user.get('id'))
    except (TypeError, ValueError):
        return None
