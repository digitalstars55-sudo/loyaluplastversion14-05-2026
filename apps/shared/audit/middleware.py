"""
AuditMiddleware — пишет в журнал каждое осмысленное действие участника.

Актора резолвим сами: сессионные (Django-админка) — из request.user; мобильные
(Bearer JWT) — декодируем токен (DRF аутентифицирует уже внутри вью, поэтому в
middleware request.user для API ещё AnonymousUser). Анонимные/сервисные/preflight
запросы и шумные поллинги не пишем — журнал остаётся «кто что сделал», без мусора.
"""
import logging

from .services import record_event

logger = logging.getLogger(__name__)

# Префиксы путей, которые НЕ логируем вовсе (статика, шум, частые поллинги).
_SKIP_PREFIXES = (
    '/static/', '/media/', '/favicon', '/admin/jsi18n', '/__',
    '/api/schema', '/api/docs', '/health', '/healthz', '/robots.txt',
)
# Точные GET-поллинги (мобилка дёргает часто) — логируем только их мутации.
_SKIP_GET_EXACT = (
    '/api/v1/support/chat/messages/',
)

# HTTP-метод → действие журнала.
_METHOD_ACTION = {
    'GET': 'view', 'HEAD': 'view',
    'POST': 'create', 'PUT': 'update', 'PATCH': 'update', 'DELETE': 'delete',
}


class AuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            self._maybe_log(request, response)
        except Exception:
            logger.exception('AuditMiddleware failed')
        return response

    def _maybe_log(self, request, response):
        method = request.method
        if method == 'OPTIONS':  # CORS preflight
            return
        path = request.path or ''
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return
        if method in ('GET', 'HEAD') and path in _SKIP_GET_EXACT:
            return

        actor, username, role = self._resolve_actor(request)
        # Логин/неудачный вход пишет сам LoginAPIView — здесь не дублируем.
        if path.endswith('/auth/login/'):
            return
        # Только если есть человек-актор (аноним/сервис не пишем).
        if actor is None and not username:
            return

        action = _METHOD_ACTION.get(method, 'view')
        record_event(
            action=action, request=request,
            actor=actor, actor_username=username, actor_role=role,
            status_code=getattr(response, 'status_code', None),
        )

    def _resolve_actor(self, request):
        """(user|None, username, role). Сессия → request.user; иначе → JWT."""
        user = getattr(request, 'user', None)
        if user is not None and getattr(user, 'is_authenticated', False):
            return user, getattr(user, 'username', '') or '', getattr(user, 'role', '') or ''

        hdr = request.META.get('HTTP_AUTHORIZATION', '')
        if hdr[:7].lower() == 'bearer ':
            token = hdr[7:].strip()
            try:
                from apps.shared.users.auth import decode_token
                payload = decode_token(token)
            except Exception:
                return None, '', ''  # сервис-ключ/протухший токен — не человек
            if payload.get('typ') != 'access':
                return None, '', ''
            from apps.shared.users.models import User
            u = User.objects.filter(pk=payload.get('sub')).first()
            if u is not None:
                return u, u.username, getattr(u, 'role', '') or ''
            return None, payload.get('username', '') or '', payload.get('role', '') or ''
        return None, '', ''
