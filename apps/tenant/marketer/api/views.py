"""
AI-маркетолог для кабинета CheckUp — контракт 3б.5 (№29).

API у приложения не было вовсе: настройки и посты жили только в админке. Здесь
ровно то, что нужно экранам CheckUp, и ни строкой больше — кнопки, подтверждения
и тексты живут на их стороне (решение владельца), наша сторона отдаёт состояние
и выполняет действия.

Что решено и почему:
  • `vk_wall_token` НЕ отдаётся и НЕ пишется через API — только признак
    `vk_wall_token_set`. Это пользовательский токен админа сообщества: утечка
    даёт право писать на стену от лица сети, а «Стена» и «Сообщения» у нас
    намеренно разные каналы (senler-токеном стену не постим, чтобы спам-флаг не
    уронил отзывы и рассылки). Токен остаётся в админке до отдельного слова
    владельца (№55).
  • Генерация — `202 {queued: true}` и блокировка на 5 минут по сети через
    кэш (`marketer-generate:<схема>`): поля «генерируется» в модели нет и
    заводить его не будем — иначе понадобится миграция и снятие зависшего
    флага. Повторный вызов внутри окна → `409 already_generating`.
  • Публикация идемпотентна по состоянию: уже опубликованный пост отвечает
    `200` со своим состоянием и НЕ уходит в `wall.post` второй раз (иначе на
    стене появятся два одинаковых поста).
  • Ошибку ВК отдаём как есть (`502 vk_error` с текстом из `post.error`):
    «VK error 214: Access denied» администратору понятнее любой нашей
    формулировки.
  • Сущность сетевая, точек у неё нет — RBAC по точкам неприменим: `client`
    только читает, писать может `network_admin` и суперадмин.

Ошибки — единой формой `{code, detail}`.
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.users.access import current_schema_name
from apps.tenant.marketer.models import (
    MarketerPost,
    MarketerPostStatus,
    MarketerPostType,
    MarketerSettings,
)

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_TEXT_LEN = 16000
# Окно блокировки генерации: дайджест пишется ИИ около минуты, пять минут с
# запасом закрывают двойной клик и параллельных сотрудников.
GENERATE_LOCK_SECONDS = 300
GENERATE_LOCK_KEY = 'marketer-generate:%s'

SETTINGS_FIELDS = (
    ('is_enabled', 'bool'),
    ('vk_group_id', 'int_or_null'),
    ('autopost_enabled', 'bool'),
    ('digest_enabled', 'bool'),
    ('digest_weekday', 'weekday'),
    ('digest_hour', 'hour'),
    ('brand_voice', 'text'),
    ('extra_facts', 'text'),
)
EDITABLE_SETTINGS = [name for name, _ in SETTINGS_FIELDS]
EDITABLE_STATUSES = (MarketerPostStatus.DRAFT, MarketerPostStatus.FAILED)
PUBLISHABLE_STATUSES = (MarketerPostStatus.DRAFT, MarketerPostStatus.FAILED)


# ── общие помощники ──────────────────────────────────────────────────────────

def _error(code: str, detail: str, status_code: int, **extra):
    payload = {'code': code, 'detail': detail}
    payload.update(extra)
    return Response(payload, status=status_code)


def _iso(value):
    return value.isoformat() if value else None


def _may_write(user) -> bool:
    return bool(getattr(user, 'is_superuser', False)
                or getattr(user, 'is_superadmin', False)
                or getattr(user, 'is_network_admin', False))


def _author(user) -> str:
    """Кто автор правки: полное имя, иначе username (у автогенерации — «ai»)."""
    full_name = ''
    getter = getattr(user, 'get_full_name', None)
    if callable(getter):
        try:
            full_name = (getter() or '').strip()
        except Exception:
            full_name = ''
    return (full_name or getattr(user, 'username', '') or '')[:150]


def _page_params(request):
    try:
        limit = int(request.query_params.get('limit') or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        offset = int(request.query_params.get('offset') or 0)
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, MAX_LIMIT)), max(0, offset)


def _settings_row():
    """Настройки сети (создаём пустые, если их ещё нет — как делает админка)."""
    cfg = MarketerSettings.objects.first()
    if cfg is None:
        cfg = MarketerSettings.objects.create()
    return cfg


def _group_id(cfg):
    from apps.tenant.marketer.publisher import _resolve_group_id
    try:
        return _resolve_group_id(cfg)
    except Exception:  # id группы — украшение ссылки, не повод отдать 500
        return None


def settings_to_dict(cfg) -> dict:
    return {
        'is_enabled': cfg.is_enabled,
        'vk_group_id': cfg.vk_group_id,
        # Сам токен не отдаём НИКОГДА — только факт его наличия.
        'vk_wall_token_set': bool(cfg.vk_wall_token),
        'autopost_enabled': cfg.autopost_enabled,
        'digest_enabled': cfg.digest_enabled,
        'digest_weekday': cfg.digest_weekday,
        'digest_weekday_label': dict(MarketerSettings.WEEKDAYS).get(cfg.digest_weekday, ''),
        'digest_hour': cfg.digest_hour,
        'last_digest_at': _iso(cfg.last_digest_at),
        'brand_voice': cfg.brand_voice,
        'extra_facts': cfg.extra_facts,
        'editable': EDITABLE_SETTINGS,
    }


def post_to_dict(post, group_id=None) -> dict:
    vk_post_url = ''
    if post.vk_post_id and group_id:
        vk_post_url = f'https://vk.com/wall-{group_id}_{post.vk_post_id}'
    return {
        'id': post.pk,
        'post_type': post.post_type,
        'post_type_label': post.get_post_type_display(),
        'status': post.status,
        'status_label': post.get_status_display(),
        'text': post.text,
        'model_used': post.model_used,
        'created_by': post.created_by,
        # 'ai' в created_by означает автогенерацию задачей, а не человека.
        'created_by_label': 'ИИ' if post.created_by == 'ai' else post.created_by,
        'published_at': _iso(post.published_at),
        'vk_post_id': post.vk_post_id,
        'vk_post_url': vk_post_url,
        'error': post.error,
        'is_editable': post.status in EDITABLE_STATUSES,
        'created_at': _iso(post.created_at),
        'updated_at': _iso(post.updated_at),
    }


class PayloadError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _parse_settings(data) -> dict:
    """Белый список: чужие ключи — ошибка. `vk_wall_token` не принимаем вовсе."""
    if 'vk_wall_token' in data:
        raise PayloadError('vk_wall_token через API не задаётся: токен стены живёт в админке')
    kinds = dict(SETTINGS_FIELDS)
    unknown = [k for k in data.keys() if k not in kinds]
    if unknown:
        raise PayloadError('нельзя менять здесь: ' + ', '.join(sorted(unknown)))
    values = {}
    for field, kind in SETTINGS_FIELDS:
        if field not in data:
            continue
        raw = data[field]
        if kind == 'bool':
            if isinstance(raw, bool):
                values[field] = raw
            elif str(raw).strip().lower() in ('true', '1', 'yes'):
                values[field] = True
            elif str(raw).strip().lower() in ('false', '0', 'no'):
                values[field] = False
            else:
                raise PayloadError(f'{field}: true или false')
        elif kind == 'int_or_null':
            if raw in (None, ''):
                values[field] = None
            else:
                try:
                    number = int(raw)
                except (TypeError, ValueError):
                    raise PayloadError(f'{field}: целое число или null')
                if number <= 0:
                    raise PayloadError(f'{field}: положительное число (ID группы без минуса)')
                values[field] = number
        elif kind == 'weekday':
            try:
                number = int(raw)
            except (TypeError, ValueError):
                raise PayloadError('digest_weekday: 0 (понедельник) … 6 (воскресенье)')
            if number not in dict(MarketerSettings.WEEKDAYS):
                raise PayloadError('digest_weekday: 0 (понедельник) … 6 (воскресенье)')
            values[field] = number
        elif kind == 'hour':
            try:
                number = int(raw)
            except (TypeError, ValueError):
                raise PayloadError('digest_hour: целое 0…23 (МСК)')
            if not 0 <= number <= 23:
                raise PayloadError('digest_hour: целое 0…23 (МСК)')
            values[field] = number
        else:
            values[field] = str(raw or '')
    if not values:
        raise PayloadError('нечего менять: передайте хотя бы одно поле')
    return values


def _confirmed(data) -> bool:
    value = data.get('confirm')
    if value is True:
        return True
    return str(value).strip().lower() in ('true', '1', 'yes')


# ── OpenAPI ──────────────────────────────────────────────────────────────────

_SETTINGS_OUT = inline_serializer(name='MarketerSettingsOut', fields={
    'is_enabled': drf_serializers.BooleanField(),
    'vk_group_id': drf_serializers.IntegerField(allow_null=True),
    'vk_wall_token_set': drf_serializers.BooleanField(help_text='сам токен не отдаётся'),
    'autopost_enabled': drf_serializers.BooleanField(),
    'digest_enabled': drf_serializers.BooleanField(),
    'digest_weekday': drf_serializers.IntegerField(help_text='0 = понедельник'),
    'digest_hour': drf_serializers.IntegerField(),
    'last_digest_at': drf_serializers.CharField(allow_null=True),
    'brand_voice': drf_serializers.CharField(allow_blank=True),
    'extra_facts': drf_serializers.CharField(allow_blank=True),
    'editable': drf_serializers.ListField(child=drf_serializers.CharField()),
})
_POST_OUT = inline_serializer(name='MarketerPostOut', fields={
    'id': drf_serializers.IntegerField(),
    'post_type': drf_serializers.CharField(),
    'status': drf_serializers.CharField(),
    'text': drf_serializers.CharField(),
    'vk_post_url': drf_serializers.CharField(allow_blank=True),
    'error': drf_serializers.CharField(allow_blank=True),
})
_POSTS_OUT = inline_serializer(name='MarketerPostsOut', fields={
    'total': drf_serializers.IntegerField(),
    'limit': drf_serializers.IntegerField(),
    'offset': drf_serializers.IntegerField(),
    'results': drf_serializers.ListField(child=drf_serializers.DictField()),
})
_ERR = OpenApiResponse(inline_serializer(name='MarketerError', fields={
    'code': drf_serializers.CharField(),
    'detail': drf_serializers.CharField(),
}), description=('invalid_payload (400) · confirm_required (400) · role_not_allowed (403) · '
                 'not_found (404) · marketer_disabled / no_vk_token / not_editable / '
                 'not_publishable / not_rejectable / already_generating (409) · vk_error (502)'))


# ── настройки ────────────────────────────────────────────────────────────────

class MarketerSettingsAPIView(APIView):
    """
    GET   /api/v1/marketer/settings/ — состояние маркетолога.
    PATCH /api/v1/marketer/settings/ — 8 полей белым списком (network_admin).

    `vk_wall_token` не отдаётся и не принимается: в ответе только
    `vk_wall_token_set`.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: _SETTINGS_OUT}, tags=['v1'])
    def get(self, request):
        return Response(settings_to_dict(_settings_row()))

    @extend_schema(request=_SETTINGS_OUT, responses={200: _SETTINGS_OUT, 400: _ERR, 403: _ERR},
                   tags=['v1'])
    def patch(self, request):
        if not _may_write(request.user):
            return _error('role_not_allowed', 'менять настройки может только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        try:
            values = _parse_settings(request.data or {})
        except PayloadError as exc:
            return _error('invalid_payload', exc.detail, http_status.HTTP_400_BAD_REQUEST,
                          editable=EDITABLE_SETTINGS)
        cfg = _settings_row()
        for field, value in values.items():
            setattr(cfg, field, value)
        cfg.save(update_fields=sorted(values) + ['updated_at'])
        log.info('marketer settings patched: %s fields=%s by=%s',
                 current_schema_name(), sorted(values), _author(request.user))
        return Response(settings_to_dict(cfg))


# ── лента постов ─────────────────────────────────────────────────────────────

class MarketerPostListAPIView(APIView):
    """GET /api/v1/marketer/posts/?status=&post_type=&limit=&offset="""
    permission_classes = [IsAuthenticated]

    @extend_schema(parameters=[
        OpenApiParameter('status', str, description=' | '.join(MarketerPostStatus.values)),
        OpenApiParameter('post_type', str, description=' | '.join(MarketerPostType.values)),
        OpenApiParameter('limit', int), OpenApiParameter('offset', int),
    ], responses={200: _POSTS_OUT, 400: _ERR}, tags=['v1'])
    def get(self, request):
        qs = MarketerPost.objects.all().order_by('-created_at')
        status_filter = (request.query_params.get('status') or '').strip()
        if status_filter:
            if status_filter not in MarketerPostStatus.values:
                return _error('invalid_payload', 'status: ' + ' | '.join(MarketerPostStatus.values),
                              http_status.HTTP_400_BAD_REQUEST)
            qs = qs.filter(status=status_filter)
        type_filter = (request.query_params.get('post_type') or '').strip()
        if type_filter:
            if type_filter not in MarketerPostType.values:
                return _error('invalid_payload',
                              'post_type: ' + ' | '.join(MarketerPostType.values),
                              http_status.HTTP_400_BAD_REQUEST)
            qs = qs.filter(post_type=type_filter)

        limit, offset = _page_params(request)
        total = qs.count()
        rows = list(qs[offset:offset + limit])
        group_id = _group_id(_settings_row())
        return Response({'total': total, 'limit': limit, 'offset': offset,
                         'results': [post_to_dict(p, group_id) for p in rows]})


def _post_or_none(pk):
    return MarketerPost.objects.filter(pk=pk).first()


class MarketerPostDetailAPIView(APIView):
    """
    GET   /api/v1/marketer/posts/{id}/
    PATCH /api/v1/marketer/posts/{id}/ {text} — только у draft и failed.

    У `failed` правка текста статус НЕ меняет: пост остаётся упавшим, пока его
    не опубликуют повторно (иначе из ленты пропадёт след ошибки).
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: _POST_OUT, 404: _ERR}, tags=['v1'])
    def get(self, request, pk: int):
        post = _post_or_none(pk)
        if post is None:
            return _error('not_found', 'Пост не найден', http_status.HTTP_404_NOT_FOUND)
        return Response(post_to_dict(post, _group_id(_settings_row())))

    @extend_schema(request=_POST_OUT, responses={200: _POST_OUT, 400: _ERR, 403: _ERR,
                                                 404: _ERR, 409: _ERR}, tags=['v1'])
    def patch(self, request, pk: int):
        if not _may_write(request.user):
            return _error('role_not_allowed', 'править пост может только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        post = _post_or_none(pk)
        if post is None:
            return _error('not_found', 'Пост не найден', http_status.HTTP_404_NOT_FOUND)
        if post.status not in EDITABLE_STATUSES:
            return _error('not_editable',
                          f'пост в статусе «{post.get_status_display()}» — текст менять нельзя',
                          http_status.HTTP_409_CONFLICT, status=post.status)
        data = request.data or {}
        if 'text' not in data:
            return _error('invalid_payload', 'text: новый текст поста',
                          http_status.HTTP_400_BAD_REQUEST)
        text = str(data.get('text') or '').strip()
        if not text:
            return _error('invalid_payload', 'text: пустой текст публиковать нечем',
                          http_status.HTTP_400_BAD_REQUEST)
        if len(text) > MAX_TEXT_LEN:
            return _error('invalid_payload', f'text: не длиннее {MAX_TEXT_LEN} символов',
                          http_status.HTTP_400_BAD_REQUEST)
        post.text = text
        post.save(update_fields=['text', 'updated_at'])
        log.info('marketer post %s edited: %s by=%s', post.pk, current_schema_name(),
                 _author(request.user))
        return Response(post_to_dict(post, _group_id(_settings_row())))


class MarketerPostContextAPIView(APIView):
    """GET /api/v1/marketer/posts/{id}/context/ — снимок фактов, из которых написан пост."""
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: OpenApiResponse(description='{context}'), 404: _ERR},
                   tags=['v1'])
    def get(self, request, pk: int):
        post = _post_or_none(pk)
        if post is None:
            return _error('not_found', 'Пост не найден', http_status.HTTP_404_NOT_FOUND)
        return Response({'id': post.pk, 'context': post.context_snapshot or {}})


# ── действия ─────────────────────────────────────────────────────────────────

class MarketerGenerateAPIView(APIView):
    """
    POST /api/v1/marketer/posts/generate/ — запустить генерацию дайджеста.

    Отвечает `202 {queued: true}`: пост появится в ленте черновиком (или
    `failed` с ошибкой) в течение минуты. Повторный запуск внутри окна
    блокировки → `409 already_generating` (поля «генерируется» в модели нет —
    блокировка живёт в кэше и сама истекает).
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={202: OpenApiResponse(description='{queued: true}'),
                                            403: _ERR, 409: _ERR}, tags=['v1'])
    def post(self, request):
        if not _may_write(request.user):
            return _error('role_not_allowed', 'запускать генерацию может только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        cfg = _settings_row()
        if not cfg.is_enabled:
            return _error('marketer_disabled', 'AI-маркетолог выключен в настройках',
                          http_status.HTTP_409_CONFLICT)
        if not cfg.digest_enabled:
            return _error('marketer_disabled', 'дайджест выключен в настройках',
                          http_status.HTTP_409_CONFLICT)

        schema = current_schema_name()
        key = GENERATE_LOCK_KEY % schema
        if not cache.add(key, timezone.now().isoformat(), GENERATE_LOCK_SECONDS):
            return _error('already_generating',
                          'генерация уже идёт — черновик появится в ленте в течение минуты',
                          http_status.HTTP_409_CONFLICT,
                          retry_after=GENERATE_LOCK_SECONDS)

        from apps.tenant.marketer.tasks import run_marketer_digest_for_tenant_task
        try:
            run_marketer_digest_for_tenant_task.delay(schema)
        except Exception as exc:
            # Очередь недоступна — блокировку снимаем, иначе кабинет будет
            # получать 409 пять минут на пустом месте.
            cache.delete(key)
            log.exception('marketer generate: очередь недоступна')
            return _error('queue_unavailable', f'не удалось поставить задачу: {exc}',
                          http_status.HTTP_503_SERVICE_UNAVAILABLE)
        log.info('marketer generate queued: %s by=%s', schema, _author(request.user))
        return Response({'queued': True, 'lock_seconds': GENERATE_LOCK_SECONDS},
                        status=http_status.HTTP_202_ACCEPTED)


class MarketerPostPublishAPIView(APIView):
    """
    POST /api/v1/marketer/posts/{id}/publish/ {confirm: true}

    Публикация необратима, поэтому нужен `confirm`. Идемпотентна по состоянию:
    уже опубликованный пост отвечает `200` и вторым `wall.post` не уходит.
    Ошибку ВК отдаём как есть — `502 vk_error`.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={200: _POST_OUT, 400: _ERR, 403: _ERR, 404: _ERR,
                                            409: _ERR, 502: _ERR}, tags=['v1'])
    def post(self, request, pk: int):
        if not _may_write(request.user):
            return _error('role_not_allowed', 'публиковать может только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        post = _post_or_none(pk)
        if post is None:
            return _error('not_found', 'Пост не найден', http_status.HTTP_404_NOT_FOUND)

        cfg = _settings_row()
        group_id = _group_id(cfg)
        if post.status == MarketerPostStatus.PUBLISHED:
            # Идемпотентность: повторный вызов ничего не делает и не ошибка.
            return Response(dict(post_to_dict(post, group_id), already_published=True))
        if not _confirmed(request.data or {}):
            return _error('confirm_required', 'публикация необратима — подтвердите confirm: true',
                          http_status.HTTP_400_BAD_REQUEST)
        if post.status not in PUBLISHABLE_STATUSES:
            return _error('not_publishable',
                          f'пост в статусе «{post.get_status_display()}» опубликовать нельзя',
                          http_status.HTTP_409_CONFLICT, status=post.status)
        if not (post.text or '').strip():
            return _error('not_publishable',
                          'пустой текст — публиковать нечего (генерация не удалась)',
                          http_status.HTTP_409_CONFLICT, status=post.status)
        # Конфигурацию проверяем ДО publish_post: иначе он пометит пост failed
        # из-за настройки, и в ленте появится ложная «ошибка публикации».
        if not cfg.is_enabled:
            return _error('marketer_disabled', 'AI-маркетолог выключен в настройках',
                          http_status.HTTP_409_CONFLICT)
        if not cfg.vk_wall_token:
            return _error('no_vk_token',
                          'не задан токен стены (задаётся в админке LoyalUP)',
                          http_status.HTTP_409_CONFLICT)

        from apps.tenant.marketer.publisher import publish_post
        ok = publish_post(post)
        post.refresh_from_db()
        if not ok:
            log.warning('marketer publish via API failed: post=%s %s', post.pk, post.error)
            return _error('vk_error', post.error or 'публикация не удалась',
                          http_status.HTTP_502_BAD_GATEWAY,
                          post=post_to_dict(post, group_id))
        log.info('marketer post %s published via API by=%s', post.pk, _author(request.user))
        return Response(post_to_dict(post, group_id))


class MarketerPostRejectAPIView(APIView):
    """
    POST /api/v1/marketer/posts/{id}/reject/ — отклонить черновик.

    Только из `draft`: отклонять опубликованный пост нечего (его снимают в ВК),
    а у `failed` есть повторная публикация.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(request=None, responses={200: _POST_OUT, 403: _ERR, 404: _ERR, 409: _ERR},
                   tags=['v1'])
    def post(self, request, pk: int):
        if not _may_write(request.user):
            return _error('role_not_allowed', 'отклонять пост может только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        post = _post_or_none(pk)
        if post is None:
            return _error('not_found', 'Пост не найден', http_status.HTTP_404_NOT_FOUND)
        if post.status == MarketerPostStatus.REJECTED:
            return Response(dict(post_to_dict(post, _group_id(_settings_row())),
                                 already_rejected=True))
        if post.status != MarketerPostStatus.DRAFT:
            return _error('not_rejectable',
                          f'пост в статусе «{post.get_status_display()}» отклонить нельзя',
                          http_status.HTTP_409_CONFLICT, status=post.status)
        post.status = MarketerPostStatus.REJECTED
        post.save(update_fields=['status', 'updated_at'])
        log.info('marketer post %s rejected by=%s', post.pk, _author(request.user))
        return Response(post_to_dict(post, _group_id(_settings_row())))
