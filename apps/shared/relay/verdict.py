"""
Вердикт по жалобе: CheckUp → LoyalUP (контракт платформы, раздел 5.2).

Негативный отзыв уезжает в реестр жалоб CheckUp (checkup_complaints.py), там
его разбирает менеджер. До этой ручки обратного канала не было: в ленте
LoyalUP жалоба выглядела нерешённой, хотя в CheckUp по ней уже выдали
компенсацию. Теперь CheckUp присылает статус и вердикт, а LoyalUP показывает
их в карточке отзыва (веб, мобилка, API).

Что нарочно НЕ делаем: не пишем сообщение гостю и не создаём строку в треде.
Ответ гостю — обычная ручка reply под токеном сотрудника, чтобы автор ответа
был известен и сработали пуши/автоответы как всегда. Вердикт — служебные
поля переписки (TestimonialConversation.checkup_*), не переписка.

Сеть определяется так же, как в обмене токена: если ручку вызвали на хосте
сети (Host: <schema>.levelupapp.ru) — по хосту; если на публичном
(Host: levelupapp.ru) — по `tenant_schema` в теле. Работают оба варианта.
"""
from __future__ import annotations

import json
import logging
import re

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django_tenants.utils import get_tenant_model, schema_context

from .views import _is_internal_request

log = logging.getLogger(__name__)

STATUS_IN_PROGRESS = 'in_progress'
STATUS_RESOLVED = 'resolved'
STATUS_REJECTED = 'rejected'
STATUSES = (STATUS_IN_PROGRESS, STATUS_RESOLVED, STATUS_REJECTED)
CLOSED = (STATUS_RESOLVED, STATUS_REJECTED)

MAX_VERDICT = 2000
MAX_MANAGER = 120
MAX_COMPLAINT_ID = 40
_SCHEMA_RE = re.compile(r'^[a-z][a-z0-9_-]{0,62}$')
_COMPLAINT_RE = re.compile(r'^(\d{1,12})(?:-[A-Za-z0-9_.-]{1,26})?$')


class VerdictError(Exception):
    def __init__(self, status: int, code: str, detail: str = '', **extra):
        super().__init__(f'{status} {code}: {detail}')
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra

    def as_dict(self) -> dict:
        body = {'code': self.code, 'detail': self.detail}
        body.update(self.extra)
        return body


def parse_complaint_id(raw) -> int:
    """`complaint_id` из жалобы — `<id переписки>-<корзина дедупа>`; нам нужен id переписки."""
    text = str(raw or '').strip()
    m = _COMPLAINT_RE.match(text)
    if not m or len(text) > MAX_COMPLAINT_ID:
        raise VerdictError(400, 'invalid_complaint_id', 'complaint_id — это <id переписки>-<корзина>, как в жалобе')
    return int(m.group(1))


def validate_payload(data) -> dict:
    if not isinstance(data, dict):
        raise VerdictError(422, 'invalid_payload', 'тело должно быть JSON-объектом')
    conversation_id = parse_complaint_id(data.get('complaint_id'))
    status = str(data.get('status') or '').strip().lower()
    if status not in STATUSES:
        raise VerdictError(422, 'invalid_status', 'status: in_progress | resolved | rejected')
    verdict = str(data.get('verdict') or '').strip()
    if len(verdict) > MAX_VERDICT:
        raise VerdictError(422, 'invalid_payload', f'verdict: не длиннее {MAX_VERDICT} символов')
    manager = str(data.get('manager_name') or '').strip()[:MAX_MANAGER]
    resolved_at = None
    raw_at = data.get('resolved_at')
    if raw_at:
        resolved_at = parse_datetime(str(raw_at))
        if resolved_at is None:
            raise VerdictError(422, 'invalid_payload', 'resolved_at: ISO-8601')
        if timezone.is_naive(resolved_at):
            resolved_at = timezone.make_aware(resolved_at, timezone.get_default_timezone())
    schema = str(data.get('tenant_schema') or '').strip().lower()
    if schema and (not _SCHEMA_RE.match(schema) or schema == 'public'):
        raise VerdictError(422, 'invalid_payload', 'tenant_schema: имя сети LoyalUP')
    return {
        'complaint_id': str(data.get('complaint_id')).strip(),
        'conversation_id': conversation_id,
        'status': status,
        'verdict': verdict,
        'manager': manager,
        'resolved_at': resolved_at,
        'force': bool(data.get('force')),
        'tenant_schema': schema,
    }


def resolve_schema(request, payload_schema: str) -> str:
    """Хост сети решает; на публичном хосте — tenant_schema из тела."""
    current = getattr(connection, 'schema_name', 'public') or 'public'
    if current != 'public':
        return current
    if not payload_schema:
        raise VerdictError(422, 'invalid_payload', 'на публичном хосте нужен tenant_schema')
    Tenant = get_tenant_model()
    tenant = Tenant.objects.filter(schema_name=payload_schema).first()
    if tenant is None or not tenant.is_active:
        raise VerdictError(404, 'tenant_not_found', f'сети {payload_schema} нет или она выключена')
    return payload_schema


def apply_verdict(schema: str, p: dict) -> dict:
    """Записать вердикт в переписку. 404 — нет такой в этой сети; 409 — уже закрыта иначе (без force)."""
    from apps.tenant.branch.models import TestimonialConversation

    with schema_context(schema):
        conv = TestimonialConversation.objects.filter(pk=p['conversation_id']).first()
        if conv is None:
            raise VerdictError(404, 'complaint_not_found', f'переписки {p["conversation_id"]} в сети {schema} нет')
        previous = conv.checkup_status or ''
        if previous in CLOSED and previous != p['status'] and not p['force']:
            raise VerdictError(409, 'already_closed', 'жалоба уже закрыта с другим статусом; чтобы переписать — force: true',
                               current_status=previous)
        now = timezone.now()
        conv.checkup_status = p['status']
        conv.checkup_verdict = p['verdict']
        conv.checkup_manager = p['manager']
        conv.checkup_complaint_id = p['complaint_id'][:MAX_COMPLAINT_ID]
        conv.checkup_verdict_at = now
        if p['status'] in CLOSED:
            conv.checkup_resolved_at = p['resolved_at'] or now
        else:
            conv.checkup_resolved_at = None
        conv.save(update_fields=['checkup_status', 'checkup_verdict', 'checkup_manager', 'checkup_complaint_id',
                                 'checkup_verdict_at', 'checkup_resolved_at'])
    log.info('verdict: tenant=%s conv=%s %s → %s by %r', schema, p['conversation_id'], previous or '-', p['status'], p['manager'])
    return {'ok': True, 'tenant_schema': schema, 'conversation_id': p['conversation_id'],
            'status': p['status'], 'previous_status': previous}


@method_decorator(csrf_exempt, name='dispatch')
class ComplaintVerdictView(View):
    """POST /api/v1/internal/complaints/verdict/ — заголовок X-LoyalUP-Relay-Secret (тот же, что у жалоб)."""
    http_method_names = ['post']

    def post(self, request):
        if not _is_internal_request(request):
            log.warning('verdict: отклонён внешний запрос remote=%s xff=%s',
                        request.META.get('REMOTE_ADDR', ''), request.headers.get('X-Forwarded-For', ''))
            return JsonResponse({'code': 'forbidden', 'detail': 'только с внутреннего адреса сервера'}, status=403)

        expected = getattr(settings, 'LOYALUP_RELAY_SECRET', '') or ''
        if not expected:
            log.error('verdict: LOYALUP_RELAY_SECRET не задан')
            return JsonResponse({'code': 'not_configured', 'detail': 'секрет релея не задан'}, status=500)
        provided = request.headers.get('X-LoyalUP-Relay-Secret', '')
        if not provided or provided != expected:
            log.warning('verdict: неверный секрет remote=%s', request.META.get('REMOTE_ADDR', ''))
            return JsonResponse({'code': 'forbidden', 'detail': 'неверный X-LoyalUP-Relay-Secret'}, status=403)

        try:
            data = json.loads(request.body or b'{}')
        except (ValueError, json.JSONDecodeError):
            return JsonResponse({'code': 'invalid_json', 'detail': 'тело не JSON'}, status=400)

        try:
            payload = validate_payload(data)
            schema = resolve_schema(request, payload['tenant_schema'])
            result = apply_verdict(schema, payload)
        except VerdictError as e:
            log.info('verdict: отказ %s %s (%s)', e.status, e.code, e.detail)
            return JsonResponse(e.as_dict(), status=e.status)
        return JsonResponse(result)
