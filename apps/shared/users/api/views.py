"""
Auth API для мобильного приложения LoyaltyUP.

Эти view *аддитивные*: они НЕ заменяют веб-логин. Веб-админка
продолжает работать через Django sessions; мобайл — через JWT.

Поведение, при котором web НЕ ломается:
- DEFAULT_AUTHENTICATION_CLASSES = [JWTAuthentication, SessionAuthentication]
- JWT-класс возвращает None если нет Bearer-заголовка → DRF спокойно
  перебрасывает запрос на Session-бэкенд.
"""

from __future__ import annotations

import datetime

from django.contrib.auth import authenticate as django_authenticate
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.users.auth import (
    ACCESS_TOKEN_LIFETIME_HOURS,
    decode_token,
    issue_access_token,
    issue_refresh_token,
)

from .serializers import (
    LoginSerializer,
    ProfileSerializer,
    PushTokenSerializer,
    RefreshSerializer,
)

User = get_user_model()


class LoginAPIView(APIView):
    """POST /api/v1/auth/login/  body: {login, password}"""
    permission_classes = [AllowAny]
    authentication_classes = []  # явно без auth: логин не должен требовать токена

    def post(self, request):
        ser = LoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        username = ser.validated_data['login']
        password = ser.validated_data['password']

        user = django_authenticate(request, username=username, password=password)
        if user is None:
            # Пробуем по email если ввели email вместо username
            try:
                u = User.objects.get(email__iexact=username)
                user = django_authenticate(request, username=u.username, password=password)
            except User.DoesNotExist:
                user = None

        if user is None or not user.is_active:
            try:
                from apps.shared.audit.services import record_event
                record_event(
                    action='login_failed', request=request,
                    actor_username=username or '', target='Вход в приложение',
                    meta={'via': 'mobile'},
                )
            except Exception:
                pass
            return Response(
                {'detail': 'Неверный логин или пароль'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            from apps.shared.audit.services import record_event
            record_event(
                action='login', request=request, actor=user,
                target='Вход в приложение', meta={'via': 'mobile'},
            )
        except Exception:
            pass

        access = issue_access_token(user)
        refresh = issue_refresh_token(user)
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            hours=ACCESS_TOKEN_LIFETIME_HOURS,
        )

        return Response({
            'token': access,
            'refresh': refresh,
            'expires_at': expires_at.isoformat(),
            'profile': ProfileSerializer(user).data,
        })


class MeAPIView(APIView):
    """
    GET   /api/v1/me/ (и /api/v1/auth/me/) — текущий профиль по Bearer-токену.
    PATCH /api/v1/me/ — редактирование своего профиля из мобильного приложения.

    Редактируемые поля: full_name (ФИО одной строкой → хранится в first_name),
    city, birthday. ДР можно установить только один раз: при первой установке
    проставляется birthday_set_at, после чего повторные изменения игнорируются
    (менять может только админ в Django-админке).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(ProfileSerializer(request.user).data)

    def patch(self, request):
        user = request.user
        data = request.data or {}

        if 'full_name' in data:
            full_name = (data.get('full_name') or '').strip()
            # ФИО хранится целиком в first_name, чтобы get_full_name() отдавал
            # строку обратно без перестановки порядка слов.
            user.first_name = full_name[:150]
            user.last_name = ''

        if 'city' in data:
            user.city = (data.get('city') or '').strip()[:80]

        if 'birthday' in data and not user.birthday_set_at:
            raw = (data.get('birthday') or '').strip()
            if raw:
                try:
                    bday = datetime.date.fromisoformat(raw)
                except ValueError:
                    return Response(
                        {'detail': 'birthday должен быть в формате YYYY-MM-DD'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                user.birthday = bday
                user.birthday_set_at = datetime.datetime.now(datetime.timezone.utc)

        user.save()
        return Response(ProfileSerializer(user).data)


class LogoutAPIView(APIView):
    """
    POST /api/v1/auth/logout/

    JWT — stateless, серверу нечего инвалидировать без blacklist-таблицы.
    Возвращаем 200 чтобы мобайл уверенно почистил локальный токен.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        return Response({'detail': 'logged out'}, status=status.HTTP_200_OK)


class DeleteAccountAPIView(APIView):
    """
    POST /api/v1/me/delete/  body: {password}

    Полное удаление собственного аккаунта владельца/менеджера из приложения
    (требование App Store, Guideline 5.1.1(v)). Удаляет учётную запись и связанные
    личные данные (профиль, push-токены — CASCADE). Данные тенанта (гости, точки,
    статистика) не трогаются — это отдельные сущности.

    Требует подтверждения паролем. Суперадмина/суперюзера через приложение
    удалить нельзя (платформенный аккаунт — только вручную в админке).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user

        if user.is_superuser or getattr(user, 'role', None) == 'superadmin':
            return Response(
                {'detail': 'Аккаунт суперадмина нельзя удалить из приложения.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        password = (request.data or {}).get('password') or ''
        if not user.check_password(password):
            return Response(
                {'detail': 'Неверный пароль. Подтвердите удаление текущим паролем.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Фиксируем удаление в журнале ДО удаления (actor станет NULL, ник останется).
        try:
            from apps.shared.audit.services import record_event
            record_event(
                action='delete', request=request, actor=user,
                target='Удаление аккаунта',
                meta={'user_id': user.pk, 'username': user.username, 'via': 'mobile'},
            )
        except Exception:
            pass

        user.delete()  # CASCADE снимет push-токены; M2M companies очистится
        return Response({'detail': 'Аккаунт удалён.'}, status=status.HTTP_200_OK)


class RefreshAPIView(APIView):
    """POST /api/v1/auth/refresh/  body: {refresh}"""
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        import jwt as pyjwt

        ser = RefreshSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            payload = decode_token(ser.validated_data['refresh'])
        except pyjwt.ExpiredSignatureError:
            return Response({'detail': 'Refresh token expired'}, status=401)
        except pyjwt.InvalidTokenError as e:
            return Response({'detail': f'Invalid refresh: {e}'}, status=401)

        if payload.get('typ') != 'refresh':
            return Response({'detail': 'Wrong token type'}, status=401)

        try:
            user = User.objects.get(pk=payload['sub'])
        except User.DoesNotExist:
            return Response({'detail': 'User no longer exists'}, status=401)
        if not user.is_active:
            return Response({'detail': 'User is inactive'}, status=401)

        access = issue_access_token(user)
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            hours=ACCESS_TOKEN_LIFETIME_HOURS,
        )
        return Response({
            'token': access,
            'expires_at': expires_at.isoformat(),
        })


class PushRegisterAPIView(APIView):
    """
    POST /api/v1/push/register/  body: {token, platform}
    DELETE /api/v1/push/register/ body: {token}

    Сохраняем Expo/APNs/FCM push-токен в `users.PushToken`. Один user может
    иметь несколько токенов (несколько устройств). При повторной регистрации
    того же токена — обновляем `last_seen_at` (auto_now=True) и перепривязываем
    к текущему user'у на случай переустановки приложения другим админом.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.shared.users.models import PushToken

        ser = PushTokenSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        token, _ = PushToken.objects.update_or_create(
            token=ser.validated_data['token'],
            defaults={
                'user': request.user,
                'platform': ser.validated_data['platform'],
            },
        )
        return Response({'ok': True, 'id': token.pk})

    def delete(self, request):
        from apps.shared.users.models import PushToken

        ser = PushTokenSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        PushToken.objects.filter(
            user=request.user,
            token=ser.validated_data['token'],
        ).delete()
        return Response({'ok': True})


class NotificationListAPIView(APIView):
    """
    GET /api/v1/notifications/?limit=50 — история уведомлений текущего пользователя.
    POST /api/v1/notifications/ {ids?: [int], all?: true} — отметить прочитанными.

    Notification лежит в public schema, поэтому работает из любого тенант-домена.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django_tenants.utils import schema_context
        from apps.shared.users.models import Notification

        try:
            limit = min(int(request.query_params.get('limit', 50)), 200)
        except (TypeError, ValueError):
            limit = 50

        with schema_context('public'):
            qs = Notification.objects.filter(user=request.user).order_by('-created_at')[:limit]
            items = [{
                'id':          n.pk,
                'type':        n.type,
                'title':       n.title,
                'body':        n.body,
                'data':        n.data or {},
                'read':        n.read_at is not None,
                'created_at':  n.created_at.isoformat(),
            } for n in qs]
            unread = Notification.objects.filter(user=request.user, read_at__isnull=True).count()

        return Response({'notifications': items, 'unread': unread})

    def post(self, request):
        from django_tenants.utils import schema_context
        from django.utils import timezone
        from apps.shared.users.models import Notification

        ids = request.data.get('ids')
        mark_all = request.data.get('all', False)

        with schema_context('public'):
            qs = Notification.objects.filter(user=request.user, read_at__isnull=True)
            if not mark_all and ids:
                qs = qs.filter(pk__in=ids)
            qs.update(read_at=timezone.now())

        return Response({'ok': True})


class PushPrefsAPIView(APIView):
    """
    GET /api/v1/me/push-prefs/  — текущие настройки + доступные тенанты/типы для UI.
    PATCH /api/v1/me/push-prefs/  body: {tenants?: {...}, types?: {...}}

    Формат push_prefs (хранится в User.push_prefs JSONField):
      {
        "tenants": {"asap_orel": true, "shavuha_ot_leo": false, "*": true},
        "types":   {"review_new": true, "draft_ready": false}
      }

    Дефолт (пустой prefs) = все пуши включены.
    """
    permission_classes = [IsAuthenticated]

    def _available_tenants(self, user) -> list[dict]:
        """SU видит ВСЕ тенанты; обычный юзер — только те, что в companies."""
        from django_tenants.utils import schema_context
        with schema_context('public'):
            from apps.shared.clients.models import Company
            if user.is_superuser:
                qs = Company.objects.exclude(schema_name='public').order_by('name')
            else:
                qs = user.companies.exclude(schema_name='public').order_by('name')
            return [{'schema_name': c.schema_name, 'name': c.name} for c in qs]

    def _available_types(self) -> list[dict]:
        from apps.shared.users.push import PUSH_TYPES
        return [{'code': code, 'label': label, 'description': desc} for code, label, desc in PUSH_TYPES]

    def get(self, request):
        prefs = request.user.push_prefs or {}
        return Response({
            'tenants':           prefs.get('tenants') or {},
            'types':             prefs.get('types') or {},
            'available_tenants': self._available_tenants(request.user),
            'available_types':   self._available_types(),
        })

    def patch(self, request):
        user = request.user
        data = request.data or {}
        prefs = dict(user.push_prefs or {})

        if 'tenants' in data:
            t = data.get('tenants') or {}
            if not isinstance(t, dict):
                return Response({'detail': 'tenants must be an object'}, status=status.HTTP_400_BAD_REQUEST)
            # Нормализуем значения в bool; пустые ключи отбрасываем
            prefs['tenants'] = {
                str(k): bool(v) for k, v in t.items() if isinstance(k, str) and k
            }

        if 'types' in data:
            t = data.get('types') or {}
            if not isinstance(t, dict):
                return Response({'detail': 'types must be an object'}, status=status.HTTP_400_BAD_REQUEST)
            from apps.shared.users.push import PUSH_TYPE_CODES
            prefs['types'] = {
                str(k): bool(v) for k, v in t.items()
                if isinstance(k, str) and k in PUSH_TYPE_CODES
            }

        user.push_prefs = prefs
        user.save(update_fields=['push_prefs'])
        return self.get(request)
