"""
Флаги механик сети для кабинета CheckUp — контракт 3б.7 (№56) и v1.8 (запись).

Чтение — по БЕЛОМУ списку полей `ClientConfig`, для обеих ролей. Запись (v1.8,
по слову владельца 19.09) — `PATCH` частичным телом `{флаг: значение}` только
для `network_admin`/суперадмина и только по списку EDITABLE: платформенные
переключатели пилотов и каталога ВК (`web_entry_enabled`, `degrade_enabled`,
`vk_catalog_*`, `vk_review_branch_inference*`, `guest_phone_enabled`) остаются
только чтением — их включает оператор платформы.

Переопределение точки (`branch_id` в теле) — только у флагов, у которых есть
пара в `BranchConfig` (пять полей сториз + окно дня рождения); `null` =
наследовать сеть. Семантика нуля у точки РАЗНАЯ: у сториз `0`/`""` = «как в
сети», у `birthday_window_days` `0` = «только день в день» (контракт 3б.3).

Чего здесь нет и не будет: `pos_type`, идентификаторы и секреты интеграций
(iiko, Dooglys), токены ВК, любые ключи. Их не должно быть даже в ответе
только-чтение: кабинет CheckUp — другой периметр.

`source: 'network'` — значение отличается от дефолта поля модели, то есть его
кто-то осознанно поставил; `'default'` — совпадает с дефолтом; у точки —
`'branch'`, когда переопределение задано.
"""
from __future__ import annotations

import logging

from django.db import connection
from rest_framework import status as http_status
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.config.models import ClientConfig
from apps.shared.users.access import current_schema_name, effective_branch_ids
from apps.tenant.branch.models import Branch, BranchConfig

log = logging.getLogger(__name__)

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

# Только чтение: платформенные переключатели пилотов и каталога ВК — включает
# оператор платформы, не кабинет сети.
READONLY_FLAGS = frozenset({
    'vk_catalog_enabled', 'vk_catalog_city', 'vk_review_branch_inference',
    'vk_review_branch_inference_hours', 'web_entry_enabled', 'degrade_enabled',
    'guest_phone_enabled',
})
EDITABLE_FLAGS = tuple(f for f in FLAG_FIELDS if f not in READONLY_FLAGS)
# Пара в BranchConfig есть только у этих флагов (переопределение точки).
BRANCH_OVERRIDABLE = ('story_game_enabled', 'story_min_order_amount', 'story_cafe_address',
                      'story_activation_text', 'story_saved_text', 'birthday_window_days')


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
    GET /api/v1/settings/features/ — что включено у сети (читают обе роли).
    PATCH — запись по списку EDITABLE (network_admin), см. FeatureFlagsPatchMixin.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiResponse(_OUT, description='флаги механик сети')},
                   tags=['v1'])
    def get(self, request):
        company = getattr(connection, 'tenant', None)
        cfg = ClientConfig.objects.filter(company=company).first() if company else None
        return Response({'flags': flags_payload(cfg), 'read_only': False,
                         'editable': list(EDITABLE_FLAGS), 'readonly': sorted(READONLY_FLAGS),
                         'branch_overridable': list(BRANCH_OVERRIDABLE)})


# ── запись (v1.8) ─────────────────────────────────────────────────────────────

class PayloadError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def _error(code: str, detail: str, status_code: int, **extra):
    payload = {'code': code, 'detail': detail}
    payload.update(extra)
    return Response(payload, status=status_code)


def _may_write(user) -> bool:
    return bool(getattr(user, 'is_superuser', False)
                or getattr(user, 'role', '') == 'network_admin'
                or getattr(user, 'is_network_admin', False) is True)


def _parse_flag_value(name: str, raw, *, model):
    """Значение по типу поля модели (`model` = ClientConfig или BranchConfig)."""
    from django.db import models as dm
    field = model._meta.get_field(name)
    if raw is None:
        if getattr(field, 'null', False):
            return None
        if isinstance(field, (dm.CharField, dm.TextField)):
            return ''
        raise PayloadError('invalid_payload', f'{name}: null здесь недопустим')
    if isinstance(field, dm.BooleanField):
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ('true', '1', 'yes'):
            return True
        if text in ('false', '0', 'no'):
            return False
        raise PayloadError('invalid_payload', f'{name}: true или false')
    if isinstance(field, dm.DateField):
        from django.utils.dateparse import parse_date
        if raw == '':
            return None
        value = parse_date(str(raw))
        if value is None:
            raise PayloadError('invalid_payload', f'{name}: дата ГГГГ-ММ-ДД')
        return value
    if isinstance(field, (dm.IntegerField, dm.PositiveSmallIntegerField, dm.PositiveIntegerField)):
        if isinstance(raw, bool):
            raise PayloadError('invalid_payload', f'{name}: целое число')
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise PayloadError('invalid_payload', f'{name}: целое число')
        if value < 0:
            raise PayloadError('invalid_payload', f'{name}: не может быть отрицательным')
        return value
    text = str(raw)
    max_len = getattr(field, 'max_length', None)
    if max_len and len(text) > max_len:
        raise PayloadError('invalid_payload', f'{name}: не длиннее {max_len} символов')
    return text


def _collect(data: dict, *, branch: bool) -> dict:
    keys = [k for k in data.keys() if k != 'branch_id']
    if not keys:
        raise PayloadError('invalid_payload', 'нечего менять: передайте хотя бы один флаг')
    values = {}
    for name in keys:
        if name not in FLAG_FIELDS:
            raise PayloadError('unknown_flag', f'{name}: такого флага нет (см. GET /settings/features/)')
        if name in READONLY_FLAGS:
            raise PayloadError('readonly_flag', f'{name}: только чтение — включает оператор платформы')
        if branch and name not in BRANCH_OVERRIDABLE:
            raise PayloadError('no_branch_override', f'{name}: у точки этого переопределения нет, флаг сетевой')
        values[name] = _parse_flag_value(name, data[name], model=BranchConfig if branch else ClientConfig)
    return values


def _branch_or_none(pk, request):
    allowed = effective_branch_ids(request.user, current_schema_name(), None)
    qs = Branch.objects.filter(pk=pk)
    if allowed is not None:
        qs = qs.filter(pk__in=allowed)
    return qs.first()


def branch_flags_payload(branch, cfg) -> dict:
    """Шесть переопределяемых флагов: effective + source branch|network|default."""
    branch_cfg = getattr(branch, 'config', None)
    out = {}
    net_defaults = {name: _field_default(name) for name in BRANCH_OVERRIDABLE}
    for name in BRANCH_OVERRIDABLE:
        override = getattr(branch_cfg, name, None) if branch_cfg is not None else None
        net_value = getattr(cfg, name, None) if cfg is not None else net_defaults[name]
        # Сториз: пусто/0 у точки = как в сети; окно ДР: None = как в сети, 0 — настоящий 0.
        if name == 'birthday_window_days':
            is_set = override is not None
        elif name in ('story_game_enabled',):
            is_set = override is not None
        else:
            is_set = bool(override)
        if is_set:
            out[name] = {'value': _plain(override), 'source': 'branch'}
        else:
            source = 'default' if (cfg is None or net_value == net_defaults[name]) else 'network'
            out[name] = {'value': _plain(net_value), 'source': source}
    return out


_PATCH_IN = inline_serializer(name='FeatureFlagsPatch', fields={
    'branch_id': drf_serializers.IntegerField(required=False, help_text='внутренний id точки — переопределение (только BRANCH_OVERRIDABLE)'),
})
_ERR = inline_serializer(name='FeatureFlagsError', fields={
    'code': drf_serializers.CharField(help_text='unknown_flag | readonly_flag | no_branch_override | invalid_payload | role_not_allowed | not_found'),
    'detail': drf_serializers.CharField(),
})


class FeatureFlagsPatchMixin:
    @extend_schema(request=_PATCH_IN, responses={200: _OUT, 400: _ERR, 403: _ERR, 404: _ERR}, tags=['v1'],
                   summary='Изменить флаги механик сети (частично); с branch_id — переопределение точки')
    def patch(self, request):
        if not _may_write(request.user):
            return _error('role_not_allowed', 'менять флаги может только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        data = request.data or {}
        if not isinstance(data, dict):
            return _error('invalid_payload', 'тело должно быть JSON-объектом', http_status.HTTP_400_BAD_REQUEST)
        branch_id = data.get('branch_id')
        company = getattr(connection, 'tenant', None)
        try:
            values = _collect(data, branch=branch_id is not None)
        except PayloadError as exc:
            return _error(exc.code, exc.detail, http_status.HTTP_400_BAD_REQUEST,
                          editable=list(EDITABLE_FLAGS), branch_overridable=list(BRANCH_OVERRIDABLE))
        if branch_id is not None:
            try:
                branch = _branch_or_none(int(branch_id), request)
            except (TypeError, ValueError):
                branch = None
            if branch is None:
                return _error('not_found', 'Точка не найдена', http_status.HTTP_404_NOT_FOUND)
            branch_cfg, created = BranchConfig.objects.get_or_create(branch=branch)
            for name, value in values.items():
                setattr(branch_cfg, name, value)
            branch_cfg.save(update_fields=None if created else sorted(values))
            branch.config = branch_cfg
            cfg = ClientConfig.objects.filter(company=company).first() if company else None
            log.info('feature flags patched: %s branch=%s fields=%s by=%s',
                     current_schema_name(), branch.pk, sorted(values), request.user)
            return Response({'branch': {'id': branch.pk, 'branch_id': branch.branch_id, 'name': branch.name},
                             'flags': branch_flags_payload(branch, cfg),
                             'read_only': False, 'branch_overridable': list(BRANCH_OVERRIDABLE)})
        cfg, _ = ClientConfig.objects.get_or_create(company=company)
        for name, value in values.items():
            setattr(cfg, name, value)
        cfg.save(update_fields=sorted(values))
        log.info('feature flags patched: %s network fields=%s by=%s',
                 current_schema_name(), sorted(values), request.user)
        return Response({'flags': flags_payload(cfg), 'read_only': False,
                         'editable': list(EDITABLE_FLAGS), 'readonly': sorted(READONLY_FLAGS),
                         'branch_overridable': list(BRANCH_OVERRIDABLE)})


# PATCH подмешивается к той же вьюхе, что и GET (один путь /settings/features/).
FeatureFlagsAPIView.patch = FeatureFlagsPatchMixin.patch
