"""
Кросс-клиентский обзор (для суперадмина): сводная статистика сразу по ВСЕМ
подключённым тенантам за выбранный период.

Механика django-tenants: каждый клиент живёт в своей Postgres-схеме, поэтому
обходим всех активных Company и для каждого внутри schema_context считаем тот же
get_general_stats, что и обычная панель аналитики. Суммируем в тоталы + строим
строку на клиента.

Производительность: get_general_stats(skip_slow=True) — БЕЗ живых POS-вызовов
(иначе N тенантов × внешний API = долгая страница). «Индекс сканирования»
считаем из кэша POSGuestCache (его наполняет почасовой Celery-таск) — без сети.
Каждый тенант обёрнут в try/except: сбой одного не роняет всю страницу.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from django.db.models import Count, Sum
from django_tenants.utils import schema_context

from apps.shared.clients.models import Company

logger = logging.getLogger(__name__)


# Пресеты периода для обзора (вчера/сегодня/7д/30д + произвольный диапазон).
OVERVIEW_PERIODS = [
    ('yesterday', 'Вчера'),
    ('today',     'Сегодня'),
    ('7d',        '7 дней'),
    ('30d',       '30 дней'),
]


def parse_overview_period(request) -> tuple[date, date, str]:
    today = date.today()
    s, e = request.GET.get('start'), request.GET.get('end')
    if s and e:
        try:
            return date.fromisoformat(s), date.fromisoformat(e), 'custom'
        except ValueError:
            pass
    preset = request.GET.get('period', '30d')
    yday = today - timedelta(days=1)
    presets = {
        'today':     (today, today),
        'yesterday': (yday, yday),
        '7d':        (today - timedelta(days=6), today),
        '30d':       (today - timedelta(days=29), today),
    }
    start, end = presets.get(preset, presets['30d'])
    return start, end, preset


def _cached_pos_guests(start: date, end: date) -> int:
    """Сумма гостей POS за период ТОЛЬКО из кэша (без живого вызова кассы)."""
    from apps.tenant.analytics.models import POSGuestCache
    return POSGuestCache.objects.filter(
        date__gte=start, date__lte=end,
    ).aggregate(s=Sum('guest_count'))['s'] or 0


def _tenant_row(company: Company, start: date, end: date) -> dict:
    """Считает строку статистики одного клиента (внутри его схемы)."""
    from apps.tenant.analytics.api.services import get_general_stats
    from apps.tenant.branch.models import TestimonialConversation

    row = {
        'name': company.name, 'schema': company.schema_name, 'client_id': company.client_id,
        'total_scans': 0, 'new_community': 0, 'new_newsletter': 0,
        'stories': 0, 'reviews': 0, 'scan_index': 0.0,
        'qr_scans': 0, 'pos_guests': 0, 'ok': False,
    }
    try:
        with schema_context(company.schema_name):
            stats = get_general_stats(None, start, end, skip_slow=True)
            reviews = (
                TestimonialConversation.objects
                .filter(last_message_at__date__gte=start, last_message_at__date__lte=end)
                .count()
            )
            pos = _cached_pos_guests(start, end)
        qr = stats.get('qr_scans', 0) or 0
        row.update({
            'total_scans':   stats.get('total_scans', 0) or 0,
            'new_community': stats.get('new_community_subscribers', 0) or 0,
            'new_newsletter': stats.get('new_newsletter_subscribers', 0) or 0,
            'stories':       stats.get('vk_stories_publishers', 0) or 0,
            'reviews':       reviews,
            'qr_scans':      qr,
            'pos_guests':    pos,
            'scan_index':    round(qr / pos * 100, 1) if pos else 0.0,
            'ok':            True,
        })
    except Exception:
        logger.exception('cross_stats: tenant %s failed', company.schema_name)
    return row


def get_cross_tenant_overview(start: date, end: date) -> dict:
    """Сводка по всем активным клиентам за период: строки + тоталы."""
    companies = (
        Company.objects
        .exclude(schema_name='public')
        .filter(is_active=True)
        .order_by('name')
    )

    rows = []
    totals = {
        'total_scans': 0, 'new_community': 0, 'new_newsletter': 0,
        'stories': 0, 'reviews': 0, 'qr_scans': 0, 'pos_guests': 0,
    }
    for c in companies:
        row = _tenant_row(c, start, end)
        rows.append(row)
        for k in ('total_scans', 'new_community', 'new_newsletter', 'stories', 'reviews', 'qr_scans', 'pos_guests'):
            totals[k] += row[k]

    totals['scan_index'] = (
        round(totals['qr_scans'] / totals['pos_guests'] * 100, 1)
        if totals['pos_guests'] else 0.0
    )
    return {'rows': rows, 'totals': totals, 'client_count': len(rows)}
