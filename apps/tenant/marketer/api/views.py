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

from apps.shared.users.feature_permissions import HasFeatureAccess

import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.users.access import current_schema_name
from apps.tenant.marketer import progress
from apps.tenant.marketer.models import (
    MarketerJob,
    MarketerPhoto,
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
    ('image_style', 'text'),
    ('image_source', 'image_source'),
)
IMAGE_SOURCES = ('ai', 'bank_first')
EDITABLE_SETTINGS = [name for name, _ in SETTINGS_FIELDS]
EDITABLE_STATUSES = (MarketerPostStatus.DRAFT, MarketerPostStatus.SCHEDULED, MarketerPostStatus.FAILED)
PUBLISHABLE_STATUSES = (MarketerPostStatus.DRAFT, MarketerPostStatus.SCHEDULED, MarketerPostStatus.FAILED)
MAX_IMAGE_BYTES = 10 * 1024 * 1024
PLAN_MAX_POSTS = 31


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
        'image_style': getattr(cfg, 'image_style', ''),
        'image_source': getattr(cfg, 'image_source', 'ai') or 'ai',
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
        'scheduled_at': _iso(post.scheduled_at),
        'topic': post.topic,
        'image_url': post.image.url if post.image else '',
        'image_source': getattr(post, 'image_source', '') or '',
        'visual': getattr(post, 'visual', '') or '',
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
        elif kind == 'image_source':
            if str(raw) not in IMAGE_SOURCES:
                raise PayloadError('image_source: ai или bank_first')
            values[field] = str(raw)
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
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

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
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

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

        # Календарь контент-плана: ?from=ГГГГ-ММ-ДД&to=ГГГГ-ММ-ДД — по дате публикации
        # (запланированной или фактической).
        date_from = parse_date((request.query_params.get('from') or '').strip())
        date_to = parse_date((request.query_params.get('to') or '').strip())
        if date_from or date_to:
            from django.db.models import Q
            from django.db.models.functions import Coalesce
            qs = qs.annotate(_when=Coalesce('published_at', 'scheduled_at')).filter(_when__isnull=False)
            if date_from:
                qs = qs.filter(_when__date__gte=date_from)
            if date_to:
                qs = qs.filter(_when__date__lte=date_to)
            qs = qs.order_by('_when')

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
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

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


class MarketerPostRewriteAPIView(APIView):
    """
    POST /api/v1/marketer/posts/{id}/rewrite/ {instruction, text?} → {text, model}

    Задание для ИИ прямо в окне правки (владелец CheckUp, 16.10.2026). Новый
    текст только ВОЗВРАЩАЕТСЯ: пост меняет обычное «Сохранить» (PATCH). Так
    задание не становится текстом публикации, прежняя версия остаётся у
    человека в окне, а запланированный пост не уйдёт с непроверенным текстом.
    `text` — что сейчас в окне (по умолчанию — сохранённый текст поста).
    """
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

    @extend_schema(request=None, responses={200: OpenApiResponse(description='{text, model}'),
                                            400: _ERR, 403: _ERR, 404: _ERR, 409: _ERR,
                                            502: _ERR}, tags=['v1'])
    def post(self, request, pk: int):
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
        instruction = str(data.get('instruction') or '').strip()
        if not instruction:
            return _error('invalid_payload', 'instruction: напишите, что сделать с текстом',
                          http_status.HTTP_400_BAD_REQUEST)
        текущий = str(data.get('text') if data.get('text') is not None else post.text)
        from apps.tenant.marketer.generator import rewrite_post
        try:
            out = rewrite_post(текущий[:MAX_TEXT_LEN], instruction[:1500],
                               brand_voice=_settings_row().brand_voice or '')
        except Exception as exc:
            log.warning('marketer rewrite %s failed: %s', post.pk, exc)
            return _error('ai_failed', f'ИИ не ответил: {exc}', http_status.HTTP_502_BAD_GATEWAY)
        log.info('marketer post %s rewritten: %s by=%s', post.pk, current_schema_name(),
                 _author(request.user))
        return Response({'text': out['text'][:MAX_TEXT_LEN], 'model': out['model']})


class MarketerPostContextAPIView(APIView):
    """GET /api/v1/marketer/posts/{id}/context/ — снимок фактов, из которых написан пост."""
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

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
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

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
        # «О чём пост» из строки кабинета (16.10.2026): с ним пишем и при
        # выключенном еженедельном дайджесте — это ручной заказ поста.
        brief = str((request.data or {}).get('brief') or '').strip()[:1500]
        if not cfg.digest_enabled and not brief:
            return _error('marketer_disabled', 'дайджест выключен в настройках',
                          http_status.HTTP_409_CONFLICT)

        schema = current_schema_name()
        key = GENERATE_LOCK_KEY % schema
        if not cache.add(key, timezone.now().isoformat(), GENERATE_LOCK_SECONDS):
            return _error('already_generating',
                          'генерация уже идёт — черновик появится в ленте в течение минуты',
                          http_status.HTTP_409_CONFLICT,
                          retry_after=GENERATE_LOCK_SECONDS)

        # Кэш LoyalUP в памяти процесса: второй воркер web замка не видит.
        # Живая генерация в базе — настоящий замок (16.10.2026).
        busy = progress.running('post')
        if busy is not None:
            cache.delete(key)
            return _error('already_generating',
                          'генерация уже идёт — черновик появится в ленте в течение минуты',
                          http_status.HTTP_409_CONFLICT, job=progress.to_dict(busy))
        job = progress.start('post', author=_author(request.user))

        from apps.tenant.marketer.tasks import run_marketer_digest_for_tenant_task
        try:
            run_marketer_digest_for_tenant_task.delay(schema, brief, job_id=job.pk)
        except Exception as exc:
            # Очередь недоступна — блокировку снимаем, иначе кабинет будет
            # получать 409 пять минут на пустом месте.
            cache.delete(key)
            progress.finish(job, error=f'не удалось поставить задачу: {exc}')
            log.exception('marketer generate: очередь недоступна')
            return _error('queue_unavailable', f'не удалось поставить задачу: {exc}',
                          http_status.HTTP_503_SERVICE_UNAVAILABLE)
        log.info('marketer generate queued: %s by=%s', schema, _author(request.user))
        return Response({'queued': True, 'lock_seconds': GENERATE_LOCK_SECONDS,
                         'job': progress.to_dict(job)},
                        status=http_status.HTTP_202_ACCEPTED)


class MarketerPostPublishAPIView(APIView):
    """
    POST /api/v1/marketer/posts/{id}/publish/ {confirm: true}

    Публикация необратима, поэтому нужен `confirm`. Идемпотентна по состоянию:
    уже опубликованный пост отвечает `200` и вторым `wall.post` не уходит.
    Ошибку ВК отдаём как есть — `502 vk_error`.
    """
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

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
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

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


# ── Волна 3: отложенные посты, картинка, контент-план ──────────────────────────

class MarketerPostScheduleAPIView(APIView):
    """
    POST /api/v1/marketer/posts/{id}/schedule/ {scheduled_at: ISO | null}

    Время в будущем → «Запланирован», задача сама опубликует в это время.
    null → снять с плана, пост снова черновик. Запланировать = одобрить
    публикацию, поэтому только администратор сети.
    """
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

    @extend_schema(request=None, responses={200: _POST_OUT, 400: _ERR, 403: _ERR, 404: _ERR,
                                            409: _ERR}, tags=['v1'])
    def post(self, request, pk: int):
        if not _may_write(request.user):
            return _error('role_not_allowed', 'планировать может только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        post = _post_or_none(pk)
        if post is None:
            return _error('not_found', 'Пост не найден', http_status.HTTP_404_NOT_FOUND)
        if post.status not in EDITABLE_STATUSES:
            return _error('not_editable', f'пост в статусе «{post.get_status_display()}»',
                          http_status.HTTP_409_CONFLICT, status=post.status)
        raw = (request.data or {}).get('scheduled_at')
        if raw in (None, ''):
            post.scheduled_at = None
            if post.status == MarketerPostStatus.SCHEDULED:
                post.status = MarketerPostStatus.DRAFT
            post.save(update_fields=['scheduled_at', 'status', 'updated_at'])
            return Response(post_to_dict(post, _group_id(_settings_row())))
        when = parse_datetime(str(raw))
        if when is None:
            return _error('invalid_payload', 'scheduled_at: дата и время ISO',
                          http_status.HTTP_400_BAD_REQUEST)
        if timezone.is_naive(when):
            when = timezone.make_aware(when)
        if when <= timezone.now():
            return _error('invalid_payload', 'scheduled_at: время уже прошло',
                          http_status.HTTP_400_BAD_REQUEST)
        if not (post.text or '').strip():
            return _error('not_publishable', 'пустой текст — планировать нечего',
                          http_status.HTTP_409_CONFLICT)
        post.scheduled_at = when
        post.status = MarketerPostStatus.SCHEDULED
        post.error = ''
        post.save(update_fields=['scheduled_at', 'status', 'error', 'updated_at'])
        log.info('marketer post %s scheduled at %s by=%s', post.pk, when.isoformat(), _author(request.user))
        return Response(post_to_dict(post, _group_id(_settings_row())))


class MarketerPostImageAPIView(APIView):
    """
    POST   /api/v1/marketer/posts/{id}/image/  multipart image=<файл> — картинка к посту.
    DELETE /api/v1/marketer/posts/{id}/image/  — убрать картинку.
    """
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

    def _target(self, request, pk):
        if not _may_write(request.user):
            return None, _error('role_not_allowed', 'менять пост может только администратор сети',
                                http_status.HTTP_403_FORBIDDEN)
        post = _post_or_none(pk)
        if post is None:
            return None, _error('not_found', 'Пост не найден', http_status.HTTP_404_NOT_FOUND)
        if post.status not in EDITABLE_STATUSES:
            return None, _error('not_editable', f'пост в статусе «{post.get_status_display()}»',
                                http_status.HTTP_409_CONFLICT, status=post.status)
        return post, None

    @extend_schema(request=None, responses={200: _POST_OUT, 400: _ERR, 403: _ERR, 404: _ERR}, tags=['v1'])
    def post(self, request, pk: int):
        post, err = self._target(request, pk)
        if err:
            return err
        f = request.FILES.get('image')
        if f is None:
            return _error('invalid_payload', 'image: файл картинки', http_status.HTTP_400_BAD_REQUEST)
        if f.size > MAX_IMAGE_BYTES:
            return _error('invalid_payload', 'image: не больше 10 МБ', http_status.HTTP_400_BAD_REQUEST)
        if (getattr(f, 'content_type', '') or '').split('/')[0] != 'image':
            return _error('invalid_payload', 'image: нужна картинка (jpg, png, webp)',
                          http_status.HTTP_400_BAD_REQUEST)
        if post.image:
            post.image.delete(save=False)
        post.image = f
        post.image_source = 'upload'
        post.save(update_fields=['image', 'image_source', 'updated_at'])
        return Response(post_to_dict(post, _group_id(_settings_row())))

    @extend_schema(request=None, responses={200: _POST_OUT, 403: _ERR, 404: _ERR}, tags=['v1'])
    def delete(self, request, pk: int):
        post, err = self._target(request, pk)
        if err:
            return err
        if post.image:
            post.image.delete(save=False)
            post.image = None
            post.image_source = ''
            post.save(update_fields=['image', 'image_source', 'updated_at'])
        return Response(post_to_dict(post, _group_id(_settings_row())))


def plan_slots(start, days: int, per_week: int, hour: int) -> list:
    """Равномерно раскладывает per_week постов в неделю на days дней с start, в hour:00."""
    from datetime import datetime, time, timedelta
    step = 7 / per_week
    slots, i = [], 0
    while True:
        offset = int(round(i * step))
        if offset >= days:
            break
        day = start + timedelta(days=offset)
        slots.append(timezone.make_aware(datetime.combine(day, time(hour=hour))))
        i += 1
    return slots[:PLAN_MAX_POSTS]


class MarketerContentPlanAPIView(APIView):
    """
    POST /api/v1/marketer/posts/plan/ {start: ГГГГ-ММ-ДД, days: 7..31, per_week: 1..7, hour: 8..22,
                                      brief?: «о чём план», with_images?: bool}

    ИИ раскладывает темы по датам по базе знаний и пишет черновик на каждый
    слот. Черновики сами НЕ публикуются: человек правит и жмёт «Запланировать».
    Дни, где уже стоит пост, пропускаются (`skipped`). 202 {queued, slots, skipped, job}.
    """
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

    @extend_schema(request=None, responses={202: OpenApiResponse(description='{queued, slots}'),
                                            400: _ERR, 403: _ERR, 409: _ERR}, tags=['v1'])
    def post(self, request):
        if not _may_write(request.user):
            return _error('role_not_allowed', 'план составляет только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        cfg = _settings_row()
        if not cfg.is_enabled:
            return _error('marketer_disabled', 'AI-маркетолог выключен в настройках',
                          http_status.HTTP_409_CONFLICT)
        data = request.data or {}
        start = parse_date(str(data.get('start') or ''))
        try:
            days = int(data.get('days', 7))
            per_week = int(data.get('per_week', 3))
            hour = int(data.get('hour', 12))
        except (TypeError, ValueError):
            return _error('invalid_payload', 'days, per_week, hour — числа', http_status.HTTP_400_BAD_REQUEST)
        if start is None or start < timezone.localdate():
            return _error('invalid_payload', 'start: дата не раньше сегодня', http_status.HTTP_400_BAD_REQUEST)
        if not (1 <= days <= 31 and 1 <= per_week <= 7 and 8 <= hour <= 22):
            return _error('invalid_payload', 'days 1–31, per_week 1–7, hour 8–22',
                          http_status.HTTP_400_BAD_REQUEST)
        slots = [s for s in plan_slots(start, days, per_week, hour) if s > timezone.now()]
        if not slots:
            return _error('invalid_payload', 'в плане нет ни одного слота в будущем',
                          http_status.HTTP_400_BAD_REQUEST)
        from apps.tenant.marketer.tasks import build_content_plan_task, free_slots
        slots, taken = free_slots(slots, MarketerPost.objects.filter(
            scheduled_at__date__gte=start, scheduled_at__date__lte=start + timedelta(days=days)))
        if not slots:
            return _error('days_taken', 'на все дни плана уже стоят посты — второй пост на день не ставлю',
                          http_status.HTTP_409_CONFLICT, skipped=[s.isoformat() for s in taken])
        brief = str(data.get('brief') or '').strip()[:1500]
        with_images = str(data.get('with_images') or '').lower() in ('1', 'true', 'yes')
        schema = current_schema_name()
        key = GENERATE_LOCK_KEY % schema
        if not cache.add(key, timezone.now().isoformat(), GENERATE_LOCK_SECONDS):
            return _error('already_generating', 'генерация уже идёт', http_status.HTTP_409_CONFLICT,
                          retry_after=GENERATE_LOCK_SECONDS)
        busy = progress.running('plan')
        if busy is not None:
            cache.delete(key)
            return _error('already_generating', 'контент-план уже составляется',
                          http_status.HTTP_409_CONFLICT, job=progress.to_dict(busy))
        job = progress.start('plan', total=len(slots), author=_author(request.user))
        try:
            build_content_plan_task.delay(schema, [s.isoformat() for s in slots], _author(request.user),
                                          brief=brief, with_images=with_images, job_id=job.pk)
        except Exception as exc:
            cache.delete(key)
            progress.finish(job, error=f'не удалось поставить задачу: {exc}')
            return _error('queue_unavailable', f'не удалось поставить задачу: {exc}',
                          http_status.HTTP_503_SERVICE_UNAVAILABLE)
        log.info('marketer content plan queued: %s slots=%s skipped=%s by=%s', schema, len(slots),
                 len(taken), _author(request.user))
        return Response({'queued': True, 'slots': [s.isoformat() for s in slots],
                         'skipped': [s.isoformat() for s in taken], 'job': progress.to_dict(job)},
                        status=http_status.HTTP_202_ACCEPTED)



# ── Волна 4: калькулятор стоимости и картинка ИИ ──────────────────────────────

class MarketerEstimateAPIView(APIView):
    """GET /api/v1/marketer/posts/estimate/?count=&image=0|1&video_seconds= — сколько обойдётся,
    ДО генерации. ИИ не вызывается."""
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

    def get(self, request):
        from apps.tenant.marketer.costs import estimate
        q = request.query_params
        try:
            count = max(1, min(int(q.get('count') or 1), 62))
            video = max(0, min(int(q.get('video_seconds') or 0), 300))
        except ValueError:
            return _error('invalid_payload', 'count и video_seconds — числа', http_status.HTTP_400_BAD_REQUEST)
        image = str(q.get('image') or '').lower() in ('1', 'true', 'yes')
        return Response(estimate(count=count, image=image, video_seconds=video))


class MarketerPostGenerateImageAPIView(APIView):
    """POST /api/v1/marketer/posts/{id}/generate-image/ — ИИ рисует картинку к посту (202)."""
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

    def post(self, request, pk: int):
        if not _may_write(request.user):
            return _error('role_not_allowed', 'картинку заказывает администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        post = _post_or_none(pk)
        if post is None:
            return _error('not_found', 'Пост не найден', http_status.HTTP_404_NOT_FOUND)
        if post.status not in EDITABLE_STATUSES:
            return _error('not_editable', f'пост в статусе «{post.get_status_display()}»',
                          http_status.HTTP_409_CONFLICT, status=post.status)
        if not _settings_row().is_enabled:
            return _error('marketer_disabled', 'AI-маркетолог выключен в настройках',
                          http_status.HTTP_409_CONFLICT)
        schema = current_schema_name()
        key = f'marketer-image:{schema}:{pk}'
        if not cache.add(key, '1', 180):
            return _error('already_generating', 'картинка уже рисуется', http_status.HTTP_409_CONFLICT)
        busy = progress.running('image')
        if busy is not None and busy.post_id == post.pk:
            cache.delete(key)
            return _error('already_generating', 'картинка уже рисуется', http_status.HTTP_409_CONFLICT,
                          job=progress.to_dict(busy))
        job = progress.start('image', post=post, author=_author(request.user))
        from apps.tenant.marketer.tasks import generate_post_image_task
        try:
            generate_post_image_task.delay(schema, pk, job_id=job.pk)
        except Exception as exc:
            cache.delete(key)
            progress.finish(job, error=f'не удалось поставить задачу: {exc}')
            return _error('queue_unavailable', f'не удалось поставить задачу: {exc}',
                          http_status.HTTP_503_SERVICE_UNAVAILABLE)
        from apps.tenant.marketer.costs import image_rub
        return Response({'queued': True, 'cost_rub': float(image_rub()), 'job': progress.to_dict(job)},
                        status=http_status.HTTP_202_ACCEPTED)


# ── 16.10.2026: ход генерации, фотобанк ─────────────────────────────────────

class MarketerJobListAPIView(APIView):
    """GET /api/v1/marketer/jobs/?active=1 — идущие генерации и закончившиеся за 10 минут.

    Кабинет опрашивает раз в 2 секунды и рисует шкалу этапов.
    """
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

    def get(self, request):
        since = timezone.now() - timedelta(minutes=10)
        qs = MarketerJob.objects.filter(updated_at__gte=since) | MarketerJob.objects.filter(
            status=MarketerJob.Status.RUNNING, updated_at__gte=timezone.now() - progress.STALE_AFTER)
        return Response({'results': [progress.to_dict(j) for j in qs.distinct().order_by('-pk')[:20]]})


class MarketerJobDetailAPIView(APIView):
    """GET /api/v1/marketer/jobs/{id}/ — одна генерация."""
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

    def get(self, request, pk: int):
        job = MarketerJob.objects.filter(pk=pk).first()
        if job is None:
            return _error('not_found', 'Генерация не найдена', http_status.HTTP_404_NOT_FOUND)
        return Response(progress.to_dict(job))


PHOTO_KINDS = ('photo', 'style')


def photo_to_dict(ph) -> dict:
    return {
        'id': ph.pk,
        'image_url': ph.image.url if ph.image else '',
        'title': ph.title,
        'tags': ph.tags,
        'kind': ph.kind,
        'kind_label': ph.get_kind_display(),
        'is_active': ph.is_active,
        'used_count': ph.used_count,
        'created_by': ph.created_by,
        'created_at': _iso(ph.created_at),
    }


def _photo_fields(data) -> dict:
    out = {}
    if 'title' in data:
        out['title'] = str(data.get('title') or '').strip()[:200]
    if 'tags' in data:
        out['tags'] = str(data.get('tags') or '').strip()[:500]
    if 'kind' in data:
        kind = str(data.get('kind') or '')
        if kind not in PHOTO_KINDS:
            raise PayloadError('kind: photo или style')
        out['kind'] = kind
    if 'is_active' in data:
        out['is_active'] = str(data.get('is_active')).lower() in ('1', 'true', 'yes')
    return out


class MarketerPhotoListAPIView(APIView):
    """
    GET  /api/v1/marketer/photos/?kind=photo|style — фотобанк сети.
    POST /api/v1/marketer/photos/ multipart image=<файл>, title, tags, kind — добавить фото.
    """
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

    def get(self, request):
        qs = MarketerPhoto.objects.all()
        kind = request.query_params.get('kind')
        if kind in PHOTO_KINDS:
            qs = qs.filter(kind=kind)
        return Response({'results': [photo_to_dict(p) for p in qs.order_by('-pk')[:200]]})

    def post(self, request):
        if not _may_write(request.user):
            return _error('role_not_allowed', 'фотобанк пополняет администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        f = request.FILES.get('image')
        if f is None:
            return _error('invalid_payload', 'image: файл фото', http_status.HTTP_400_BAD_REQUEST)
        if f.size > MAX_IMAGE_BYTES:
            return _error('invalid_payload', 'image: не больше 10 МБ', http_status.HTTP_400_BAD_REQUEST)
        if (getattr(f, 'content_type', '') or '').split('/')[0] != 'image':
            return _error('invalid_payload', 'image: нужна картинка (jpg, png, webp)',
                          http_status.HTTP_400_BAD_REQUEST)
        try:
            fields = _photo_fields(request.data)
        except PayloadError as exc:
            return _error('invalid_payload', exc.detail, http_status.HTTP_400_BAD_REQUEST)
        fields.pop('is_active', None)
        photo = MarketerPhoto.objects.create(image=f, created_by=_author(request.user), **fields)
        return Response(photo_to_dict(photo), status=http_status.HTTP_201_CREATED)


class MarketerPhotoDetailAPIView(APIView):
    """PATCH/DELETE /api/v1/marketer/photos/{id}/ — подпись, метки, вид, «используется»; удалить."""
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

    def _get(self, request, pk):
        if not _may_write(request.user):
            return None, _error('role_not_allowed', 'фотобанк правит администратор сети',
                                http_status.HTTP_403_FORBIDDEN)
        photo = MarketerPhoto.objects.filter(pk=pk).first()
        if photo is None:
            return None, _error('not_found', 'Фото не найдено', http_status.HTTP_404_NOT_FOUND)
        return photo, None

    def patch(self, request, pk: int):
        photo, err = self._get(request, pk)
        if err:
            return err
        try:
            fields = _photo_fields(request.data or {})
        except PayloadError as exc:
            return _error('invalid_payload', exc.detail, http_status.HTTP_400_BAD_REQUEST)
        if not fields:
            return _error('invalid_payload', 'нечего менять', http_status.HTTP_400_BAD_REQUEST)
        for k, v in fields.items():
            setattr(photo, k, v)
        photo.save(update_fields=sorted(fields) + ['updated_at'])
        return Response(photo_to_dict(photo))

    def delete(self, request, pk: int):
        photo, err = self._get(request, pk)
        if err:
            return err
        # Посты держат свою копию файла — удаление из банка их не трогает.
        photo.image.delete(save=False)
        photo.delete()
        return Response(status=http_status.HTTP_204_NO_CONTENT)


class MarketerPostUsePhotoAPIView(APIView):
    """POST /api/v1/marketer/posts/{id}/use-photo/ {photo_id?} — фото из банка к посту.

    Без photo_id — подобрать по тексту поста; не нашлось — 404 no_match.
    """
    permission_classes = [IsAuthenticated, HasFeatureAccess.factory('broadcasts')]

    def post(self, request, pk: int):
        if not _may_write(request.user):
            return _error('role_not_allowed', 'картинку меняет администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        post = _post_or_none(pk)
        if post is None:
            return _error('not_found', 'Пост не найден', http_status.HTTP_404_NOT_FOUND)
        if post.status not in EDITABLE_STATUSES:
            return _error('not_editable', f'пост в статусе «{post.get_status_display()}»',
                          http_status.HTTP_409_CONFLICT, status=post.status)
        from apps.tenant.marketer import bank
        raw_id = (request.data or {}).get('photo_id')
        if raw_id not in (None, ''):
            photo = MarketerPhoto.objects.filter(pk=raw_id, kind='photo').first()
            if photo is None:
                return _error('not_found', 'Фото не найдено', http_status.HTTP_404_NOT_FOUND)
        else:
            photo = bank.best_match(post.text, post.visual,
                                    MarketerPhoto.objects.filter(kind='photo', is_active=True))
            if photo is None:
                return _error('no_match', 'в фотобанке нет фото к этому посту — добавьте или нарисуйте ИИ',
                              http_status.HTTP_404_NOT_FOUND)
        bank.copy_to_post(photo, post)
        return Response(post_to_dict(post, _group_id(_settings_row())))
