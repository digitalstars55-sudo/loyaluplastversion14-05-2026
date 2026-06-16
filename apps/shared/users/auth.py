"""
JWT-аутентификация для мобильного приложения LoyaltyUP.

Аддитивный модуль — НЕ ЗАМЕНЯЕТ существующую SessionAuthentication, на которой
работает веб-панель. Регистрируется ВТОРЫМ в DEFAULT_AUTHENTICATION_CLASSES
(см. settings.py), поэтому если у запроса нет Authorization-заголовка, DRF
просто переходит к Session-бэкенду — и веб-админка работает как работала.

Используем PyJWT, который уже в requirements.txt. Никаких внешних зависимостей.
"""

from __future__ import annotations

import datetime
import hmac
from typing import Optional, Tuple

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import authentication, exceptions

User = get_user_model()

# ── Конфиг ──────────────────────────────────────────────────────────
ACCESS_TOKEN_LIFETIME_HOURS = 24 * 30      # 30 дней — мобайл редко логинится
REFRESH_TOKEN_LIFETIME_DAYS = 90
JWT_ALGORITHM = 'HS256'
JWT_ISSUER = 'loyalup-mobile'

def _jwt_secret() -> str:
    """Используем SECRET_KEY Django — он уже настроен и хранится в env."""
    return settings.SECRET_KEY


# ── Генерация токенов ───────────────────────────────────────────────
def issue_access_token(user) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        'sub': str(user.pk),
        'username': user.username,
        'role': getattr(user, 'role', 'client'),
        'iat': int(now.timestamp()),
        'exp': int((now + datetime.timedelta(hours=ACCESS_TOKEN_LIFETIME_HOURS)).timestamp()),
        'iss': JWT_ISSUER,
        'typ': 'access',
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def issue_refresh_token(user) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        'sub': str(user.pk),
        'iat': int(now.timestamp()),
        'exp': int((now + datetime.timedelta(days=REFRESH_TOKEN_LIFETIME_DAYS)).timestamp()),
        'iss': JWT_ISSUER,
        'typ': 'refresh',
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(
        token,
        _jwt_secret(),
        algorithms=[JWT_ALGORITHM],
        issuer=JWT_ISSUER,
        options={'require': ['exp', 'sub', 'typ']},
    )


# ── DRF authentication class ────────────────────────────────────────
class JWTAuthentication(authentication.BaseAuthentication):
    """
    Authorization: Bearer <access_token>

    Если заголовка нет — возвращаем None, и DRF спокойно переходит на
    следующий бэкенд (Session). Это и есть «не ломаем веб».
    """
    keyword = 'Bearer'

    def authenticate(self, request) -> Optional[Tuple[object, str]]:
        auth_header = authentication.get_authorization_header(request).split()
        if not auth_header or auth_header[0].lower() != self.keyword.lower().encode():
            return None
        if len(auth_header) == 1:
            raise exceptions.AuthenticationFailed('Invalid token header. No credentials provided.')
        if len(auth_header) > 2:
            raise exceptions.AuthenticationFailed('Invalid token header. Token string contains spaces.')
        try:
            token = auth_header[1].decode('utf-8')
        except UnicodeDecodeError:
            raise exceptions.AuthenticationFailed('Invalid token header. Token contains invalid characters.')

        try:
            payload = decode_token(token)
        except jwt.ExpiredSignatureError:
            raise exceptions.AuthenticationFailed('Token expired')
        except jwt.InvalidTokenError as e:
            raise exceptions.AuthenticationFailed(f'Invalid token: {e}')

        if payload.get('typ') != 'access':
            raise exceptions.AuthenticationFailed('Wrong token type — expected access')

        try:
            user = User.objects.get(pk=payload['sub'])
        except User.DoesNotExist:
            raise exceptions.AuthenticationFailed('User no longer exists')

        if not user.is_active:
            raise exceptions.AuthenticationFailed('User is inactive')

        return (user, token)

    def authenticate_header(self, request) -> str:
        return self.keyword


# ── Сервис-аутентификация (server-to-server) ────────────────────────
class ServicePrincipal:
    """
    Лёгкий «пользователь» для сервисных вызовов внешнего ordering-BFF.

    Не строка в БД, не Django-User — просто объект, удовлетворяющий
    IsAuthenticated (is_authenticated=True). Прав в админке/тенанте не несёт:
    loyalty-вью самодостаточны и работают по vk_id/branch_id из запроса.
    """
    is_authenticated = True
    is_active = True
    is_staff = False
    is_superuser = False
    is_anonymous = False
    username = 'ordering-bff'
    pk = None
    id = None

    def __str__(self) -> str:
        return 'ordering-bff (service)'


class ServiceKeyAuthentication(authentication.BaseAuthentication):
    """
    Authorization: Bearer <LOYALTY_SERVICE_API_KEY>

    Только для сервисных эндпоинтов (loyalty-API). Подключается ЯВНО через
    `authentication_classes = [ServiceKeyAuthentication]` на вью — в глобальную
    DEFAULT_AUTHENTICATION_CLASSES НЕ добавляется (иначе ловила бы гостевые
    JWT). Сравнение ключа — постоянное по времени (hmac.compare_digest).

    Если ключ не настроен в окружении — fail closed (никого не пускаем).
    """
    keyword = 'Bearer'

    def authenticate(self, request) -> Optional[Tuple[object, str]]:
        auth_header = authentication.get_authorization_header(request).split()
        if not auth_header or auth_header[0].lower() != self.keyword.lower().encode():
            return None
        if len(auth_header) != 2:
            raise exceptions.AuthenticationFailed('Invalid service key header.')
        try:
            token = auth_header[1].decode('utf-8')
        except UnicodeDecodeError:
            raise exceptions.AuthenticationFailed('Invalid service key encoding.')

        expected = getattr(settings, 'LOYALTY_SERVICE_API_KEY', None)
        if not expected:
            raise exceptions.AuthenticationFailed('Service auth is not configured.')
        if not hmac.compare_digest(token, str(expected)):
            raise exceptions.AuthenticationFailed('Invalid service key.')

        return (ServicePrincipal(), token)

    def authenticate_header(self, request) -> str:
        return self.keyword
