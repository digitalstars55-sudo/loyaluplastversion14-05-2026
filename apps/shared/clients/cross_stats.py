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


# Пресеты периода для обзора (вчера/сегодня/7д/30д/всё время + произвольный диапазон).
OVERVIEW_PERIODS = [
    ('yesterday', 'Вчера'),
    ('today',     'Сегодня'),
    ('7d',        '7 дней'),
    ('30d',       '30 дней'),
    ('all',       'Всё время'),
]

# Начало «всего времени» — заведомо раньше старта платформы. Метрики по датам
# фильтруются по периоду, данных раньше нет; стоимость обслуживания всё равно
# считается только с даты начала тарифа (cost_for клампит по start_date тарифа).
_ALL_TIME_START = date(2020, 1, 1)


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
        'all':       (_ALL_TIME_START, today),
    }
    start, end = presets.get(preset, presets['30d'])
    return start, end, preset


def overview_period_qs(active_period: str, start: date, end: date) -> str:
    """Фрагмент query-string, сохраняющий выбранный период в ссылках/пагинации."""
    if active_period == 'custom':
        return f'start={start.isoformat()}&end={end.isoformat()}'
    return f'period={active_period}'


def _cached_pos_guests(start: date, end: date) -> int:
    """Сумма гостей POS за период ТОЛЬКО из кэша (без живого вызова кассы)."""
    from apps.tenant.analytics.models import POSGuestCache
    return POSGuestCache.objects.filter(
        date__gte=start, date__lte=end,
    ).aggregate(s=Sum('guest_count'))['s'] or 0


# Тональность → (подпись, css-класс) для ленты. SPAM в ленту не идёт.
_SENTIMENT_FEED = {
    'POSITIVE':           ('Позитивный', 'pos'),
    'NEGATIVE':           ('Негативный', 'neg'),
    'PARTIALLY_NEGATIVE': ('Частично негативный', 'neg'),
    'NEUTRAL':            ('Нейтральный', 'neu'),
    'WAITING':            ('Без оценки', 'neu'),
}

# Фильтры типа отзыва для страницы «Все отзывы».
SENTIMENT_FILTERS = [
    ('all',      'Все'),
    ('positive', 'Позитивные'),
    ('neutral',  'Нейтральные'),
    ('negative', 'Негативные'),
]


def _sentiment_in(sentiment_filter: str) -> list[str]:
    """Какие conversation.sentiment попадают под выбранный фильтр."""
    if sentiment_filter == 'positive':
        return ['POSITIVE']
    if sentiment_filter == 'negative':
        return ['NEGATIVE', 'PARTIALLY_NEGATIVE']
    if sentiment_filter == 'neutral':
        return ['NEUTRAL']
    return ['POSITIVE', 'NEGATIVE', 'PARTIALLY_NEGATIVE', 'NEUTRAL', 'WAITING']  # all (без SPAM)


def _company_logo(company: Company) -> str:
    """URL логотипа клиента из ClientConfig (public-схема) или '' если нет."""
    try:
        cfg = getattr(company, 'config', None)
        if cfg and cfg.logotype_image:
            return cfg.logotype_image.url
    except Exception:
        pass
    return ''


def _conv_review_link(conv, kind: str, fallback: str) -> str:
    """Ссылка отзыв-площадки для диалога: ссылки точки → фолбэк основной точки сети."""
    br = getattr(conv, 'branch', None)
    if br:
        val = br.review_link_yandex if kind == 'yandex' else br.review_link_2gis
        if val:
            return val
    return fallback or ''


def _tenant_row(company: Company, start: date, end: date) -> tuple[dict, list]:
    """Считает строку статистики + последние отзывы одного клиента (в его схеме)."""
    from apps.tenant.analytics.api.services import get_general_stats
    from apps.tenant.branch.models import TestimonialMessage

    logo = _company_logo(company)
    row = {
        'name': company.name, 'schema': company.schema_name, 'client_id': company.client_id,
        'domain': '', 'logo': logo, 'total_scans': 0, 'new_community': 0, 'new_newsletter': 0,
        'stories': 0, 'reviews': 0, 'scan_index': 0.0,
        'qr_scans': 0, 'pos_guests': 0, 'ok': False,
        # «Экономика клиента» (ТЗ)
        'gift_cost': 0.0, 'service_cost': 0.0, 'total_cost': 0.0,
        'sub_contacts': 0, 'unique_digitized': 0,
        'cost_per_contact': None, 'cost_per_unique': None,
    }
    feed = []
    try:
        with schema_context(company.schema_name):
            stats = get_general_stats(None, start, end, skip_slow=True)
            # Новые отзывы = диалоги с гостевым сообщением за период (НЕ admin-ответы,
            # НЕ «оживлённые» рассылкой по last_message_at — считаем по созданию сообщения).
            guest_msgs = (
                TestimonialMessage.objects
                .filter(created_at__date__gte=start, created_at__date__lte=end)
                .exclude(source=TestimonialMessage.Source.ADMIN_REPLY)
            )
            reviews = guest_msgs.values('conversation').distinct().count()
            pos = _cached_pos_guests(start, end)
            # Ссылки на отзыв-площадки (для кнопки «Вставить ссылки» в мобилке)
            from apps.tenant.branch.api.services import get_fallback_review_links
            fb_ya, fb_gis = get_fallback_review_links()
            # последние 6 отзывов клиента (с классифицированной тональностью)
            for m in guest_msgs.select_related('conversation__branch').order_by('-created_at')[:6]:
                meta = _SENTIMENT_FEED.get(m.conversation.sentiment)
                if not meta or not (m.text or '').strip():
                    continue
                feed.append({
                    'client': company.name,
                    'logo': logo,
                    'domain': '',  # проставляется в get_cross_tenant_overview
                    'conversation_id': m.conversation_id,
                    'text': m.text.strip(),
                    'created_at': m.created_at,
                    'sentiment_label': meta[0],
                    'sentiment_class': meta[1],
                    'review_link_yandex': _conv_review_link(m.conversation, 'yandex', fb_ya),
                    'review_link_2gis':   _conv_review_link(m.conversation, '2gis', fb_gis),
                })
        qr = stats.get('qr_scans', 0) or 0
        new_community  = stats.get('new_community_subscribers', 0) or 0
        new_newsletter = stats.get('new_newsletter_subscribers', 0) or 0
        # «Экономика клиента»: затраты на подарки (снимок) + стоимость обслуживания
        # (проренка по истории тарифов, считается в public-схеме — мы уже вышли из
        # schema_context тенанта). Производные цены — с защитой от деления на ноль.
        from apps.shared.clients.models import ServiceCostPeriod
        gift_cost    = round(float(stats.get('gift_cost_rub', 0) or 0), 2)
        service_cost = round(float(ServiceCostPeriod.cost_for(company, start, end)), 2)
        total_cost   = round(service_cost + gift_cost, 2)
        sub_contacts = new_community + new_newsletter
        unique_dig   = stats.get('unique_digitized_guests', 0) or 0
        row.update({
            'total_scans':   stats.get('total_scans', 0) or 0,
            'new_community': new_community,
            'new_newsletter': new_newsletter,
            'stories':       stats.get('vk_stories_publishers', 0) or 0,
            'reviews':       reviews,
            'qr_scans':      qr,
            'pos_guests':    pos,
            'scan_index':    round(qr / pos * 100, 1) if pos else 0.0,
            'gift_cost':     gift_cost,
            'service_cost':  service_cost,
            'total_cost':    total_cost,
            'sub_contacts':  sub_contacts,
            'unique_digitized': unique_dig,
            'cost_per_contact': round(total_cost / sub_contacts, 2) if sub_contacts else None,
            'cost_per_unique':  round(total_cost / unique_dig, 2) if unique_dig else None,
            'ok':            True,
        })
    except Exception:
        logger.exception('cross_stats: tenant %s failed', company.schema_name)
    return row, feed


def get_cross_tenant_overview(start: date, end: date) -> dict:
    """Сводка по всем активным клиентам за период: строки + тоталы."""
    companies = (
        Company.objects
        .exclude(schema_name='public')
        .filter(is_active=True)
        .select_related('config')
        .prefetch_related('domains')
        .order_by('name')
    )

    rows = []
    feed = []
    totals = {
        'total_scans': 0, 'new_community': 0, 'new_newsletter': 0,
        'stories': 0, 'reviews': 0, 'qr_scans': 0, 'pos_guests': 0,
        # «Экономика клиента» — суммарно по всем клиентам
        'gift_cost': 0.0, 'service_cost': 0.0, 'total_cost': 0.0,
        'sub_contacts': 0, 'unique_digitized': 0,
    }
    idx_qr = 0  # числитель индекса — только тенанты, у которых есть POS-данные
    for c in companies:
        row, c_feed = _tenant_row(c, start, end)
        primary = next((d for d in c.domains.all() if d.is_primary), None) \
            or next(iter(c.domains.all()), None)
        row['domain'] = primary.domain if primary else ''
        for fi in c_feed:
            fi['domain'] = row['domain']
        rows.append(row)
        feed.extend(c_feed)
        for k in ('total_scans', 'new_community', 'new_newsletter', 'stories', 'reviews',
                  'qr_scans', 'pos_guests', 'gift_cost', 'service_cost', 'total_cost',
                  'sub_contacts', 'unique_digitized'):
            totals[k] += row[k]
        if row['pos_guests']:
            idx_qr += row['qr_scans']

    totals['scan_index'] = (
        round(idx_qr / totals['pos_guests'] * 100, 1)
        if totals['pos_guests'] else 0.0
    )
    # Округление денег + производные цены по тоталам (защита от деления на ноль).
    totals['gift_cost']    = round(totals['gift_cost'], 2)
    totals['service_cost'] = round(totals['service_cost'], 2)
    totals['total_cost']   = round(totals['total_cost'], 2)
    totals['cost_per_contact'] = (
        round(totals['total_cost'] / totals['sub_contacts'], 2)
        if totals['sub_contacts'] else None
    )
    totals['cost_per_unique'] = (
        round(totals['total_cost'] / totals['unique_digitized'], 2)
        if totals['unique_digitized'] else None
    )
    # лента: все отзывы клиентов, новые сверху, топ-20
    feed.sort(key=lambda r: r['created_at'], reverse=True)
    feed = feed[:20]
    return {'rows': rows, 'totals': totals, 'client_count': len(rows), 'feed': feed}


def get_cross_tenant_reviews(start: date, end: date, sentiment_filter: str = 'all') -> list:
    """
    ПОЛНЫЙ список отзывов со всех клиентов за период (для страницы «Все отзывы»),
    отфильтрованный по типу тональности, новые сверху. Сразу по всем клиентам.

    Ограничение: до 500 на клиента (период и так бьёт объём) — для дашборда хватает.
    """
    from apps.tenant.branch.models import TestimonialMessage

    companies = (
        Company.objects
        .exclude(schema_name='public')
        .filter(is_active=True)
        .select_related('config')
        .prefetch_related('domains')
        .order_by('name')
    )
    sents = _sentiment_in(sentiment_filter)
    out = []
    for c in companies:
        logo = _company_logo(c)
        primary = next((d for d in c.domains.all() if d.is_primary), None) \
            or next(iter(c.domains.all()), None)
        domain = primary.domain if primary else ''
        try:
            with schema_context(c.schema_name):
                from apps.tenant.branch.api.services import get_fallback_review_links
                fb_ya, fb_gis = get_fallback_review_links()
                qs = (
                    TestimonialMessage.objects
                    .filter(created_at__date__gte=start, created_at__date__lte=end)
                    .exclude(source=TestimonialMessage.Source.ADMIN_REPLY)
                    .filter(conversation__sentiment__in=sents)
                    .select_related('conversation__branch')
                    .order_by('-created_at')[:500]
                )
                for m in qs:
                    if not (m.text or '').strip():
                        continue
                    meta = _SENTIMENT_FEED.get(m.conversation.sentiment, ('Без оценки', 'neu'))
                    out.append({
                        'client': c.name, 'logo': logo, 'domain': domain,
                        'conversation_id': m.conversation_id, 'text': m.text.strip(),
                        'created_at': m.created_at, 'rating': m.rating,
                        'sentiment_label': meta[0], 'sentiment_class': meta[1],
                        'review_link_yandex': _conv_review_link(m.conversation, 'yandex', fb_ya),
                        'review_link_2gis':   _conv_review_link(m.conversation, '2gis', fb_gis),
                    })
        except Exception:
            logger.exception('cross_stats reviews: tenant %s failed', c.schema_name)
    out.sort(key=lambda r: r['created_at'], reverse=True)
    return out
