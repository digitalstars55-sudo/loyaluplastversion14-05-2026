"""
Телефон гостя с согласием через ВКонтакте (карта переезда, №78).

Мини-апп вызывает bridge `VKWebAppGetPhoneNumber`; ВКонтакте показывает гостю
окно согласия и возвращает `{phone_number, sign}`. Подпись по документации ВК:

    sign = base64( sha256( app_id + secret + user_id + "phone_number" + phone_number ) )

где `app_id` — id мини-аппа (`settings.VK_MINI_APP_ID`), `secret` — «Защищённый
ключ» приложения (`settings.VK_SECRET`, тот же, что подписывает launch-параметры),
`user_id` — id гостя. `user_id` берём НЕ из тела запроса, а из проверенных
launch-параметров (`request.vk_user_id`), иначе подпись ничего не доказывает:
подделать тело с чужим id и своей подписью нельзя, а с чужим id без ключа —
тем более.

Кодировка дайджеста в документации описана неоднозначно (base64 «сырых» байт,
base64url без «=», hex), поэтому считаем несколько кандидатов и возвращаем,
какой сошёлся — в режиме наблюдения это уходит в лог, как было с ключом
запуска (`vk_sign.candidate_check`). Пока `GUEST_PHONE_SIGN_ENFORCE=off`,
несовпадение подписи телефон не блокирует, только логируется; после нуля
`mismatch` на живом трафике владелец включает enforce.

Телефон, который ПОДПИСЫВАЕТСЯ, — ровно та строка, что вернул ВК (`phone_number`
как есть). Нормализованный E.164 — только для хранения и склейки с CheckUp.
"""

import base64
import hashlib
import hmac
import re

from django.conf import settings

_DIGITS_RE = re.compile(r'\D+')
_PHONE_SUFFIX = 'phone_number'
MIN_DIGITS = 10
MAX_DIGITS = 15


def guest_phone_enabled() -> bool:
    """
    Флаг фичи целиком: общий выключатель платформы (`GUEST_PHONE_ENABLED`) И флаг
    сети (`ClientConfig.guest_phone_enabled` текущего тенанта). Любой выкл →
    ручка отвечает 404, кнопки в мини-аппе нет, поля не заполняются.
    """
    if not getattr(settings, 'GUEST_PHONE_ENABLED', False):
        return False
    return tenant_guest_phone_enabled()


def tenant_guest_phone_enabled() -> bool:
    """Флаг сети из ClientConfig текущего тенанта; вне тенанта (public) — False."""
    from django.db import connection
    tenant = getattr(connection, 'tenant', None)
    # RelatedObjectDoesNotExist — подкласс AttributeError: конфига нет → None → False.
    config = getattr(tenant, 'config', None) if tenant is not None else None
    return bool(getattr(config, 'guest_phone_enabled', False))


def phone_sign_enforce() -> bool:
    """'on' → несовпадение подписи = отказ; иначе только лог (наблюдение)."""
    return str(getattr(settings, 'GUEST_PHONE_SIGN_ENFORCE', 'off') or 'off').lower() == 'on'


def normalize_phone(raw) -> str | None:
    """
    Телефон в E.164 (`+7XXXXXXXXXX`) или None, если это не телефон.

    Правила для российских номеров: `8XXXXXXXXXX` и `7XXXXXXXXXX` → `+7…`,
    10 цифр с `9` в начале (мобильный без кода страны) → `+7…`. Остальное —
    как есть с плюсом, если длина 10–15 цифр (E.164 допускает до 15).
    """
    if raw is None:
        return None
    digits = _DIGITS_RE.sub('', str(raw))
    if not digits:
        return None
    if len(digits) == 11 and digits[0] in ('7', '8'):
        digits = '7' + digits[1:]
    elif len(digits) == 10 and digits[0] == '9':
        digits = '7' + digits
    if not (MIN_DIGITS <= len(digits) <= MAX_DIGITS):
        return None
    return '+' + digits


def _payload(app_id, secret: str, user_id, phone_number: str) -> bytes:
    return (str(app_id) + secret + str(user_id) + _PHONE_SUFFIX + str(phone_number)).encode('utf-8')


def phone_sign_candidates(app_id, secret: str, user_id, phone_number: str) -> dict[str, str]:
    """Все правдоподобные кодировки дайджеста: имя варианта → подпись."""
    digest = hashlib.sha256(_payload(app_id, secret, user_id, phone_number)).digest()
    b64 = base64.b64encode(digest).decode('ascii')
    return {
        'b64': b64,
        'b64_nopad': b64.rstrip('='),
        'b64url': base64.urlsafe_b64encode(digest).decode('ascii'),
        'b64url_nopad': base64.urlsafe_b64encode(digest).decode('ascii').rstrip('='),
        'hex': digest.hex(),
    }


def check_phone_sign(user_id, phone_number, sign) -> str:
    """
    Проверка подписи телефона: 'off' | 'ok:<вариант>' | 'mismatch'.

    'off' — не задан `VK_SECRET` (или `VK_MINI_APP_ID`): проверять нечем.
    Функция ничего не решает — решение (отказ или только лог) принимает вызывающий
    по `phone_sign_enforce()`.
    """
    secret = getattr(settings, 'VK_SECRET', '') or ''
    app_id = getattr(settings, 'VK_MINI_APP_ID', None)
    if not secret or app_id in (None, ''):
        return 'off'
    if not sign or not phone_number or not user_id:
        return 'mismatch'
    sign = str(sign).strip()
    for name, candidate in phone_sign_candidates(app_id, secret, user_id, phone_number).items():
        if hmac.compare_digest(candidate, sign):
            return 'ok:' + name
    return 'mismatch'
