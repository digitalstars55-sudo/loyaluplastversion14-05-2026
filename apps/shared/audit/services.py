"""
Запись событий в журнал действий + утилиты.

record_event — единая best-effort точка записи (никогда не должна ронять
запрос/логин). Пишет в public-схему (модель AuditEvent — SHARED), что работает
и из тенант-контекста (search_path содержит public).
"""
import logging

logger = logging.getLogger(__name__)


def client_ip(request) -> str | None:
    """Реальный IP за nginx (X-Forwarded-For), иначе REMOTE_ADDR."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()[:45] or None
    return (request.META.get('REMOTE_ADDR') or None)


# Карта «префикс пути → человекочитаемый раздел» для колонки «Раздел/объект».
# Самый длинный совпавший префикс выигрывает.
_SECTION_MAP = [
    ('/api/v1/overview/',            'Сводная по клиентам'),
    ('/api/v1/analytics/contact-points', 'Точки контакта'),
    ('/api/v1/analytics/',          'Аналитика'),
    ('/api/v1/reviews/',            'Отзывы'),
    ('/api/v1/testimonial',         'Отзывы'),
    ('/api/v1/campaigns/',          'Рассылки'),
    ('/api/v1/broadcast',           'Рассылки'),
    ('/api/v1/senler',              'Рассылки'),
    ('/api/v1/catalog/',            'Каталог'),
    ('/api/v1/inventory/',          'Подарки'),
    ('/api/v1/quest',               'Квесты'),
    ('/api/v1/game',                'Игра'),
    ('/api/v1/guests',              'Гости'),
    ('/api/v1/birthday',            'Дни рождения'),
    ('/api/v1/support/chat',        'Чат с поддержкой'),
    ('/api/v1/branches',            'Точки'),
    ('/api/v1/me',                  'Профиль'),
    ('/api/v1/auth/',               'Авторизация'),
    ('/api/v1/',                    'API'),
    ('/superadmin/',                'Суперадмин-панель'),
    ('/admin/',                     'Админка клиента'),
]


def section_for_path(path: str) -> str:
    for prefix, label in _SECTION_MAP:
        if path.startswith(prefix):
            return label
    return ''


def record_event(
    *, action: str, request=None, actor=None,
    actor_username='', actor_role='', tenant_schema='', tenant_name='',
    target='', method='', path='', status_code=None, ip=None,
    user_agent='', meta=None,
):
    """Best-effort запись в журнал. Любая ошибка глушится."""
    try:
        from .models import AuditEvent

        if request is not None:
            method = method or request.method
            path = path or request.path
            ip = ip or client_ip(request)
            user_agent = user_agent or (request.META.get('HTTP_USER_AGENT', '') or '')[:300]
            if not tenant_schema:
                tnt = getattr(request, 'tenant', None)
                if tnt is not None:
                    tenant_schema = getattr(tnt, 'schema_name', '') or ''
                    if tenant_schema and tenant_schema != 'public':
                        tenant_name = tenant_name or getattr(tnt, 'name', '') or ''

        if actor is not None and getattr(actor, 'pk', None):
            actor_username = actor_username or getattr(actor, 'username', '') or ''
            actor_role = actor_role or getattr(actor, 'role', '') or ''

        if not target and path:
            target = section_for_path(path)

        AuditEvent.objects.create(
            actor=actor if (actor is not None and getattr(actor, 'pk', None)) else None,
            actor_username=actor_username[:150],
            actor_role=actor_role[:20],
            tenant_schema=tenant_schema[:63],
            tenant_name=tenant_name[:255],
            action=action,
            target=target[:255],
            method=(method or '')[:8],
            path=(path or '')[:512],
            status_code=status_code,
            ip=ip,
            user_agent=user_agent[:300],
            meta=meta or {},
        )
    except Exception:
        logger.exception('audit.record_event failed (action=%s path=%s)', action, path)
