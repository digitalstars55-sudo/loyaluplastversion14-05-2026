"""
Internal relay endpoint for CheckUp → LoyalUP support chat integration.

Receives manager replies from CheckUp side. Creates a SupportChatMessage
(sender=MANAGER) in the specified tenant schema and triggers a push
'chat_message' notification to that tenant's admins.

Auth: shared secret in header + internal network only (CheckUp lives in
same Docker host, traffic comes through docker bridge → private IPs).
Refuses requests with X-Forwarded-For (those came through nginx → public exposure).
Lives under public_urls (NOT tenant routing) — tenant is resolved from payload.
"""
import ipaddress
import json
import logging
import re

from django.conf import settings
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

log = logging.getLogger(__name__)

_TEXT_MAX = 4096
_SCHEMA_RE = re.compile(r'^[a-z][a-z0-9_]{0,62}$')  # safe schema name shape


def _is_internal_request(request) -> bool:
    """
    Accept only requests originating inside the Docker host:
      - loopback (127.0.0.1, ::1)  — from curl on host or unit tests
      - private ranges (10/8, 172.16/12, 192.168/16)  — docker bridge / podman
    Refuse if X-Forwarded-For is present (came through nginx → public).
    """
    if request.headers.get('X-Forwarded-For'):
        return False
    remote = request.META.get('REMOTE_ADDR', '')
    try:
        ip = ipaddress.ip_address(remote)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


@method_decorator(csrf_exempt, name='dispatch')
class InboundReplyView(View):
    """
    POST /api/v1/internal/support/inbound-reply/

    Body: {"tenant_schema": "...", "text": "...", "manager_name": "..."?}
    Auth: X-LoyalUP-Relay-Secret header + internal-network REMOTE_ADDR.
    """

    def post(self, request):
        # 1) internal-network only
        if not _is_internal_request(request):
            log.warning(
                'inbound-reply: rejected non-internal request remote=%s xff=%s',
                request.META.get('REMOTE_ADDR', ''),
                request.headers.get('X-Forwarded-For', ''),
            )
            return JsonResponse({'error': 'forbidden'}, status=403)

        # 2) shared secret
        provided = request.headers.get('X-LoyalUP-Relay-Secret', '')
        expected = getattr(settings, 'LOYALUP_RELAY_SECRET', '') or ''
        if not expected:
            log.error('inbound-reply: LOYALUP_RELAY_SECRET not configured')
            return JsonResponse({'error': 'not_configured'}, status=500)
        if provided != expected:
            log.warning('inbound-reply: bad secret')
            return JsonResponse({'error': 'forbidden'}, status=403)

        # 3) payload
        try:
            data = json.loads(request.body or b'{}')
        except (ValueError, json.JSONDecodeError):
            return JsonResponse({'error': 'invalid_json'}, status=400)

        tenant_schema = (data.get('tenant_schema') or '').strip()
        text          = (data.get('text') or '').strip()
        manager_name  = (data.get('manager_name') or 'Менеджер').strip()[:120]

        if not tenant_schema or not text:
            return JsonResponse({'error': 'tenant_schema and text required'}, status=400)
        if not _SCHEMA_RE.match(tenant_schema):
            return JsonResponse({'error': 'invalid tenant_schema'}, status=400)
        if len(text) > _TEXT_MAX:
            return JsonResponse({'error': f'text too long (max {_TEXT_MAX})'}, status=400)

        # 4) resolve tenant
        from django_tenants.utils import schema_context, get_tenant_model
        Tenant = get_tenant_model()
        try:
            tenant = Tenant.objects.get(schema_name=tenant_schema)
        except Tenant.DoesNotExist:
            return JsonResponse({'error': f'unknown tenant {tenant_schema}'}, status=404)

        # 5) write message inside tenant schema
        message_pk = None
        with schema_context(tenant_schema):
            from apps.tenant.branch.models import SupportChatMessage

            m = SupportChatMessage.objects.create(
                sender=SupportChatMessage.Sender.MANAGER,
                text=text,
                # author_id stays NULL — this is from external CheckUp manager
            )
            message_pk = m.pk

        # 6) push to tenant admins (best-effort, never raises)
        push_result = {'sent': 0, 'reason': 'unknown'}
        try:
            from apps.tenant.analytics.auto_reply import push_chat_message
            push_result = push_chat_message(
                schema_name=tenant_schema,
                tenant_name=tenant.name,
                message_id=message_pk,
                manager_name=manager_name,
                preview=text[:120],
            )
        except Exception:
            log.exception('inbound-reply: push_chat_message failed tenant=%s msg=%s', tenant_schema, message_pk)
            push_result = {'sent': 0, 'reason': 'push_failed'}

        return JsonResponse(
            {'ok': True, 'message_id': message_pk, 'push': push_result},
            status=201,
        )