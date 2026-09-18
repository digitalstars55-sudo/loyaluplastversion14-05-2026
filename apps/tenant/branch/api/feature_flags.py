"""
Флаги механик сети для кабинета CheckUp — контракт 3б.7 (№56), ТОЛЬКО ЧТЕНИЕ.

Ответ «что включено у этого клиента» собирался глазами из четырёх моделей в
двух схемах, и CheckUp не мог показать владельцу, какие механики у него живут.
Здесь один список по БЕЛОМУ списку полей `ClientConfig`.

Почему только чтение. Включение механики меняет поведение мини-аппа сразу у
всех гостей сети (игра, колесо, подарки, телефон гостя), а некоторые флаги
стоят денег — `guest_phone_reward_coins` начисляет баллы за номер. Запись
появится отдельным решением владельца; пока кабинет показывает состояние, а
переключают его в LoyalUP.

Чего здесь нет и не будет: `pos_type`, идентификаторы и секреты интеграций
(iiko, Dooglys), токены ВК, любые ключи. Их не должно быть даже в ответе
только-чтение: кабинет CheckUp — другой периметр.

`source: 'network'` — значение отличается от дефолта поля модели, то есть его
кто-то осознанно поставил; `'default'` — совпадает с дефолтом (никто не
настраивал). Ровно это отличие владелец и спрашивает, глядя на список.
"""
from __future__ import annotations

from django.db import connection
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.config.models import ClientConfig

# Белый список из контракта 3б.7. Ничего, кроме этих ключей, ручка не отдаёт —
# добавление поля сюда должно быть осознанным решением, а не побочным эффектом
# появления новой колонки в ClientConfig.
FLAG_FIELDS = (
    'story_game_enabled', 'story_min_order_amount', 'story_activation_minutes',
    'story_require_cafe_visit', 'story_cafe_address', 'story_activation_text',
    'story_saved_text', 'story_gift_lifetime_days', 'story_gift_reminder_days',
    'story_campaign_start', 'story_campaign_end',
    'birthday_window_days', 'auto_broadcast_weekly_cap', 'rf_orchestrator_enabled',
    'vk_catalog_enabled', 'vk_catalog_city',
    'vk_review_branch_inference', 'vk_review_branch_inference_hours',
    'web_entry_enabled', 'degrade_enabled',
    'guest_phone_enabled', 'guest_phone_reward_coins',
    'code_prompt_message', 'quest_show_message',
    'brand_color', 'brand_color_secondary',
)


def _field_default(name):
    """Дефолт поля модели (NOT_PROVIDED → None)."""
    from django.db.models.fields import NOT_PROVIDED
    try:
        field = ClientConfig._meta.get_field(name)
    except Exception:
        return None
    default = getattr(field, 'default', NOT_PROVIDED)
    if default is NOT_PROVIDED:
        return None
    return default() if callable(default) else default


def _plain(value):
    """Дата/Decimal → строка, остальное как есть (ответ должен быть JSON-ready)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    isoformat = getattr(value, 'isoformat', None)
    return isoformat() if callable(isoformat) else str(value)


def flags_payload(cfg) -> dict:
    flags = {}
    for name in FLAG_FIELDS:
        if cfg is None:
            flags[name] = {'value': _plain(_field_default(name)), 'source': 'default'}
            continue
        value = getattr(cfg, name, None)
        source = 'default' if value == _field_default(name) else 'network'
        flags[name] = {'value': _plain(value), 'source': source}
    return flags


_OUT = inline_serializer(name='FeatureFlags', fields={
    'flags': drf_serializers.DictField(help_text='{ключ: {value, source: network|default}}'),
})


class FeatureFlagsAPIView(APIView):
    """
    GET /api/v1/settings/features/ — что включено у сети (только чтение).

    Читают обе роли: `client` видит состояние своей сети, но переключать
    механики через API нельзя никому (см. шапку модуля).
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiResponse(_OUT, description='флаги механик сети')},
                   tags=['v1'])
    def get(self, request):
        company = getattr(connection, 'tenant', None)
        cfg = ClientConfig.objects.filter(company=company).first() if company else None
        return Response({'flags': flags_payload(cfg), 'read_only': True})
