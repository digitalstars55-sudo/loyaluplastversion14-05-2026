"""
Настройки механики «Игра через сториз» для кабинета CheckUp — контракт 3б.3 (№23).

Настройки живут в двух местах: сеть (`ClientConfig.story_*`, 11 полей) и точка
(`BranchConfig.story_*`, 5 полей-переопределений). Резолв «точка → сеть →
дефолт» УЖЕ есть в коде гостя и вынесен в
`apps.tenant.inventory.api.story_services.resolve_story_settings_for_branch`.
Здесь второй копии правил нет намеренно: сотрудник в CheckUp обязан видеть то
же, что увидит гость, — иначе настройка «включено» будет расходиться с реальным
поведением мини-аппа. Модуль добавляет к общему резолву только то, чего у него
нет: `source` (откуда взялось значение) и превью текстов.

Правила наследования НЕ одинаковы у разных полей, и это часть контракта:
  • булевы (`story_game_enabled`, `story_require_cafe_visit`) — по «is not
    None»: `false` у точки ПЕРЕБИВАЕТ `true` сети (выключить игру на одной
    точке можно);
  • число и тексты — по пустоте: `0` и `""` значат «как в сети». Настоящий
    порог «0 ₽» в v1.5 задать нельзя — это правка резолва и миграция.
`source` поэтому считается ровно теми же правилами, а тест-матрица
`SourceAgreesWithResolverTest` сверяет их с общим резолвом: поедет правило
там — упадёт тест здесь, а не кабинет у клиента.

Значения `source`: `branch` (переопределено точкой) · `network` (значение сети)
· `branch_address` (только у `story_cafe_address`: пусто и там, и там, поэтому
подставлен адрес точки из её карточки) · `default` (хардкод в коде).

Права: писать может `network_admin` и суперадмин, `client` — только читать
(`403 role_not_allowed`). Точка вне доступа сотрудника — `404 not_found`.
Миграций модуль не требует.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from django.db import transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.config.models import ClientConfig
from apps.shared.users.access import current_schema_name, effective_branch_ids
from apps.tenant.branch.models import Branch, BranchConfig
from apps.tenant.inventory.api.story_services import (
    render_story_text,
    resolve_story_settings_for_branch,
    story_gifts_for_branch,
)

log = logging.getLogger(__name__)

PLACEHOLDERS = ['[адрес кафе]', '[сумма]', '[время]', '[название кафе]', '[название подарка]']

# Поле → (тип, правило наследования, переопределяется ли точкой, ключ в резолве).
# Правило и ключ — из resolve_story_settings_for_branch; менять их здесь в
# одиночку нельзя, иначе `source` начнёт врать (держит тест-матрица).
BOOL, INT, TEXT, DATE = 'bool', 'int', 'text', 'date'
NOT_NONE, TRUTHY = 'not_none', 'truthy'

FIELDS = (
    ('story_game_enabled',       BOOL, NOT_NONE, True,  'enabled'),
    ('story_min_order_amount',   INT,  TRUTHY,   True,  'min_order_amount'),
    ('story_activation_minutes', INT,  TRUTHY,   False, 'activation_minutes'),
    ('story_require_cafe_visit', BOOL, NOT_NONE, False, 'require_cafe_visit'),
    ('story_cafe_address',       TEXT, TRUTHY,   True,  'cafe_address'),
    ('story_activation_text',    TEXT, TRUTHY,   True,  'activation_text'),
    ('story_saved_text',         TEXT, TRUTHY,   True,  'saved_text'),
    ('story_gift_lifetime_days', INT,  NOT_NONE, False, 'gift_lifetime_days'),
    ('story_gift_reminder_days', INT,  NOT_NONE, False, 'gift_reminder_days'),
    ('story_campaign_start',     DATE, NOT_NONE, False, 'campaign_start'),
    ('story_campaign_end',       DATE, NOT_NONE, False, 'campaign_end'),
)
NETWORK_FIELDS = [f[0] for f in FIELDS]
OVERRIDE_FIELDS = [f[0] for f in FIELDS if f[3]]
KIND = {f[0]: f[1] for f in FIELDS}
RULE = {f[0]: f[2] for f in FIELDS}
RESOLVED_KEY = {f[0]: f[4] for f in FIELDS}


# ── общие помощники (форма как в senler/api/broadcasts.py) ────────────────────

def _atomic():
    """transaction.atomic() отдельной функцией — тесты на моках подменяют её пустым контекстом."""
    return transaction.atomic()


def _error(code: str, detail: str, status_code: int, **extra):
    payload = {'code': code, 'detail': detail}
    payload.update(extra)
    return Response(payload, status=status_code)


def _may_write(user) -> bool:
    """Писать настройки сети и точки может админ сети и суперадмин."""
    return bool(getattr(user, 'is_superuser', False)
                or getattr(user, 'is_superadmin', False)
                or getattr(user, 'is_network_admin', False))


def _iso(value):
    return value.isoformat() if value else None


def _network_config():
    """ClientConfig текущей сети (создаём, если её ещё нет)."""
    from django.db import connection
    company = getattr(connection, 'tenant', None)
    cfg, _ = ClientConfig.objects.get_or_create(company=company)
    return cfg


def _branch_or_none(pk, request):
    allowed = effective_branch_ids(request.user, current_schema_name(), None)
    qs = Branch.objects.select_related('config').filter(pk=pk)
    if allowed is not None:
        qs = qs.filter(pk__in=allowed)
    return qs.first()


# ── разбор значений ──────────────────────────────────────────────────────────

class PayloadError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _parse_value(field: str, raw, *, inheritable: bool):
    """
    Значение поля из тела запроса.

    `inheritable=True` (переопределение точки): `null`, `""` и `0` означают
    «как в сети» и превращаются в «не задано» — `None` для числа и булева,
    `''` для текста (в БД у текстов нет NULL).
    """
    kind = KIND[field]
    if inheritable and (raw is None or raw == '' or (kind == INT and raw == 0)):
        return '' if kind == TEXT else None

    if kind == BOOL:
        if isinstance(raw, bool):
            return raw
        if str(raw).strip().lower() in ('true', '1', 'yes'):
            return True
        if str(raw).strip().lower() in ('false', '0', 'no'):
            return False
        raise PayloadError(f'{field}: true или false')
    if kind == INT:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise PayloadError(f'{field}: целое число')
        if value < 0:
            raise PayloadError(f'{field}: не может быть отрицательным')
        return value
    if kind == DATE:
        if raw in (None, ''):
            return None
        from django.utils.dateparse import parse_date
        value = parse_date(str(raw))
        if value is None:
            raise PayloadError(f'{field}: дата в формате ГГГГ-ММ-ДД')
        return value
    text = str(raw)
    if field == 'story_cafe_address' and len(text) > 255:
        raise PayloadError('story_cafe_address: не длиннее 255 символов')
    return text


def _collect(data, fields: list[str], *, inheritable: bool) -> dict:
    """Белый список: чужие ключи — ошибка, а не тихое молчание."""
    unknown = [k for k in data.keys() if k not in fields]
    if unknown:
        raise PayloadError('нельзя менять здесь: ' + ', '.join(sorted(unknown)))
    values = {}
    for field in fields:
        if field in data:
            values[field] = _parse_value(field, data[field], inheritable=inheritable)
    if not values:
        raise PayloadError('нечего менять: передайте хотя бы одно поле')
    return values


def _check_campaign_window(cfg, values: dict):
    start = values.get('story_campaign_start', getattr(cfg, 'story_campaign_start', None))
    end = values.get('story_campaign_end', getattr(cfg, 'story_campaign_end', None))
    if start and end and start > end:
        raise PayloadError('story_campaign_start не может быть позже story_campaign_end')


# ── источник значения ────────────────────────────────────────────────────────

def _source_for(field: str, branch_cfg, net_cfg, branch_address: str) -> str:
    """
    Откуда взялось действующее значение. Правила — те же, что в резолве.

    `branch_address` — адрес точки из её карточки: у `story_cafe_address` он
    служит дефолтом раньше хардкода (resolve_story_settings_for_branch).
    """
    overridable = field in OVERRIDE_FIELDS
    b = getattr(branch_cfg, field, None) if (branch_cfg is not None and overridable) else None
    n = getattr(net_cfg, field, None) if net_cfg is not None else None

    if RULE[field] == TRUTHY:
        if b:
            return 'branch'
        if n:
            return 'network'
        if field == 'story_cafe_address' and branch_address:
            return 'branch_address'
        return 'default'
    if b is not None:
        return 'branch'
    if n is not None:
        return 'network'
    return 'default'


def _effective(branch) -> dict:
    """Действующие значения — общим резолвом, под именами полей контракта."""
    resolved = resolve_story_settings_for_branch(branch)
    out = {}
    for field in NETWORK_FIELDS:
        value = resolved.get(RESOLVED_KEY[field])
        out[field] = _iso(value) if KIND[field] == DATE else value
    return out


def _network_settings_dict(cfg) -> dict:
    out = {}
    for field in NETWORK_FIELDS:
        value = getattr(cfg, field, None)
        out[field] = _iso(value) if KIND[field] == DATE else value
    return out


def _overrides_dict(branch_cfg) -> dict:
    """Переопределения точки: «не задано» отдаём как `null`, не как `''`/`0`."""
    out = {}
    for field in OVERRIDE_FIELDS:
        value = getattr(branch_cfg, field, None) if branch_cfg is not None else None
        out[field] = None if value in ('', 0) else value
    return out


def _network_domain() -> str:
    """Primary-домен сети, например `levone.levelupapp.ru`.

    Из НЕГО собирается ссылка на картинку, а не из запроса: CheckUp ходит к нам
    через loopback по http с подменённым `Host`, поэтому
    `request.build_absolute_uri` дал бы `http://127.0.0.1:7000/media/...` —
    такую ссылку кабинет не покажет никому (★5 ревью CheckUp).
    """
    from django.db import connection
    from apps.shared.clients.models import Domain
    company = getattr(connection, 'tenant', None)
    domain = (Domain.objects.filter(tenant=company, is_primary=True).first()
              or Domain.objects.filter(tenant=company).first())
    return domain.domain if domain else ''


POOL_LIMIT = 50


def _prizes(branch):
    """Призы точки: полный размер пула, первые POOL_LIMIT штук и картинка сториса.

    Порядок — тот же, что видит гость (`story_gifts_for_branch`), поэтому
    `pool[0]` и есть подарок, которым подставлен `rendered`.

    ⚠️ `story_image` живёт на самой `Branch` (models.py:63), а НЕ на
    `BranchConfig`: у гостя эта же картинка берётся как
    `_image_url(branch.story_image)` (branch/api/services.py:632).
    """
    qs = story_gifts_for_branch(branch)
    pool = [{'id': p.pk, 'name': p.name, 'emoji': getattr(p, 'emoji', '') or '',
             'price': getattr(p, 'price', 0)} for p in qs[:POOL_LIMIT]]
    first = None
    if pool:
        first = SimpleNamespace(pk=pool[0]['id'], name=pool[0]['name'])
    image = getattr(branch, 'story_image', None)
    url = None
    if image and getattr(image, 'name', ''):
        try:
            domain = _network_domain()
            # Гостю мини-апп отдаёт относительный путь и склеивает его сам;
            # кабинету CheckUp нужен полный адрес — тот же файл, просто
            # с доменом сети (★5 ревью).
            url = f'https://{domain}{image.url}' if domain else image.url
        except Exception:  # картинка не повод отдать 500
            url = None
    return {
        'count': qs.count(),
        'pool': pool,
        'first': ({'id': pool[0]['id'], 'name': pool[0]['name']} if pool else None),
        'story_image_url': url,
    }, first


def _rendered(branch, effective: dict, first_gift) -> dict:
    """
    Текст, который увидит гость.

    Имя подарка берём у первого приза пула (тот же порядок, что у гостя). Пула
    нет — оставляем подстановку `[название подарка]` в тексте как есть: пусть
    сотрудник видит, что подставлять нечего (рядом `prizes.count = 0`).
    """
    settings_for_text = {
        'cafe_address': effective['story_cafe_address'],
        'min_order_amount': effective['story_min_order_amount'],
        'activation_minutes': effective['story_activation_minutes'],
    }
    gift_name = first_gift.name if first_gift is not None else '[название подарка]'
    return {
        'activation_text': render_story_text(
            effective['story_activation_text'], cafe_name=branch.name,
            settings=settings_for_text, gift_name=gift_name),
        'saved_text': render_story_text(
            effective['story_saved_text'], cafe_name=branch.name,
            settings=settings_for_text, gift_name=gift_name),
    }


# ── OpenAPI (только описание, на поведение не влияет) ────────────────────────

_SETTINGS_OUT = inline_serializer(name='StoryNetworkSettings', fields={
    'settings': drf_serializers.DictField(help_text='11 полей ClientConfig.story_*'),
    'placeholders': drf_serializers.ListField(child=drf_serializers.CharField()),
    'branch_override_fields': drf_serializers.ListField(child=drf_serializers.CharField()),
    'prizes': drf_serializers.DictField(help_text='{network_count}'),
})
_BRANCH_OUT = inline_serializer(name='StoryBranchSettings', fields={
    'branch': drf_serializers.DictField(help_text='{id, branch_id, name}'),
    'overrides': drf_serializers.DictField(help_text='5 полей, null = как в сети'),
    'effective': drf_serializers.DictField(help_text='11 полей после резолва'),
    'source': drf_serializers.DictField(help_text='branch | network | branch_address | default'),
    'prizes': drf_serializers.DictField(help_text='{count, first, story_image_url}'),
    'rendered': drf_serializers.DictField(help_text='{activation_text, saved_text}'),
})
_ERR = OpenApiResponse(inline_serializer(name='StoryError', fields={
    'code': drf_serializers.CharField(),
    'detail': drf_serializers.CharField(),
}), description='invalid_payload (400, с editable) · role_not_allowed (403) · not_found (404)')


# ── ручки ────────────────────────────────────────────────────────────────────

class NetworkStorySettingsAPIView(APIView):
    """
    GET   /api/v1/settings/story/ — настройки механики у сети.
    PATCH /api/v1/settings/story/ — правка (только network_admin/суперадмин).

    Пишем ТОЛЬКО 11 полей белым списком: в `ClientConfig` рядом лежат
    брендинг, флаги механик и настройки интеграций — запись «всем телом»
    оттуда однажды снесёт чужую настройку всей сети.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: _SETTINGS_OUT}, tags=['v1'])
    def get(self, request):
        cfg = _network_config()
        return Response(self._payload(cfg))

    @extend_schema(request=_SETTINGS_OUT, responses={200: _SETTINGS_OUT, 400: _ERR, 403: _ERR},
                   tags=['v1'])
    def patch(self, request):
        if not _may_write(request.user):
            return _error('role_not_allowed',
                          'менять настройки сети может только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        cfg = _network_config()
        try:
            values = _collect(request.data or {}, NETWORK_FIELDS, inheritable=False)
            _check_campaign_window(cfg, values)
        except PayloadError as exc:
            return _error('invalid_payload', exc.detail, http_status.HTTP_400_BAD_REQUEST,
                          editable=NETWORK_FIELDS)
        for field, value in values.items():
            setattr(cfg, field, value)
        cfg.save(update_fields=sorted(values))
        log.info('story settings patched: %s fields=%s by=%s',
                 current_schema_name(), sorted(values), request.user)
        return Response(self._payload(cfg))

    @staticmethod
    def _payload(cfg) -> dict:
        from apps.tenant.catalog.models import Product
        return {
            'settings': _network_settings_dict(cfg),
            'placeholders': PLACEHOLDERS,
            'branch_override_fields': OVERRIDE_FIELDS,
            'prizes': {'network_count': Product.objects.filter(
                is_story_prize=True, is_archived=False).count()},
        }


class BranchStorySettingsAPIView(APIView):
    """
    GET   /api/v1/mobile/branches/{id}/story/ — переопределения точки,
          действующие значения, источник каждого значения, призы и превью.
    PATCH /api/v1/mobile/branches/{id}/story/ — переопределения; `null`, `""`
          и `0` означают «как в сети» (только network_admin/суперадмин).

    ⚠️ Порог «0 ₽» задать НЕЛЬЗЯ ни у точки, ни у сети: резолв трактует ноль
    как «не задано», поэтому `story_min_order_amount: 0` вернётся со
    `source: network` (или `default` = 600 ₽, если у сети тоже ноль). Ответ на
    такой PATCH — `200`, а не ошибка: поле принято, просто означает
    «наследовать». Настоящее «без порога» — правка резолва и миграция, вне
    v1.5 (★15/м16 ревью CheckUp).
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: _BRANCH_OUT, 404: _ERR}, tags=['v1'])
    def get(self, request, pk: int):
        branch = _branch_or_none(pk, request)
        if branch is None:
            return _error('not_found', 'Точка не найдена', http_status.HTTP_404_NOT_FOUND)
        return Response(self._payload(branch))

    @extend_schema(request=_BRANCH_OUT, responses={200: _BRANCH_OUT, 400: _ERR, 403: _ERR,
                                                   404: _ERR}, tags=['v1'])
    def patch(self, request, pk: int):
        if not _may_write(request.user):
            return _error('role_not_allowed',
                          'менять настройки точки может только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        branch = _branch_or_none(pk, request)
        if branch is None:
            return _error('not_found', 'Точка не найдена', http_status.HTTP_404_NOT_FOUND)
        try:
            values = _collect(request.data or {}, OVERRIDE_FIELDS, inheritable=True)
        except PayloadError as exc:
            return _error('invalid_payload', exc.detail, http_status.HTTP_400_BAD_REQUEST,
                          editable=OVERRIDE_FIELDS)

        with _atomic():
            cfg, created = BranchConfig.objects.get_or_create(branch=branch)
            for field, value in values.items():
                setattr(cfg, field, value)
            cfg.save(update_fields=sorted(values) if not created else None)
        log.info('story branch settings patched: %s branch=%s fields=%s by=%s',
                 current_schema_name(), branch.pk, sorted(values), request.user)
        branch = _branch_or_none(pk, request)  # перечитываем: config уже свежий
        return Response(self._payload(branch))

    @staticmethod
    def _payload(branch) -> dict:
        branch_cfg = getattr(branch, 'config', None)
        net_cfg = _network_config()
        branch_address = getattr(branch_cfg, 'address', '') or ''
        effective = _effective(branch)
        prizes, first_gift = _prizes(branch)
        return {
            'branch': {'id': branch.pk, 'branch_id': branch.branch_id, 'name': branch.name},
            'overrides': _overrides_dict(branch_cfg),
            'effective': effective,
            'source': {f: _source_for(f, branch_cfg, net_cfg, branch_address)
                       for f in NETWORK_FIELDS},
            'prizes': prizes,
            'rendered': _rendered(branch, effective, first_gift),
        }
