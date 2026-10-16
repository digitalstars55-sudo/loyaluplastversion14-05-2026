"""
Счётчик переходов по ссылкам на Яндекс Карты / 2ГИС из сообщений ВК (16.10.2026).

Владелец CheckUp: «переходы на карты — подвязать к ссылкам, которые отправляем в
личку». Кнопка ведёт на `https://<домен сети>/r/map/<подпись>/`: переход
записывается (`ReviewLinkClick`), гость уходит на карту 302-м. Подпись — Django
signing: подменить адрес назначения нельзя. Домен сети не найден — ссылка остаётся
прямой, как раньше (счётчик просто не растёт).
"""
from __future__ import annotations

import logging

from django.core import signing
from django.db import connection
from django.http import Http404, HttpResponseRedirect
from django.views.decorators.http import require_GET

log = logging.getLogger(__name__)

SALT = 'lu-map-link'
PLATFORMS = ('yandex', '2gis')


def _домен_сети() -> str:
    try:
        from django_tenants.utils import get_tenant_domain_model
        Domain = get_tenant_domain_model()
        qs = Domain.objects.filter(tenant__schema_name=connection.schema_name)
        d = qs.filter(is_primary=True).first() or qs.first()
        return d.domain if d else ''
    except Exception:                                    # noqa: BLE001 — ссылка важнее счётчика
        log.warning('map_links: домен сети не найден', exc_info=True)
        return ''


def tracked(conv, platform: str, target: str) -> str:
    """Ссылка через наш домен; без домена — прямая `target`."""
    target = (target or '').strip()
    if not target or platform not in PLATFORMS:
        return target
    домен = _домен_сети()
    if not домен:
        return target
    token = signing.dumps({'c': getattr(conv, 'pk', None), 'p': platform, 'u': target},
                          salt=SALT, compress=True)
    return f'https://{домен}/r/map/{token}/'


@require_GET
def redirect_view(request, token: str):
    try:
        data = signing.loads(token, salt=SALT)
    except signing.BadSignature:
        raise Http404
    target = str(data.get('u') or '')
    if not target.startswith(('https://', 'http://')):
        raise Http404
    try:
        from .models import ReviewLinkClick, TestimonialConversation
        conv = TestimonialConversation.objects.filter(pk=data.get('c')).first()
        ReviewLinkClick.objects.create(
            conversation=conv,
            branch_id=(conv.branch_id or getattr(conv, 'inferred_branch_id', None)) if conv else None,
            vk_id=str(getattr(conv, 'vk_sender_id', '') or '')[:32] if conv else '',
            platform=str(data.get('p') or '')[:8],
        )
    except Exception:                                    # noqa: BLE001 — гость должен дойти до карты
        log.warning('map_links: переход не записан', exc_info=True)
    return HttpResponseRedirect(target)


def counts(start, end, branch_ids=None) -> dict:
    """Переходы за период: уникальные гости и клики, по площадкам и всего."""
    from django.db.models import Count
    from .models import ReviewLinkClick
    qs = ReviewLinkClick.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
    if branch_ids:
        qs = qs.filter(branch_id__in=branch_ids)
    out = {}
    for p in PLATFORMS:
        part = qs.filter(platform=p)
        out[p] = {'clicks': part.count(),
                  'guests': part.exclude(vk_id='').values('vk_id').distinct().count()}
    out['total'] = {'clicks': qs.count(),
                    'guests': qs.exclude(vk_id='').values('vk_id').distinct().count()}
    return out
