"""
Рассылки для внешнего кабинета CheckUp: черновики, предпросмотр аудитории,
отправка и история запусков (контракт платформы, ручки №11 и №13).

ПОЧЕМУ ЛОГИКА ЗДЕСЬ ПРОДУБЛИРОВАНА
──────────────────────────────────
Живой путь отправки — analytics.api.views.SendSegmentBroadcastAPIView (им
пользуется мобильное приложение и веб). Он работает и трогать его нельзя:
любая правка ради внешнего кабинета рискует повторить ЧП «рассылка на 1
ушла 612». Поэтому шаги отправки (фильтр оцифрованных, дедуп по vk_id между
точками, Broadcast(SPECIFIC) + create_send на каждую точку, порог
синхронной отправки) здесь ПОВТОРЕНЫ, но все нижележащие функции
вызываются ТЕ ЖЕ: resolve_rf_cell_client_ids, create_send, run_broadcast,
run_broadcast_task, edit/delete_broadcast_send_in_vk.

ИНВАРИАНТЫ
──────────
1. Один .delay на запрос. run_broadcast внутри спит 0.05с (лимит VK ≤20
   msg/s). Параллельные celery-задачи на один токен сообщества = бан, поэтому
   все BroadcastSend одного запроса уходят ОДНИМ серийным таском.
2. expected_count обязателен. Отправка без цифры, которую видел человек,
   запрещена; расхождение сверяется guard.audience_changed (409).
3. RBAC по точкам. Видимость черновика — его branch_ids ⊆ доступных точек
   пользователя; видимость запуска — send.broadcast.branch_id среди
   доступных. Чужой объект = 404 (не раскрываем существование).
4. Картинки в v1 не поддержаны: поле в модели есть, API всегда отдаёт
   image: null и не принимает файл (multipart не нужен).
5. Существующие ручки и сервисы не меняются — модуль только добавляет.
"""
from __future__ import annotations

import json
import random
from datetime import date

from django.db import connection, transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.users.access import current_schema_name, effective_branch_ids
from apps.tenant.analytics.api.services import resolve_rf_cell_client_ids
from apps.tenant.analytics.models import RFSegment
from apps.tenant.branch.models import Branch, ClientBranch
from apps.tenant.senler.models import (
    AudienceType, Broadcast, BroadcastDraft, BroadcastSend,
    DraftMode, DraftStatus, GenderFilter, SendStatus,
)
from apps.tenant.senler.services import (
    create_send, delete_broadcast_send_in_vk, edit_broadcast_send_in_vk, run_broadcast,
)

from .guard import audience_changed
from .serializers import draft_to_dict, iso, segment_to_dict, send_to_dict

# Лимит VK на длину сообщения.
MAX_TEXT_LEN = 4096
# Порог синхронной отправки — как в образце (analytics.api.views:1409).
SYNC_RECIPIENT_LIMIT = 30
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


# ── Общие помощники ───────────────────────────────────────────────────────────

def _error(code: str, detail: str, status_code: int, **extra):
    """Единая форма ошибки для кабинета: {'code': ..., 'detail': ...}."""
    payload = {'code': code, 'detail': detail}
    payload.update(extra)
    return Response(payload, status=status_code)


def _allowed_branches(request):
    """None — доступны все точки; [-1] — доступа нет; иначе список PK."""
    return effective_branch_ids(request.user, current_schema_name(), None)


def _draft_visible(draft, allowed) -> bool:
    if allowed is None:
        return True
    ids = {int(x) for x in (draft.branch_ids or [])}
    return bool(ids) and ids.issubset({int(x) for x in allowed})


def _page_params(request):
    """limit/offset с потолком, чтобы кабинет не выкачал таблицу целиком."""
    try:
        limit = int(request.query_params.get('limit') or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        offset = int(request.query_params.get('offset') or 0)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    return limit, offset


def _confirmed(data) -> bool:
    value = data.get('confirm')
    if value is True:
        return True
    return str(value).strip().lower() in ('true', '1', 'yes')


def _username(request) -> str:
    return getattr(request.user, 'username', '') or 'api'


# ── Разбор тела запроса ───────────────────────────────────────────────────────

class PayloadError(ValueError):
    """Некорректное тело запроса — превращается в 400 invalid_payload."""


def _parse_branch_ids(raw) -> list[int]:
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    elif isinstance(raw, str):
        items = [x for x in raw.split(',') if x.strip()]
    else:
        raise PayloadError('branch_ids: ожидается список PK точек')
    try:
        ids = [int(x) for x in items]
    except (TypeError, ValueError):
        raise PayloadError('branch_ids: ожидается список целых PK точек')
    if not ids:
        raise PayloadError('branch_ids: нужно выбрать хотя бы одну точку')
    return ids


def _parse_variants(raw) -> list[dict]:
    if raw in (None, '', [], ()):
        return []
    parsed = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            raise PayloadError('variants: некорректный JSON')
    if not isinstance(parsed, list):
        raise PayloadError('variants: ожидается список')
    out: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise PayloadError('variants: каждый вариант — объект {percent, message_text}')
        try:
            percent = int(item.get('percent', 0))
        except (TypeError, ValueError):
            raise PayloadError('variants: percent должен быть числом')
        out.append({'percent': percent, 'message_text': (item.get('message_text') or '').strip()})
    return out


def _parse_int(raw, field: str):
    if raw in (None, ''):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise PayloadError(f'{field}: ожидается число')


def _parse_date(raw, field: str):
    if raw in (None, ''):
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        raise PayloadError(f'{field}: ожидается дата в формате ГГГГ-ММ-ДД')


def _validate_texts(message_text: str, variants: list[dict]) -> str | None:
    """
    Проверка текстов — как в образце (analytics.api.views:1157-1165).
    Возвращает текст ошибки или None.
    """
    if variants:
        for i, v in enumerate(variants):
            text = v.get('message_text') or ''
            if not text:
                return f'Вариант {i + 1}: текст не может быть пустым'
            if len(text) > MAX_TEXT_LEN:
                return f'Вариант {i + 1}: превышен лимит {MAX_TEXT_LEN} символов'
        if len(variants) > 1 and sum(v.get('percent', 0) for v in variants) != 100:
            return 'Сумма процентов вариантов должна быть 100'
        return None
    text = (message_text or '').strip()
    if not text:
        return 'Текст сообщения не может быть пустым'
    if len(text) > MAX_TEXT_LEN:
        return f'Превышен лимит {MAX_TEXT_LEN} символов'
    return None


def _parse_payload(data) -> dict:
    """
    Разбирает только ПЕРЕДАННЫЕ ключи (годится и для POST, и для PATCH).
    Бросает PayloadError. Значения mode/gender_filter приводятся к
    допустимым, как в образце (неизвестное → дефолт), остальное — строгое.
    """
    values: dict = {}
    if 'name' in data:
        values['name'] = str(data.get('name') or '').strip()[:200]
    if 'message_text' in data:
        values['message_text'] = str(data.get('message_text') or '').strip()
    if 'mode' in data:
        mode = data.get('mode')
        values['mode'] = mode if mode in (DraftMode.RESTAURANT, DraftMode.DELIVERY) else DraftMode.RESTAURANT
    if 'gender_filter' in data:
        gender = data.get('gender_filter')
        values['gender_filter'] = (
            gender if gender in (GenderFilter.ALL, GenderFilter.MALE, GenderFilter.FEMALE)
            else GenderFilter.ALL
        )
    if 'segment_id' in data:
        values['segment_id'] = _parse_int(data.get('segment_id'), 'segment_id')
    if 'r_score' in data:
        values['r_score'] = _parse_int(data.get('r_score'), 'r_score')
    if 'f_score' in data:
        values['f_score'] = _parse_int(data.get('f_score'), 'f_score')
    if 'start' in data:
        values['start'] = _parse_date(data.get('start'), 'start')
    if 'end' in data:
        values['end'] = _parse_date(data.get('end'), 'end')
    if 'branch_ids' in data:
        values['branch_ids'] = _parse_branch_ids(data.get('branch_ids'))
    if 'variants' in data:
        values['variants'] = _parse_variants(data.get('variants'))
    if 'status' in data:
        st = data.get('status')
        if st not in (DraftStatus.DRAFT, DraftStatus.ARCHIVED):
            raise PayloadError('status: допустимы только draft и archived')
        values['status'] = st
    return values


# ── Аудитория ─────────────────────────────────────────────────────────────────

def resolve_audience(draft) -> dict:
    """
    Кто получит рассылку по этому черновику — РОВНО тем же способом, каким
    считает отправка мобилки (analytics.api.views:1288-1320):

      • база: оцифрованные гости точки (не сотрудники, клиент активен, есть vk_id);
      • фильтр пола (если задан);
      • ячейка RF-матрицы, если у черновика есть сегмент: список guest.Client
        из resolve_rf_cell_client_ids (mode + r/f + период), а если ячейку
        определить нельзя (нестандартный код сегмента без r/f) — фолбэк на
        сохранённый FK сегмента с учётом режима;
      • дедуп по vk_id между точками: гость, привязанный к нескольким
        выбранным точкам, получает ОДНО сообщение — от первой по возрастанию pk.

    Без сегмента — «все оцифрованные» (r_score/f_score при этом не
    применяются, как и в образце).

    Возвращает {'total', 'by_branch': [{'branch_id','name','count','cb_ids'}]}:
    cb_ids — PK ClientBranch, из них отправка соберёт specific_clients.
    """
    branch_ids = [int(x) for x in (draft.branch_ids or [])]
    segment = draft.segment if draft.segment_id else None

    segment_client_ids = None
    if segment is not None:
        segment_client_ids = resolve_rf_cell_client_ids(
            branch_ids or None,
            mode=draft.mode,
            segment=segment,
            r_score=draft.r_score,
            f_score=draft.f_score,
            start_date=draft.start,
            end_date=draft.end,
        )

    by_branch: list[dict] = []
    seen_vk_ids: set[int] = set()
    total = 0
    branches = Branch.objects.filter(is_active=True, pk__in=branch_ids).order_by('pk')
    for branch in branches:
        cb_qs = ClientBranch.objects.filter(
            branch=branch,
            is_employee=False,
            client__is_active=True,
            client__vk_id__isnull=False,
        ).select_related('client')
        if draft.gender_filter != GenderFilter.ALL:
            cb_qs = cb_qs.filter(client__gender=draft.gender_filter)
        if segment_client_ids is not None:
            cb_qs = cb_qs.filter(client_id__in=segment_client_ids)
        elif segment is not None:
            seg_rel = ('client__rf_score_delivery__segment'
                       if draft.mode == DraftMode.DELIVERY else 'client__rf_score__segment')
            cb_qs = cb_qs.filter(**{seg_rel: segment})
        cb_qs = cb_qs.exclude(client__vk_id__in=seen_vk_ids)

        cb_list = list(cb_qs)
        seen_vk_ids.update(cb.client.vk_id for cb in cb_list if cb.client.vk_id)
        by_branch.append({
            'branch_id': branch.pk,
            'name':      branch.name,
            'count':     len(cb_list),
            'cb_ids':    [cb.pk for cb in cb_list],
        })
        total += len(cb_list)
    return {'total': total, 'by_branch': by_branch}


def _broadcast_label(draft, segment) -> str:
    """Название Broadcast в истории — как в образце, если кабинет не задал своё."""
    if draft.name:
        return draft.name[:255]
    if segment is not None:
        mode_label = ' (доставка)' if draft.mode == DraftMode.DELIVERY else ''
        emoji = getattr(segment, 'emoji', '') or ''
        return f'RF{mode_label}: {emoji} {segment.name} ({segment.code})'[:255]
    return 'Рассылка всем оцифрованным гостям'


# ── Черновики ─────────────────────────────────────────────────────────────────

class BroadcastDraftListCreateAPIView(APIView):
    """
    GET  /api/v1/broadcasts/ — список черновиков (limit/offset/total).
    POST /api/v1/broadcasts/ — создать черновик.

    RBAC: пользователь без ограничений видит все черновики; ограниченный —
    только те, чьи branch_ids целиком внутри его точек.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        allowed = _allowed_branches(request)
        limit, offset = _page_params(request)
        qs = BroadcastDraft.objects.all().select_related('segment')
        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)

        if allowed is None:
            total = qs.count()
            rows = list(qs[offset:offset + limit])
        else:
            # branch_ids — JSON-список, подмножество в БД не проверить:
            # черновиков мало, фильтруем в Python.
            visible = [d for d in qs if _draft_visible(d, allowed)]
            total = len(visible)
            rows = visible[offset:offset + limit]
        return Response({
            'total':   total,
            'limit':   limit,
            'offset':  offset,
            'results': [draft_to_dict(d) for d in rows],
        })

    def post(self, request):
        data = request.data or {}
        try:
            values = _parse_payload(data)
        except PayloadError as exc:
            return _error('invalid_payload', str(exc), http_status.HTTP_400_BAD_REQUEST)

        if not values.get('branch_ids'):
            return _error('invalid_payload', 'branch_ids: нужно выбрать хотя бы одну точку',
                          http_status.HTTP_400_BAD_REQUEST)

        err = _validate_texts(values.get('message_text', ''), values.get('variants', []))
        if err:
            return _error('invalid_payload', err, http_status.HTTP_400_BAD_REQUEST)

        allowed = _allowed_branches(request)
        if allowed is not None:
            if set(values['branch_ids']) - {int(x) for x in allowed}:
                return _error('branch_forbidden', 'Нет доступа к выбранным точкам',
                              http_status.HTTP_403_FORBIDDEN)

        segment = None
        if values.get('segment_id'):
            segment = RFSegment.objects.filter(pk=values['segment_id']).first()
            if segment is None:
                return _error('segment_not_found', 'RF-сегмент не найден',
                              http_status.HTTP_404_NOT_FOUND)

        draft = BroadcastDraft.objects.create(
            name=values.get('name', ''),
            message_text=values.get('message_text', ''),
            mode=values.get('mode', DraftMode.RESTAURANT),
            segment=segment,
            r_score=values.get('r_score'),
            f_score=values.get('f_score'),
            start=values.get('start'),
            end=values.get('end'),
            branch_ids=values['branch_ids'],
            gender_filter=values.get('gender_filter', GenderFilter.ALL),
            variants=values.get('variants', []),
            created_by=(getattr(request.user, 'username', '') or '')[:150],
        )
        return Response(draft_to_dict(draft), status=http_status.HTTP_201_CREATED)


def _load_draft(request, pk):
    """Черновик с учётом RBAC. None — нет или чужой (отвечаем 404)."""
    draft = BroadcastDraft.objects.filter(pk=pk).first()
    if draft is None:
        return None
    if not _draft_visible(draft, _allowed_branches(request)):
        return None
    return draft


class BroadcastDraftDetailAPIView(APIView):
    """
    GET/PATCH/DELETE /api/v1/broadcasts/{id}/

    Править и удалять можно только черновик в статусе draft: отправленный —
    это уже история, её меняют аварийными ручками sends/*.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        draft = _load_draft(request, pk)
        if draft is None:
            return _error('not_found', 'Черновик не найден', http_status.HTTP_404_NOT_FOUND)
        return Response(draft_to_dict(draft))

    def patch(self, request, pk):
        draft = _load_draft(request, pk)
        if draft is None:
            return _error('not_found', 'Черновик не найден', http_status.HTTP_404_NOT_FOUND)
        if draft.status != DraftStatus.DRAFT:
            return _error('not_a_draft', 'Изменять можно только черновик',
                          http_status.HTTP_409_CONFLICT)

        try:
            values = _parse_payload(request.data or {})
        except PayloadError as exc:
            return _error('invalid_payload', str(exc), http_status.HTTP_400_BAD_REQUEST)

        message_text = values.get('message_text', draft.message_text or '')
        variants = values.get('variants', list(draft.variants or []))
        err = _validate_texts(message_text, variants)
        if err:
            return _error('invalid_payload', err, http_status.HTTP_400_BAD_REQUEST)

        if 'branch_ids' in values:
            allowed = _allowed_branches(request)
            if allowed is not None and set(values['branch_ids']) - {int(x) for x in allowed}:
                return _error('branch_forbidden', 'Нет доступа к выбранным точкам',
                              http_status.HTTP_403_FORBIDDEN)

        if 'segment_id' in values:
            segment_id = values.pop('segment_id')
            if segment_id:
                segment = RFSegment.objects.filter(pk=segment_id).first()
                if segment is None:
                    return _error('segment_not_found', 'RF-сегмент не найден',
                                  http_status.HTTP_404_NOT_FOUND)
                draft.segment = segment
            else:
                draft.segment = None

        for field, value in values.items():
            setattr(draft, field, value)
        draft.save()
        return Response(draft_to_dict(draft))

    def delete(self, request, pk):
        draft = _load_draft(request, pk)
        if draft is None:
            return _error('not_found', 'Черновик не найден', http_status.HTTP_404_NOT_FOUND)
        if draft.status != DraftStatus.DRAFT:
            return _error('not_a_draft', 'Удалять можно только черновик',
                          http_status.HTTP_409_CONFLICT)
        draft.delete()
        return Response({'ok': True})


class BroadcastDraftPreviewAPIView(APIView):
    """
    GET /api/v1/broadcasts/{id}/preview/

    count — та самая цифра, которую кабинет обязан вернуть в expected_count
    при отправке. Считается ровно тем же кодом, что и аудитория отправки.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        draft = _load_draft(request, pk)
        if draft is None:
            return _error('not_found', 'Черновик не найден', http_status.HTTP_404_NOT_FOUND)
        audience = resolve_audience(draft)
        return Response({
            'count': audience['total'],
            'by_branch': [
                {'branch_id': b['branch_id'], 'name': b['name'], 'count': b['count']}
                for b in audience['by_branch']
            ],
            'mode':    draft.mode,
            'segment': segment_to_dict(draft.segment if draft.segment_id else None),
            'period':  {'start': iso(draft.start), 'end': iso(draft.end)},
        })


class BroadcastDraftSendAPIView(APIView):
    """
    POST /api/v1/broadcasts/{id}/send/  {expected_count, confirm: true}

    Порядок (нарушать нельзя):
      1) RBAC и статус черновика;
      2) expected_count + confirm обязательны;
      3) аудитория пересчитывается СВЕЖО;
      4) пусто → 400, разошлось с показанным → 409 (guard);
      5) Broadcast(SPECIFIC) + create_send на каждую точку (и на каждый
         A/B-вариант, если они заданы);
      6) черновик помечается sent ДО запуска — чтобы падение отправки не
         позволило отправить его второй раз;
      7) запуск: >30 получателей — ОДИН celery-таск на весь запрос,
         иначе синхронно.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        draft = _load_draft(request, pk)
        if draft is None:
            return _error('not_found', 'Черновик не найден', http_status.HTTP_404_NOT_FOUND)
        if draft.status == DraftStatus.SENT:
            return _error('already_sent', 'Этот черновик уже отправлен',
                          http_status.HTTP_409_CONFLICT)
        if draft.status != DraftStatus.DRAFT:
            return _error('not_a_draft', 'Отправлять можно только черновик',
                          http_status.HTTP_409_CONFLICT)

        data = request.data or {}
        if data.get('expected_count') in (None, ''):
            return _error(
                'expected_count_required',
                'Нужно передать expected_count — число получателей из предпросмотра',
                http_status.HTTP_400_BAD_REQUEST,
            )
        try:
            expected = int(data.get('expected_count'))
        except (TypeError, ValueError):
            return _error('invalid_payload', 'expected_count: ожидается число',
                          http_status.HTTP_400_BAD_REQUEST)
        if not _confirmed(data):
            return _error('confirm_required', 'Нужно подтверждение: confirm=true',
                          http_status.HTTP_400_BAD_REQUEST)

        variants = list(draft.variants or [])
        err = _validate_texts(draft.message_text or '', variants)
        if err:
            return _error('invalid_payload', err, http_status.HTTP_400_BAD_REQUEST)

        audience = resolve_audience(draft)
        actual = audience['total']
        if actual == 0:
            return _error('empty_audience', 'В выбранной аудитории сейчас нет получателей',
                          http_status.HTTP_400_BAD_REQUEST)
        if audience_changed(expected, actual):
            return _error(
                'audience_changed',
                f'Аудитория изменилась: показано {expected}, сейчас {actual} — подтвердите заново',
                http_status.HTTP_409_CONFLICT,
                expected=expected, actual=actual,
            )

        segment = draft.segment if draft.segment_id else None
        label = _broadcast_label(draft, segment)
        triggered_by = _username(request)
        pending: list[dict] = []

        with transaction.atomic():
            for row in audience['by_branch']:
                cb_ids = list(row['cb_ids'])
                if not cb_ids:
                    continue
                if len(variants) > 1:
                    pending.extend(self._create_variant_sends(
                        draft, row, cb_ids, variants, label, segment, triggered_by))
                else:
                    text = (variants[0].get('message_text') if variants
                            else (draft.message_text or ''))
                    send = self._create_send_for(
                        draft, row['branch_id'], cb_ids, label, text, segment, triggered_by)
                    pending.append({
                        'send': send, 'count': len(cb_ids),
                        'meta': {'branch_id': row['branch_id'], 'branch_name': row['name']},
                    })

            if not pending:
                return _error('empty_audience', 'В выбранной аудитории сейчас нет получателей',
                              http_status.HTTP_400_BAD_REQUEST)

            draft.status = DraftStatus.SENT
            draft.sent_at = timezone.now()
            draft.last_send = pending[0]['send']
            draft.save(update_fields=['status', 'sent_at', 'last_send', 'updated_at'])

        total_recipients = sum(p['count'] for p in pending)
        queued = total_recipients > SYNC_RECIPIENT_LIMIT

        if queued:
            # ОДИН серийный таск на весь запрос — см. инвариант №1.
            from apps.tenant.senler.tasks import run_broadcast_task
            run_broadcast_task.delay(connection.schema_name, [p['send'].id for p in pending])
            sends = [
                {'id': p['send'].id, **p['meta'], 'recipients': p['count'], 'status': 'queued'}
                for p in pending
            ]
        else:
            sends = []
            for p in pending:
                send = p['send']
                row = {'id': send.id, **p['meta'], 'recipients': p['count']}
                try:
                    run_broadcast(send)
                    send.refresh_from_db()
                    row.update({
                        'status': send.status,
                        'sent': send.sent_count,
                        'failed': send.failed_count,
                        'skipped': send.skipped_count,
                        'error': send.error_message or '',
                    })
                except Exception as exc:                     # noqa: BLE001
                    row.update({'status': 'failed', 'error': str(exc)})
                sends.append(row)

        return Response({
            'ok':               True,
            'queued':           queued,
            'sends':            sends,
            'total_recipients': total_recipients,
        })

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _create_send_for(draft, branch_id, cb_ids, label, text, segment, triggered_by):
        broadcast = Broadcast.objects.create(
            branch_id=branch_id,
            name=label,
            message_text=text,
            audience_type=AudienceType.SPECIFIC,
            gender_filter=draft.gender_filter,
        )
        broadcast.specific_clients.set(cb_ids)
        if segment is not None:
            # На SPECIFIC-рассылке сегмент на аудиторию НЕ влияет (services.py:56):
            # нужен только чтобы у сегмента обновилась дата последней кампании.
            broadcast.rf_segments.set([segment])
        return create_send(broadcast, triggered_by=triggered_by, trigger_type='manual')

    @classmethod
    def _create_variant_sends(cls, draft, row, cb_ids, variants, label, segment, triggered_by):
        """A/B/%-сплит внутри точки — как в образце (analytics.api.views:1341-1393)."""
        pool = list(cb_ids)
        random.shuffle(pool)
        n = len(pool)
        out: list[dict] = []
        cursor = 0
        for i, v in enumerate(variants):
            if i == len(variants) - 1:
                chunk = pool[cursor:]                       # хвост — последнему
            else:
                size = round(n * int(v.get('percent', 0)) / 100)
                chunk = pool[cursor:cursor + size]
                cursor += size
            variant_label = f'{label} — вариант {i + 1} ({v.get("percent", 0)}%)'[:255]
            send = cls._create_send_for(
                draft, row['branch_id'], chunk, variant_label,
                v.get('message_text') or '', segment, triggered_by,
            )
            out.append({
                'send': send, 'count': len(chunk),
                'meta': {
                    'branch_id': row['branch_id'],
                    'branch_name': row['name'],
                    'variant': f'#{i + 1} ({v.get("percent", 0)}%)',
                },
            })
        return out


# ── История запусков и аварийные действия ─────────────────────────────────────

def _load_send(request, pk):
    """Запуск с учётом RBAC по точке рассылки. None — нет или чужой."""
    send = BroadcastSend.objects.filter(pk=pk).first()
    if send is None:
        return None
    allowed = _allowed_branches(request)
    if allowed is None:
        return send
    branch_id = send.broadcast.branch_id if send.broadcast_id else None
    if branch_id is None or int(branch_id) not in {int(x) for x in allowed}:
        return None
    return send


class BroadcastSendListAPIView(APIView):
    """
    GET /api/v1/broadcasts/sends/ — история запусков.

    В отличие от старой истории кампаний, здесь RBAC: ограниченный
    пользователь видит только запуски по своим точкам. Авторассылки (запуск
    без Broadcast — по шаблону/правилу) в выдачу не попадают: у них нет
    точки, по которой можно проверить доступ.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        allowed = _allowed_branches(request)
        limit, offset = _page_params(request)

        qs = (BroadcastSend.objects
              .filter(broadcast__isnull=False)
              .select_related('broadcast', 'broadcast__branch'))
        if allowed is not None:
            qs = qs.filter(broadcast__branch_id__in=[int(x) for x in allowed])

        branch_id = request.query_params.get('branch_id')
        if branch_id:
            try:
                qs = qs.filter(broadcast__branch_id=int(branch_id))
            except (TypeError, ValueError):
                return _error('invalid_payload', 'branch_id: ожидается число',
                              http_status.HTTP_400_BAD_REQUEST)

        qs = qs.annotate(
            read_count=Count('recipients', filter=Q(recipients__read_at__isnull=False)),
        )
        total = qs.count()
        rows = list(qs.order_by('-created_at')[offset:offset + limit])
        return Response({
            'total':   total,
            'limit':   limit,
            'offset':  offset,
            'results': [send_to_dict(s) for s in rows],
        })


class BroadcastSendCancelAPIView(APIView):
    """
    POST /api/v1/broadcasts/sends/{id}/cancel/  {confirm: true}

    Отмена НЕ мгновенная: run_broadcast сверяется со статусом раз в 10
    сообщений (services.py:506-520) — поэтому в ответе честное примечание.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        send = _load_send(request, pk)
        if send is None:
            return _error('not_found', 'Запуск не найден', http_status.HTTP_404_NOT_FOUND)
        if not _confirmed(request.data or {}):
            return _error('confirm_required', 'Нужно подтверждение: confirm=true',
                          http_status.HTTP_400_BAD_REQUEST)
        if send.status not in (SendStatus.PENDING, SendStatus.RUNNING):
            return _error('not_cancellable',
                          'Отменить можно только рассылку в статусе «Ожидает» или «Отправляется»',
                          http_status.HTTP_409_CONFLICT)

        send.status = SendStatus.CANCELLED
        send.error_message = (send.error_message or '') + f'\n[Отменено через API: {_username(request)}]'
        send.save(update_fields=['status', 'error_message', 'updated_at'])
        return Response({
            'ok':     True,
            'status': send.status,
            'note':   'до 10 сообщений ещё может уйти',
        })


def _vk_manageable(send) -> bool:
    """Аварийные действия ВК допустимы только для завершённой отправки с доставками."""
    return send.status == SendStatus.DONE and send.sent_count > 0


class BroadcastSendEditInVKAPIView(APIView):
    """
    POST /api/v1/broadcasts/sends/{id}/edit-in-vk/  {confirm: true, message_text}

    Меняет текст уже доставленных сообщений. VK допускает правку 24 часа —
    более старые получатели попадут в skipped (решает сам сервис).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        send = _load_send(request, pk)
        if send is None:
            return _error('not_found', 'Запуск не найден', http_status.HTTP_404_NOT_FOUND)
        data = request.data or {}
        if not _confirmed(data):
            return _error('confirm_required', 'Нужно подтверждение: confirm=true',
                          http_status.HTTP_400_BAD_REQUEST)
        if not _vk_manageable(send):
            return _error('not_editable',
                          'Изменить текст можно только у завершённой рассылки с доставленными сообщениями',
                          http_status.HTTP_409_CONFLICT)
        text = (data.get('message_text') or '').strip()
        if not text:
            return _error('invalid_payload', 'Текст сообщения не может быть пустым',
                          http_status.HTTP_400_BAD_REQUEST)
        if len(text) > MAX_TEXT_LEN:
            return _error('invalid_payload', f'Превышен лимит {MAX_TEXT_LEN} символов',
                          http_status.HTTP_400_BAD_REQUEST)

        result = edit_broadcast_send_in_vk(send, text)
        return Response({'ok': True, **result})


class BroadcastSendDeleteInVKAPIView(APIView):
    """
    POST /api/v1/broadcasts/sends/{id}/delete-in-vk/  {confirm: true}

    Удаляет доставленные сообщения у гостей (окно ВК — 24 часа). Если
    удаление прошло без ошибок, запуск помечается отменённым — так же, как
    это делает админка (admin.py:436-440).
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        send = _load_send(request, pk)
        if send is None:
            return _error('not_found', 'Запуск не найден', http_status.HTTP_404_NOT_FOUND)
        if not _confirmed(request.data or {}):
            return _error('confirm_required', 'Нужно подтверждение: confirm=true',
                          http_status.HTTP_400_BAD_REQUEST)
        if not _vk_manageable(send):
            return _error('not_deletable',
                          'Удалить сообщения можно только у завершённой рассылки с доставленными сообщениями',
                          http_status.HTTP_409_CONFLICT)

        result = delete_broadcast_send_in_vk(send)
        if result.get('deleted', 0) > 0 and not result.get('errors'):
            send.status = SendStatus.CANCELLED
            send.error_message = (send.error_message or '') + '\n[Удалено из ВК через API]'
            send.save(update_fields=['status', 'error_message', 'updated_at'])
        return Response({'ok': True, 'status': send.status, **result})
