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
