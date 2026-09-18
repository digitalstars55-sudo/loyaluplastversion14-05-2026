"""
Подключение ВКонтакте для внешнего кабинета CheckUp — контракт платформы, №55.

Что здесь настраивается. `SenlerConfig` (apps/tenant/senler/models.py:73) — это
не «настройки рассылок», как говорит его имя, а ЕДИНСТВЕННОЕ место, где живёт
связка «точка ↔ сообщество ВК». Токен оттуда обслуживает сразу четыре механики:

  • рассылки гостям               — senler/services.py `send_vk_message`;
  • ответы на отзывы и автоответ ИИ — branch/api/services.py;
  • опрос непрочитанных диалогов   — branch/tasks.py;
  • кнопку «поделиться номером»    — branch/api/vk_message_event.py.

Отсюда главное решение модуля: **менять токен, группу или секрет можно только
с `confirm: true`**, а новый токен перед записью проверяется у самого ВК
(`groups.getTokenPermissions` + `groups.getById`). Опечатка в токене иначе тихо
выключает все четыре механики разом, и узнают об этом через сутки по молчащим
отзывам.

Почему секрет и строка подтверждения пишутся НА ВСЮ ГРУППУ. Одно сообщество
может обслуживать несколько точек (несколько `SenlerConfig` с одним
`vk_group_id` — это норма, а не дубль). ВК шлёт на `/api/v1/vk/callback/` ровно
один секрет на группу. Когда-то у конфигов одной группы секреты разошлись,
приёмник сверял секрет первого попавшегося конфига и отвечал 403 — ВК отключил
callback, и сеть сутки не видела ни отзывов, ни ответов. Приёмник с тех пор
принимает секрет ЛЮБОГО конфига группы (branch/api/services.py:338-355), но
разъезд секретов остаётся миной: здесь PATCH раскладывает секрет и строку
подтверждения по ВСЕМ конфигам группы (`applied_to` в ответе), а сводка
`GET /api/v1/settings/vk/` показывает `secrets_consistent: false`, пока мина не
обезврежена.

Секреты наружу не отдаются НИКОГДА. Ни токен, ни `vk_callback_secret`, ни
строка подтверждения не попадают ни в один ответ: есть только `token_set` /
`token_last4` (последние 4 знака — чтобы человек узнал «тот ли токен»), а также
`secret_set` / `confirmation_set`. Прочитать записанное нельзя ни одной ручкой —
можно только перезаписать.

Права. Читают обе роли (`client` тоже: ему нужно понимать, почему молчат
отзывы). Пишут и проверяют — только `network_admin` и суперадмин
(`403 role_not_allowed`): смена токена задевает всю сеть.

Ошибки единой формой `{code, detail}`: `invalid_payload` и `confirm_required`
(400), `role_not_allowed` (403), `not_found` (404), `group_mismatch` и
`group_in_use` (409), `vk_error` (424, плюс `vk_error_code`/`vk_error_msg`),
`vk_timeout` (504). Отказ ВК не становится 500 никогда — ручка обязана
объяснить кабинету, что сломалось на стороне ВК, а не «что-то пошло не так».

Миграций модуль не требует.
"""
from __future__ import annotations

import logging

from django.db import transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.monitoring.checks import OUR_CALLBACK_MARKER, fetch_vk_callback_servers
from apps.shared.users.access import current_schema_name, effective_branch_ids
from apps.tenant.branch.models import Branch, TestimonialConversation
from apps.tenant.senler.models import BroadcastSend, SenlerConfig
from apps.tenant.senler.services import VkApiError, vk_group_info, vk_token_permissions

log = logging.getLogger(__name__)

# Белый список PATCH. `confirm` сюда не входит: это не поле конфига, а подпись
# под риском (разбирается отдельно), и писать его в модель нельзя.
EDITABLE = ('vk_group_id', 'vk_community_token', 'vk_callback_secret',
            'vk_callback_confirmation', 'is_active', 'notes')
CONFIRM_KEY = 'confirm'

# Поля, смена которых рвёт живую интеграцию → требуют `confirm: true`.
RISKY = ('vk_group_id', 'vk_community_token', 'vk_callback_secret')

# Поля, которые обязаны быть ОДИНАКОВЫМИ у всех конфигов одной группы.
GROUP_WIDE = ('vk_callback_secret', 'vk_callback_confirmation')

# Права токена сообщества, без которых продукт не работает:
#   messages — писать гостю (рассылки, ответы на отзывы, автоответ ИИ);
#   manage   — читать и чинить callback-серверы группы.
REQUIRED_PERMISSIONS = ('messages', 'manage')

MAX_TOKEN_LEN = 512          # SenlerConfig.vk_community_token
MAX_SECRET_LEN = 64          # SenlerConfig.vk_callback_secret
MAX_CONFIRMATION_LEN = 64    # SenlerConfig.vk_callback_confirmation
MAX_NOTES_LEN = 4000

CHECK_FIELDS = ('vk_group_id', 'vk_community_token')


# ── общие помощники (форма как в branch/api/story_settings.py) ────────────────

def _atomic():
    """transaction.atomic() отдельной функцией — тесты на моках подменяют её."""
    return transaction.atomic()


def _error(code: str, detail: str, status_code: int, **extra):
    payload = {'code': code, 'detail': detail}
    payload.update(extra)
    return Response(payload, status=status_code)


def _may_write(user) -> bool:
    """Подключение ВК правит админ сети и суперадмин; `client` — только читает."""
    return bool(getattr(user, 'is_superuser', False)
                or getattr(user, 'is_superadmin', False)
                or getattr(user, 'is_network_admin', False))


def _iso(value):
    return value.isoformat() if value else None


def _scope(request):
    """
    Доступные точки. ⚠️ `requested=None`, НИКОГДА `[]`.

    `None` — ограничений нет (все точки), `[-1]` — доступа нет вовсе, иначе
    список внутренних id. Проверять `is not None` при фильтрации нельзя:
    суперадмин получил бы пустую выдачу.
    """
    return effective_branch_ids(request.user, current_schema_name(), None)


def _branch_or_none(pk, request):
    """Точка с учётом доступа. None — нет такой или чужая (отвечаем 404)."""
    allowed = _scope(request)
    qs = Branch.objects.filter(pk=pk)
    if allowed:
        qs = qs.filter(pk__in=allowed)
    return qs.first()


def _config_for(branch):
    return SenlerConfig.objects.filter(branch_id=branch.pk).first()


def _primary_domain() -> str:
    """
    Primary-домен сети (`levone.levelupapp.ru`).

    Из НЕГО, а не из запроса, собирается callback-URL: CheckUp ходит к нам через
    loopback с подменённым `Host`, и `request.build_absolute_uri` дал бы
    `http://127.0.0.1:7000/...` — такой адрес в настройки ВК не вставишь.
    """
    try:
        from django.db import connection

        company = getattr(connection, 'tenant', None)
        if company is None:
            return ''
        from apps.shared.clients.models import Domain
        domain = (Domain.objects.filter(tenant=company, is_primary=True).first()
                  or Domain.objects.filter(tenant=company).first())
        return domain.domain if domain else ''
    except Exception:   # домен не повод отдать 500
        return ''


def _callback_url(domain: str) -> str:
    """Адрес, который человек вставляет в VK → Callback API (как в senler/admin.py:60)."""
    return f'https://{domain}/api/v1/vk/callback/' if domain else '/api/v1/vk/callback/'


def _last4(token: str):
    """Хвост токена — узнать «тот ли», не показывая сам токен."""
    token = token or ''
    return token[-4:] if token else None


def _config_state(config, callback_url: str) -> dict:
    """
    Состояние подключения БЕЗ секретов.

    Ни `vk_community_token`, ни `vk_callback_secret`, ни
    `vk_callback_confirmation` наружу не выходят — только «задано / не задано».
    """
    token = (getattr(config, 'vk_community_token', '') or '') if config is not None else ''
    group_id = getattr(config, 'vk_group_id', None) if config is not None else None
    return {
        'connected': config is not None,
        # `is_active` здесь — флаг САМОГО подключения (SenlerConfig.is_active),
        # не «точка работает»: у точки без конфига подключение выключено.
        'is_active': bool(getattr(config, 'is_active', False)) if config is not None else False,
        'vk_group_id': int(group_id) if group_id else None,
        'token_set': bool(token),
        'token_last4': _last4(token),
        'confirmation_set': bool((getattr(config, 'vk_callback_confirmation', '') or '')
                                 if config is not None else ''),
        'secret_set': bool((getattr(config, 'vk_callback_secret', '') or '')
                           if config is not None else ''),
        'callback_url': callback_url,
    }


def _branch_row(branch, config, callback_url: str) -> dict:
    """Строка сводки по сети (ручка №1)."""
    return {'id': branch.pk, 'branch_id': branch.branch_id, 'name': branch.name,
            **_config_state(config, callback_url)}


def _group_shared_with(branch, config) -> list:
    """
    Публичные `branch_id` ДРУГИХ точек на том же сообществе.

    Кабинет обязан это показать: правка секрета здесь меняет его и у соседей
    (иначе ВК отключит callback всей группе — см. шапку модуля).
    """
    group_id = getattr(config, 'vk_group_id', None) if config is not None else None
    if not group_id:
        return []
    out = []
    for sib in (SenlerConfig.objects.select_related('branch')
                .filter(vk_group_id=group_id)):
        if sib.branch_id == branch.pk:
            continue
        sib_branch = getattr(sib, 'branch', None)
        if sib_branch is not None:
            out.append(sib_branch.branch_id)
    return sorted(out)


def _card(branch, config, callback_url: str) -> dict:
    """Карточка подключения точки (ручка №2; она же тело ответа PATCH)."""
    return {
        'branch': {'id': branch.pk, 'branch_id': branch.branch_id, 'name': branch.name},
        **_config_state(config, callback_url),
        'notes': (getattr(config, 'notes', '') or '') if config is not None else '',
        'group_shared_with': _group_shared_with(branch, config),
        'updated_at': _iso(getattr(config, 'updated_at', None)) if config is not None else None,
    }


# ── разбор тела ──────────────────────────────────────────────────────────────

class PayloadError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _parse_group_id(raw):
    if raw in (None, ''):
        raise PayloadError('vk_group_id: числовой ID сообщества (без минуса)')
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise PayloadError('vk_group_id: целое число')
    if value <= 0:
        raise PayloadError('vk_group_id: положительное число — ID сообщества пишется без минуса')
    return value


def _parse_token(raw):
    token = str(raw or '').strip()
    if not token:
        raise PayloadError('vk_community_token: пустой токен')
    if len(token) > MAX_TOKEN_LEN:
        raise PayloadError(f'vk_community_token: не длиннее {MAX_TOKEN_LEN} символов')
    return token


def _parse_short(field: str, raw, limit: int) -> str:
    text = str(raw if raw is not None else '').strip()
    if len(text) > limit:
        raise PayloadError(f'{field}: не длиннее {limit} символов')
    return text


def _parse_bool(field: str, raw) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ('true', '1', 'yes'):
        return True
    if text in ('false', '0', 'no'):
        return False
    raise PayloadError(f'{field}: true или false')


def _parse_patch(data: dict) -> dict:
    values = {}
    if 'vk_group_id' in data:
        values['vk_group_id'] = _parse_group_id(data['vk_group_id'])
    if 'vk_community_token' in data:
        values['vk_community_token'] = _parse_token(data['vk_community_token'])
    if 'vk_callback_secret' in data:
        values['vk_callback_secret'] = _parse_short(
            'vk_callback_secret', data['vk_callback_secret'], MAX_SECRET_LEN)
    if 'vk_callback_confirmation' in data:
        values['vk_callback_confirmation'] = _parse_short(
            'vk_callback_confirmation', data['vk_callback_confirmation'], MAX_CONFIRMATION_LEN)
    if 'is_active' in data:
        values['is_active'] = _parse_bool('is_active', data['is_active'])
    if 'notes' in data:
        values['notes'] = _parse_short('notes', data['notes'], MAX_NOTES_LEN)
    return values


# ── разговор с ВК ────────────────────────────────────────────────────────────

def _vk_error_response(exc: VkApiError):
    """
    Отказ ВК → 424, недоступность ВК → 504. Пятисотки здесь быть не может.

    `code is None` у `VkApiError` означает «до ВК не достучались» (таймаут,
    сеть, битый ответ): виноваты не настройки, и кабинету надо предложить
    повтор, а не правку токена.
    """
    if exc.code is None:
        return _error('vk_timeout',
                      'ВКонтакте не ответил вовремя. Настройки не тронуты — попробуйте ещё раз.',
                      http_status.HTTP_504_GATEWAY_TIMEOUT, vk_error_msg=exc.msg)
    return _error('vk_error', f'ВКонтакте отклонил запрос: {exc.msg}',
                  http_status.HTTP_424_FAILED_DEPENDENCY,
                  vk_error_code=exc.code, vk_error_msg=exc.msg)


def _vk_probe(token: str):
    """
    Права токена + сообщество, которому он принадлежит. Два вызова ВК.

    `groups.getById` зовём БЕЗ `group_ids` намеренно: community-токену ВК
    отвечает его собственным сообществом, и только так можно ответить на
    вопрос «а от той ли группы этот токен» (спросив про чужую группу, мы
    получили бы отказ доступа, а не ответ).

    Любое исключение превращается в `VkApiError` — ручка обязана ответить
    424/504, а не 500.
    """
    try:
        permissions = vk_token_permissions(token)
        group = vk_group_info(token, None)
    except VkApiError:
        raise
    except Exception as exc:                       # pragma: no cover — страховка от 500
        raise VkApiError(None, f'проверка токена: {exc}') from exc
    return permissions, group


def _callback_servers(group_id, token: str):
    """
    Callback-серверы группы (`groups.getCallbackServers`) — третий и последний
    вызов ВК за запрос.

    Переиспользуем `fetch_vk_callback_servers` из мониторинга вместе с его
    маркером «наш домен»: второй копии этого знания в проекте быть не должно —
    у части сетей в той же группе живёт чужой callback (Senler), и путать их
    нельзя. Ошибка здесь НЕ валит проверку: чаще всего это отсутствующее право
    `manage`, о котором рядом уже написано в `missing_permissions`.
    """
    try:
        raw = fetch_vk_callback_servers(group_id, token) or []
    except Exception as exc:
        return [], str(exc)
    out = []
    for server in raw:
        url = server.get('url') or ''
        out.append({
            'id': server.get('id'),
            'title': server.get('title') or '',
            'url': url,
            'status': server.get('status') or '',
            'ours': OUR_CALLBACK_MARKER in url,
        })
    return out, None


# ── OpenAPI (только описание, на поведение не влияет) ────────────────────────

_SUMMARY_OUT = inline_serializer(name='VkConnectSummary', fields={
    'branches': drf_serializers.ListField(
        child=drf_serializers.DictField(),
        help_text='{id, branch_id, name, connected, is_active, vk_group_id, token_set, '
                  'token_last4, confirmation_set, secret_set, callback_url}'),
    'groups': drf_serializers.ListField(
        child=drf_serializers.DictField(),
        help_text='{vk_group_id, branches: [id], secrets_consistent}'),
})
_CARD_OUT = inline_serializer(name='VkConnectCard', fields={
    'branch': drf_serializers.DictField(help_text='{id, branch_id, name}'),
    'connected': drf_serializers.BooleanField(),
    'is_active': drf_serializers.BooleanField(),
    'vk_group_id': drf_serializers.IntegerField(allow_null=True),
    'token_set': drf_serializers.BooleanField(),
    'token_last4': drf_serializers.CharField(allow_null=True),
    'confirmation_set': drf_serializers.BooleanField(),
    'secret_set': drf_serializers.BooleanField(),
    'callback_url': drf_serializers.CharField(),
    'notes': drf_serializers.CharField(),
    'group_shared_with': drf_serializers.ListField(child=drf_serializers.IntegerField()),
    'updated_at': drf_serializers.CharField(allow_null=True),
})
_CHECK_IN = inline_serializer(name='VkConnectCheckIn', fields={
    'vk_group_id': drf_serializers.IntegerField(required=False),
    'vk_community_token': drf_serializers.CharField(required=False),
})
_CHECK_OUT = inline_serializer(name='VkConnectCheckOut', fields={
    'ok': drf_serializers.BooleanField(),
    'group': drf_serializers.DictField(allow_null=True, help_text='{id, name, screen_name, photo}'),
    'permissions': drf_serializers.ListField(child=drf_serializers.CharField()),
    'missing_permissions': drf_serializers.ListField(child=drf_serializers.CharField()),
    'group_matches': drf_serializers.BooleanField(),
    'callback_servers': drf_serializers.ListField(
        child=drf_serializers.DictField(), help_text='{id, title, url, status, ours}'),
    'callback_servers_error': drf_serializers.CharField(allow_null=True),
    'can_save': drf_serializers.BooleanField(),
})
_PATCH_IN = inline_serializer(name='VkConnectPatchIn', fields={
    'vk_group_id': drf_serializers.IntegerField(required=False),
    'vk_community_token': drf_serializers.CharField(required=False),
    'vk_callback_secret': drf_serializers.CharField(required=False),
    'vk_callback_confirmation': drf_serializers.CharField(required=False),
    'is_active': drf_serializers.BooleanField(required=False),
    'notes': drf_serializers.CharField(required=False),
    'confirm': drf_serializers.BooleanField(required=False),
})
_ERR = OpenApiResponse(inline_serializer(name='VkConnectError', fields={
    'code': drf_serializers.CharField(),
    'detail': drf_serializers.CharField(),
}), description='invalid_payload / confirm_required (400) · role_not_allowed (403) · '
                'not_found (404) · group_mismatch / group_in_use (409) · '
                'vk_error (424, + vk_error_code/vk_error_msg) · vk_timeout (504)')


# ── №1: сводка по сети ───────────────────────────────────────────────────────

class NetworkVkSettingsAPIView(APIView):
    """
    GET /api/v1/settings/vk/ — какие точки подключены к ВК и как.

    Ни одного вызова ВК: это моментальная карта настроек, её открывают, чтобы
    понять «куда смотреть», а не чтобы проверить токены (для этого есть
    `.../vk/check/`).

    `groups` — разрез по сообществам. `secrets_consistent: false` означает, что
    у конфигов одной группы РАЗНЫЕ (или пустые) секреты callback: именно этот
    разъезд однажды заставил ВК отключить callback всей сети. Согласованность
    считается по ВСЕМ конфигам группы, а в `branches` перечислены только точки,
    доступные сотруднику: иначе ограниченный по точкам сотрудник видел бы
    зелёный флаг там, где мина лежит у соседа.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: _SUMMARY_OUT}, tags=['v1'])
    def get(self, request):
        scope = _scope(request)
        qs = Branch.objects.all().order_by('name', 'pk')
        # Пустой scope = «все точки» (так effective_branch_ids отвечает
        # неограниченному сотруднику); [-1] = доступа нет и выдача пустая.
        if scope:
            qs = qs.filter(pk__in=scope)
        branches = list(qs)
        branch_pks = [b.pk for b in branches]

        visible = list(SenlerConfig.objects.filter(branch_id__in=branch_pks)) if branch_pks else []
        by_branch = {c.branch_id: c for c in visible}
        callback_url = _callback_url(_primary_domain())
        rows = [_branch_row(b, by_branch.get(b.pk), callback_url) for b in branches]

        group_ids = sorted({int(c.vk_group_id) for c in visible if c.vk_group_id})
        all_of_groups = (list(SenlerConfig.objects.filter(vk_group_id__in=group_ids))
                         if group_ids else [])
        visible_pks = set(branch_pks)
        groups = []
        for gid in group_ids:
            siblings = [c for c in all_of_groups if int(c.vk_group_id or 0) == gid] or \
                       [c for c in visible if int(c.vk_group_id or 0) == gid]
            secrets = [(c.vk_callback_secret or '') for c in siblings]
            groups.append({
                'vk_group_id': gid,
                'branches': sorted(c.branch_id for c in siblings if c.branch_id in visible_pks),
                # «Согласованы» = у всех конфигов группы один и тот же НЕПУСТОЙ
                # секрет. Пустой секрет — тоже беда: callback никем не подписан.
                'secrets_consistent': bool(secrets) and all(secrets) and len(set(secrets)) == 1,
            })

        return Response({'branches': rows, 'groups': groups})


# ── №2 и №4: карточка точки и её правка ──────────────────────────────────────

class BranchVkAPIView(APIView):
    """
    GET   /api/v1/mobile/branches/{id}/vk/ — карточка подключения (обе роли).
    PATCH /api/v1/mobile/branches/{id}/vk/ — правка (network_admin/суперадмин).

    PATCH создаёт подключение, если его ещё нет (тогда `vk_group_id` и
    `vk_community_token` обязательны). Смена токена, группы или секрета требует
    `confirm: true` — см. шапку модуля. Новый токен ВСЕГДА проверяется у ВК до
    записи; сменить группу у точки, по которой уже есть история (рассылки или
    диалоги отзывов), нельзя — `409 group_in_use`: старая переписка привязана к
    прежнему сообществу, и после подмены гости просто перестанут получать
    ответы в свой диалог.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: _CARD_OUT, 404: _ERR}, tags=['v1'])
    def get(self, request, pk: int):
        branch = _branch_or_none(pk, request)
        if branch is None:
            return _error('not_found', 'Точка не найдена', http_status.HTTP_404_NOT_FOUND)
        config = _config_for(branch)
        return Response(_card(branch, config, _callback_url(_primary_domain())))

    @extend_schema(request=_PATCH_IN,
                   responses={200: _CARD_OUT, 400: _ERR, 403: _ERR, 404: _ERR,
                              409: _ERR, 424: _ERR, 504: _ERR},
                   tags=['v1'])
    def patch(self, request, pk: int):
        if not _may_write(request.user):
            return _error('role_not_allowed',
                          'подключение ВКонтакте меняет только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        branch = _branch_or_none(pk, request)
        if branch is None:
            return _error('not_found', 'Точка не найдена', http_status.HTTP_404_NOT_FOUND)

        data = request.data if isinstance(request.data, dict) else None
        if data is None:
            return _error('invalid_payload', 'ожидается объект JSON',
                          http_status.HTTP_400_BAD_REQUEST, editable=list(EDITABLE))
        unknown = sorted(k for k in data if k not in EDITABLE and k != CONFIRM_KEY)
        if unknown:
            return _error('invalid_payload', 'нельзя менять здесь: ' + ', '.join(unknown),
                          http_status.HTTP_400_BAD_REQUEST,
                          unknown=unknown, editable=list(EDITABLE))
        try:
            values = _parse_patch(data)
            confirm = _parse_bool(CONFIRM_KEY, data.get(CONFIRM_KEY, False))
        except PayloadError as exc:
            return _error('invalid_payload', exc.detail, http_status.HTTP_400_BAD_REQUEST,
                          editable=list(EDITABLE))
        if not values:
            return _error('invalid_payload', 'нечего менять: передайте хотя бы одно поле',
                          http_status.HTTP_400_BAD_REQUEST, editable=list(EDITABLE))

        config = _config_for(branch)
        creating = config is None
        if creating and not ('vk_group_id' in values and 'vk_community_token' in values):
            return _error('invalid_payload',
                          'подключение заводится сразу с vk_group_id и vk_community_token',
                          http_status.HTTP_400_BAD_REQUEST, editable=list(EDITABLE))

        # Меняем только то, что реально отличается: повторный PATCH тем же
        # токеном не должен требовать подтверждения и дёргать ВК.
        changed = sorted(f for f, v in values.items()
                         if creating or getattr(config, f, None) != v)
        if not changed:
            return Response({**_card(branch, config, _callback_url(_primary_domain())),
                             'changed': [], 'applied_to': []})

        risky = [f for f in RISKY if f in changed]
        if not creating and risky and not confirm:
            return _error(
                'confirm_required',
                'Этот токен обслуживает рассылки, ответы на отзывы, автоответ ИИ и опрос '
                'сообщений сразу. Ошибка выключит их все разом. Повторите запрос с '
                'confirm: true, если уверены.',
                http_status.HTTP_400_BAD_REQUEST, fields=risky)

        # Проверка у ВК — ДО записи. Нужна, когда приходит новый токен либо
        # меняется группа (тогда сверяем, что сохранённый токен от неё же).
        target_group = values.get('vk_group_id', getattr(config, 'vk_group_id', None))
        if 'vk_community_token' in changed or 'vk_group_id' in changed:
            token = values.get('vk_community_token') or getattr(config, 'vk_community_token', '')
            if not token:
                return _error('invalid_payload',
                              'нет токена сообщества: передайте vk_community_token',
                              http_status.HTTP_400_BAD_REQUEST, editable=list(EDITABLE))
            try:
                permissions, group = _vk_probe(token)
            except VkApiError as exc:
                return _vk_error_response(exc)
            if target_group and str(group.get('id')) != str(target_group):
                return _error(
                    'group_mismatch',
                    f'Токен выдан сообществом «{group.get("name") or group.get("id")}» '
                    f'(id {group.get("id")}), а подключить его просят к сообществу '
                    f'{target_group}. Возьмите токен в том сообществе, ID которого указан.',
                    http_status.HTTP_409_CONFLICT,
                    vk_group_id=int(target_group), token_group_id=group.get('id'))
            missing = [p for p in REQUIRED_PERMISSIONS if p not in (permissions or [])]
            if missing:
                # Не блокируем: бывает, что права выдают уже после записи токена.
                # Но след в логе нужен — «почему молчат отзывы» начинают с него.
                log.warning('vk connect: token without permissions %s schema=%s branch=%s',
                            missing, current_schema_name(), branch.pk)

        # Смена группы у точки с историей запрещена: рассылки и диалоги отзывов
        # привязаны к прежнему сообществу.
        if not creating and 'vk_group_id' in changed:
            if (BroadcastSend.objects.filter(broadcast__branch=branch).exists()
                    or TestimonialConversation.objects.filter(branch=branch).exists()):
                return _error(
                    'group_in_use',
                    'По этой точке уже есть рассылки или диалоги отзывов в прежнем сообществе. '
                    'Смена ID сообщества оставит эту переписку без ответов. Заведите точку '
                    'заново или обратитесь в поддержку.',
                    http_status.HTTP_409_CONFLICT, vk_group_id=int(target_group or 0))

        applied_to: list = []
        with _atomic():
            if creating:
                config = SenlerConfig.objects.create(branch=branch, **values)
            else:
                for field, value in values.items():
                    setattr(config, field, value)
                config.save(update_fields=changed)

            shared = {f: values[f] for f in GROUP_WIDE if f in values}
            if shared and getattr(config, 'vk_group_id', None):
                applied_to = self._apply_to_group(branch, config, shared)

        try:
            from apps.shared.audit.services import record_event
            record_event(action='update', request=request, actor=request.user,
                         target=f'Подключение ВК точки «{branch.name}» (id={branch.pk})',
                         # В журнал — только ИМЕНА полей: значения токена и
                         # секрета не должны осесть ни в одной таблице логов.
                         meta={'via': 'api', 'fields': changed, 'created': creating,
                               'applied_to': applied_to})
        except Exception:
            pass
        log.info('vk connect patched: %s branch=%s fields=%s created=%s applied_to=%s by=%s',
                 current_schema_name(), branch.pk, changed, creating, applied_to,
                 getattr(request.user, 'username', '?'))

        return Response({**_card(branch, config, _callback_url(_primary_domain())),
                         'changed': changed, 'applied_to': applied_to})

    @staticmethod
    def _apply_to_group(branch, config, shared: dict) -> list:
        """
        Секрет и строку подтверждения — на ВСЕ конфиги группы.

        ВК шлёт на callback один секрет на сообщество. Разъезд секретов у
        конфигов одной группы уже стоил сети суток без отзывов, поэтому правка
        здесь принудительно выравнивает всю группу, а не одну точку.
        `applied_to` — публичные `branch_id` всех выровненных точек.
        """
        applied = [branch.branch_id]
        for sib in (SenlerConfig.objects.select_related('branch')
                    .filter(vk_group_id=config.vk_group_id)):
            if sib.branch_id == branch.pk:
                continue
            dirty = sorted(f for f, v in shared.items() if getattr(sib, f, None) != v)
            if dirty:
                for field, value in shared.items():
                    setattr(sib, field, value)
                sib.save(update_fields=dirty)
            sib_branch = getattr(sib, 'branch', None)
            if sib_branch is not None:
                applied.append(sib_branch.branch_id)
        return sorted(set(applied))


# ── №3: проверка подключения ─────────────────────────────────────────────────

class BranchVkCheckAPIView(APIView):
    """
    POST /api/v1/mobile/branches/{id}/vk/check/ — «а живой ли токен?».

    Тело `{vk_group_id?, vk_community_token?}` — проверить ещё НЕ сохранённые
    значения (форма в кабинете); пустое тело — проверить сохранённые. Ручка
    НИЧЕГО не пишет: её зовут до PATCH и после, чтобы понять, что именно
    сломалось.

    Не больше трёх вызовов ВК за запрос: права токена, его сообщество и
    callback-серверы группы. Отказ ВК — 424 с настоящим `vk_error_code`
    (5 — токен невалиден, 27 — ключ сообщества недействителен, 15 — нет
    доступа): кабинет показывает человеку причину, а не «ошибка сервера».
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(request=_CHECK_IN,
                   responses={200: _CHECK_OUT, 400: _ERR, 403: _ERR, 404: _ERR,
                              424: _ERR, 504: _ERR},
                   tags=['v1'])
    def post(self, request, pk: int):
        if not _may_write(request.user):
            return _error('role_not_allowed',
                          'проверять подключение ВКонтакте может только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        branch = _branch_or_none(pk, request)
        if branch is None:
            return _error('not_found', 'Точка не найдена', http_status.HTTP_404_NOT_FOUND)

        data = request.data if isinstance(request.data, dict) else {}
        unknown = sorted(k for k in data if k not in CHECK_FIELDS)
        if unknown:
            return _error('invalid_payload', 'лишние поля: ' + ', '.join(unknown),
                          http_status.HTTP_400_BAD_REQUEST,
                          unknown=unknown, editable=list(CHECK_FIELDS))

        config = _config_for(branch)
        try:
            if 'vk_group_id' in data:
                group_id = _parse_group_id(data['vk_group_id'])
            else:
                group_id = getattr(config, 'vk_group_id', None) if config is not None else None
            if 'vk_community_token' in data:
                token = _parse_token(data['vk_community_token'])
            else:
                token = ((getattr(config, 'vk_community_token', '') or '')
                         if config is not None else '')
        except PayloadError as exc:
            return _error('invalid_payload', exc.detail, http_status.HTTP_400_BAD_REQUEST,
                          editable=list(CHECK_FIELDS))
        if not token:
            return _error('invalid_payload',
                          'проверять нечего: у точки нет сохранённого токена, '
                          'передайте vk_community_token',
                          http_status.HTTP_400_BAD_REQUEST, editable=list(CHECK_FIELDS))

        try:
            permissions, group = _vk_probe(token)
        except VkApiError as exc:
            return _vk_error_response(exc)

        token_group_id = group.get('id')
        missing = [p for p in REQUIRED_PERMISSIONS if p not in (permissions or [])]
        # Группу не назвали ни в теле, ни в настройках — сверять не с чем:
        # сохранится сообщество самого токена, противоречия нет.
        group_matches = (str(token_group_id) == str(group_id) if group_id
                         else bool(token_group_id))

        servers, servers_error = [], None
        probe_group = group_id or token_group_id
        if probe_group:
            servers, servers_error = _callback_servers(probe_group, token)

        ok = bool(token_group_id)
        return Response({
            'ok': ok,
            'group': (group if token_group_id else None),
            'permissions': permissions or [],
            'missing_permissions': missing,
            'group_matches': group_matches,
            'callback_servers': servers,
            # Своё поле сверх контракта: без него отказ getCallbackServers
            # (чаще всего — нет права `manage`) выглядел бы как «серверов нет».
            'callback_servers_error': servers_error,
            'can_save': ok and not missing and group_matches,
        })
