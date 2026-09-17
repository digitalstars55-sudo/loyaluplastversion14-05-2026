"""
Телефон гостя с согласия через ВКонтакте — карта переезда, №78.

    POST   /api/v1/client/phone/   {vk_id, phone_number, sign}   → сохранить
    DELETE /api/v1/client/phone/   {vk_id}                       → отозвать согласие

Откуда данные: мини-апп вызывает bridge `VKWebAppGetPhoneNumber`, ВКонтакте
показывает гостю окно согласия и отдаёт `{phone_number, sign}`; мини-апп шлёт
их сюда как есть (телефон НЕ нормализовать на клиенте — подпись покрывает
строку, которую вернул ВК). Заголовок `X-VK-Launch-Params` axios ставит сам,
поэтому ручка живёт под префиксом `/api/v1/client/` и проходит
`VKLaunchParamsMiddleware`: если заголовок есть и подпись запуска сошлась,
в `request.vk_user_id` лежит ДОКАЗАННЫЙ id гостя. Это первая ручка в проекте,
которая его читает.

Кто кому что доказывает:
- подпись телефона (`vk_phone.check_phone_sign`) доказывает, что пару
  (vk_id, phone_number) выдал ВКонтакте — подделать без защищённого ключа нельзя;
- подпись запуска доказывает, что запрос прислал именно этот гость.

Решение по режимам (`GUEST_PHONE_SIGN_ENFORCE`):
- подпись телефона сошлась → сохраняем как `phone_source='vk'` (доказано ВК,
  заголовок запуска не обязателен);
- не сошлась, но гость доказан заголовком и enforce=off → сохраняем как
  `'vk_unverified'` (режим наблюдения: формула подписи ВК в документации описана
  неоднозначно, смотрим в логе `guest phone: ... sign=` какой вариант сходится);
- не сошлась и гость НЕ доказан → 403 всегда: иначе любой мог бы вписать
  чужому гостю мусорный номер;
- enforce=on → сохраняем только доказанное с обеих сторон.

Флаг `GUEST_PHONE_ENABLED` выключен → 404 на всё, прод неотличим от эталона.
Формат ошибок — `{code, detail}`, как у остальных новых ручек контракта.
"""

import logging
import re

from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.guest.models import Client
from apps.shared.guest.vk_phone import (
    check_phone_sign,
    guest_phone_enabled,
    normalize_phone,
    phone_sign_enforce,
)

log = logging.getLogger(__name__)

SOURCE_VK = 'vk'
SOURCE_VK_UNVERIFIED = 'vk_unverified'


class ClientPhoneSerializer(serializers.Serializer):
    vk_id = serializers.IntegerField(min_value=1)
    phone_number = serializers.CharField(max_length=32)
    sign = serializers.CharField(max_length=256)
    # Где гость дал номер ('profile' | 'review' | …) — чтобы сравнивать места.
    placement = serializers.CharField(max_length=32, required=False, allow_blank=True, default='')
    # Публичный id точки, где гость сейчас: на её счёт падают баллы за номер
    # (баллы пер-точечные). Без него награды нет, номер всё равно сохраняется.
    branch_id = serializers.IntegerField(required=False, allow_null=True, default=None)


_PLACEMENT_RE = re.compile(r'^[a-z0-9_-]{1,32}$')


def _clean_placement(raw) -> str:
    value = str(raw or '').strip().lower()
    return value if _PLACEMENT_RE.match(value) else ''


class ClientPhoneRevokeSerializer(serializers.Serializer):
    vk_id = serializers.IntegerField(min_value=1)


def _err(code: str, detail: str, http_status: int) -> Response:
    return Response({'code': code, 'detail': detail}, status=http_status)


def _grant_phone_reward(client, branch_id) -> int:
    """
    Баллы за первый номер (№78): `ClientConfig.guest_phone_reward_coins` сети,
    один раз на гостя (`Client.phone_reward_at`), на счёт точки `branch_id`
    (баллы пер-точечные). Без branch_id, без профиля в точке или при нуле в
    настройках — 0. При отзыве согласия баллы не отбираются.
    """
    if client.phone_reward_at or not branch_id:
        return 0
    from django.db import connection
    from apps.tenant.branch.models import ClientBranch, CoinTransaction, TransactionSource, TransactionType

    tenant = getattr(connection, 'tenant', None)
    cfg = getattr(tenant, 'config', None) if tenant is not None else None
    amount = int(getattr(cfg, 'guest_phone_reward_coins', 0) or 0)
    if amount <= 0:
        return 0
    cb = ClientBranch.objects.filter(client=client, branch__branch_id=branch_id).first()
    if cb is None:
        return 0
    CoinTransaction.objects.create_transfer(
        cb, amount, TransactionType.INCOME, TransactionSource.PHONE,
        description='Спасибо за номер телефона',
    )
    client.phone_reward_at = timezone.now()
    client.save(update_fields=['phone_reward_at', 'updated_at'])
    return amount


def _proven_vk_id(request) -> int | None:
    """Доказанный заголовком запуска id гостя или None (заголовка нет / не сошёлся)."""
    value = getattr(request, 'vk_user_id', None)
    try:
        value = int(value) if value else None
    except (TypeError, ValueError):
        return None
    return value if value and value > 0 else None


class ClientPhoneView(APIView):
    """Сохранить / отозвать телефон гостя, полученный через `VKWebAppGetPhoneNumber`."""

    @extend_schema(
        request=ClientPhoneSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT,
                   403: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
        description='Телефон гостя с согласия через ВК (№78). Тело — ответ bridge VKWebAppGetPhoneNumber как есть.',
    )
    def post(self, request: Request) -> Response:
        if not guest_phone_enabled():
            return _err('feature_disabled', 'Не найдено.', status.HTTP_404_NOT_FOUND)

        s = ClientPhoneSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        claimed = int(s.validated_data['vk_id'])
        raw_phone = s.validated_data['phone_number'].strip()
        sign = s.validated_data['sign'].strip()
        placement = _clean_placement(s.validated_data.get('placement'))

        proven = _proven_vk_id(request)
        if proven and proven != claimed:
            log.warning('guest phone: vk_id=%s не совпадает с доказанным %s', claimed, proven)
            return _err('vk_id_mismatch', 'vk_id не совпадает с параметрами запуска.', status.HTTP_403_FORBIDDEN)

        sign_status = check_phone_sign(claimed, raw_phone, sign)
        verified = sign_status.startswith('ok')
        enforce = phone_sign_enforce()
        # Строка наблюдения (логгер выведен на INFO в settings.LOGGING): по ней видно,
        # какая кодировка подписи у ВК, доказан ли гость заголовком и где он дал номер.
        log.info('guest phone: vk_id=%s proven=%s sign=%s enforce=%s placement=%s',
                 claimed, bool(proven), sign_status, 'on' if enforce else 'off', placement or '-')

        if not verified and (enforce or not proven):
            return _err('phone_sign_invalid', 'Подпись телефона не подтверждена.', status.HTTP_403_FORBIDDEN)
        if enforce and not proven:
            return _err('vk_sign_required', 'Нужны параметры запуска мини-приложения.', status.HTTP_403_FORBIDDEN)

        phone = normalize_phone(raw_phone)
        if not phone:
            return _err('phone_invalid', 'Не похоже на номер телефона.', status.HTTP_400_BAD_REQUEST)

        client = Client.objects.filter(vk_id=claimed).first()
        if client is None:
            return _err('client_not_found', 'Профиль гостя не найден.', status.HTTP_404_NOT_FOUND)

        now = timezone.now()
        client.phone = phone
        client.phone_source = SOURCE_VK if verified else SOURCE_VK_UNVERIFIED
        client.phone_consent_at = now
        client.phone_placement = placement
        client.save(update_fields=['phone', 'phone_source', 'phone_consent_at', 'phone_placement', 'updated_at'])

        # Награда — отдельно от сохранения номера: её сбой номер не теряет.
        reward = 0
        try:
            reward = _grant_phone_reward(client, s.validated_data.get('branch_id'))
        except Exception as e:  # noqa: BLE001 — любой сбой начисления только в лог
            log.warning('guest phone: награда vk_id=%s branch_id=%s не начислена: %s',
                        claimed, s.validated_data.get('branch_id'), e)
        if reward:
            log.info('guest phone: vk_id=%s награда %s баллов, placement=%s', claimed, reward, placement or '-')

        return Response({
            'phone': phone,
            'verified': verified,
            'proven': bool(proven),
            'consent_at': now.isoformat(),
            'placement': placement,
            'reward_coins': reward,
        })

    @extend_schema(
        request=ClientPhoneRevokeSerializer,
        responses={200: OpenApiTypes.OBJECT, 403: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
        description='Отзыв согласия: гость убирает свой номер.',
    )
    def delete(self, request: Request) -> Response:
        if not guest_phone_enabled():
            return _err('feature_disabled', 'Не найдено.', status.HTTP_404_NOT_FOUND)

        s = ClientPhoneRevokeSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        claimed = int(s.validated_data['vk_id'])

        proven = _proven_vk_id(request)
        if proven and proven != claimed:
            return _err('vk_id_mismatch', 'vk_id не совпадает с параметрами запуска.', status.HTTP_403_FORBIDDEN)
        # Убрать номер можно только доказанному гостю: без заголовка запуска
        # любой мог бы стереть чужой телефон, зная vk_id.
        if not proven:
            return _err('vk_sign_required', 'Нужны параметры запуска мини-приложения.', status.HTTP_403_FORBIDDEN)

        client = Client.objects.filter(vk_id=claimed).first()
        if client is None:
            return _err('client_not_found', 'Профиль гостя не найден.', status.HTTP_404_NOT_FOUND)

        if client.phone or client.phone_source or client.phone_consent_at or client.phone_placement:
            client.phone = ''
            client.phone_source = ''
            client.phone_consent_at = None
            client.phone_placement = ''
            client.save(update_fields=['phone', 'phone_source', 'phone_consent_at', 'phone_placement', 'updated_at'])
        log.info('guest phone: vk_id=%s согласие отозвано', claimed)
        return Response({'phone': '', 'revoked': True})
