"""
Knowledge Core — сборщик фактов тенанта для генерации контента.

Возвращает плоский JSON-совместимый dict: только числа/строки из живых
данных лояльности. ИИ строит пост ИСКЛЮЧИТЕЛЬНО из этих фактов + ручных
заметок владельца (MarketerSettings.extra_facts) — ничего не выдумывает.

Вызывается внутри схемы тенанта (schema_context / request.tenant).
⚠️ Под celery schema_context connection.tenant — FakeTenant без pk, поэтому
ClientConfig добывается через senler.engine._tenant_client_config
(резолв Company по schema_name).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

logger = logging.getLogger(__name__)

REVIEW_QUOTE_LIMIT = 3          # сколько цитат позитивных отзывов брать
REVIEW_QUOTE_MAX_LEN = 220      # обрезка длинных отзывов


def _mini_app_link() -> str:
    app_id = getattr(settings, 'VK_MINI_APP_ID', '')
    return f'https://vk.com/app{app_id}' if app_id else ''


def collect_knowledge(days: int = 7) -> dict:
    """Собирает снимок знаний за окно `days` (по умолчанию неделя)."""
    from apps.tenant.branch.models import (
        Branch, ClientBranch, ClientBranchVisit, TestimonialConversation,
        TestimonialMessage,
    )
    from apps.tenant.inventory.models import InventoryItem
    from apps.tenant.senler.engine import _tenant_client_config

    now = timezone.now()
    since = now - timedelta(days=days)

    data: dict = {
        'period_days': days,
        'period_end': timezone.localtime(now).strftime('%d.%m.%Y'),
        'mini_app_link': _mini_app_link(),
    }

    # ── Бренд ────────────────────────────────────────────────────────────────
    try:
        cfg = _tenant_client_config()
        if cfg is not None:
            data['brand_name'] = (
                getattr(cfg, 'vk_group_name', '') or cfg.company.name
            )
    except Exception:
        logger.exception('knowledge: brand block failed')

    # ── Точки ────────────────────────────────────────────────────────────────
    try:
        data['branches'] = list(
            Branch.objects.filter(is_active=True)
            .order_by('branch_id')
            .values_list('name', flat=True)
        )
    except Exception:
        logger.exception('knowledge: branches block failed')

    # ── Статистика недели ────────────────────────────────────────────────────
    try:
        data['week'] = {
            'new_guests': ClientBranch.objects.filter(created_at__gte=since).count(),
            'visits': ClientBranchVisit.objects.filter(visited_at__gte=since).count(),
            'gifts_issued': InventoryItem.objects.filter(created_at__gte=since).count(),
            'gifts_redeemed': InventoryItem.objects.filter(used_at__gte=since).count(),
        }
    except Exception:
        logger.exception('knowledge: week stats block failed')

    # ── Позитивные отзывы (анонимные цитаты) ─────────────────────────────────
    try:
        positive = TestimonialConversation.objects.filter(
            sentiment=TestimonialConversation.Sentiment.POSITIVE,
            updated_at__gte=since,
        )
        data['positive_reviews_count'] = positive.count()
        quotes = []
        for conv in positive.order_by('-updated_at')[:REVIEW_QUOTE_LIMIT * 2]:
            msg = (
                conv.messages.exclude(text='')
                .filter(source__in=[
                    TestimonialMessage.Source.APP,
                    TestimonialMessage.Source.VK_MESSAGE,
                ])
                .order_by('-id')
                .first()
            )
            if msg and len(msg.text.strip()) >= 15:
                quotes.append(msg.text.strip()[:REVIEW_QUOTE_MAX_LEN])
            if len(quotes) >= REVIEW_QUOTE_LIMIT:
                break
        data['positive_review_quotes'] = quotes
    except Exception:
        logger.exception('knowledge: reviews block failed')

    # ── RF-сводка (обезличенная) ─────────────────────────────────────────────
    try:
        from apps.tenant.analytics.models import GuestRFScore
        seg_counts = (
            GuestRFScore.objects.filter(segment__isnull=False)
            .values('segment__code', 'segment__name')
            .annotate(n=Count('id'))
            .order_by('-n')[:6]
        )
        data['rf_segments'] = [
            {'code': s['segment__code'], 'name': s['segment__name'], 'guests': s['n']}
            for s in seg_counts
        ]
    except Exception:
        logger.exception('knowledge: rf block failed')

    # ── Каталог наград (что гостя ждёт в приложении) ─────────────────────────
    try:
        from apps.tenant.inventory.models import RewardCatalogItem
        items = RewardCatalogItem.objects.filter(is_active=True)[:10]
        names = []
        for it in items:
            name = it.name or (it.product.name if it.product_id and it.product else '')
            if name:
                names.append(name)
        data['reward_catalog'] = names
    except Exception:
        logger.exception('knowledge: reward catalog block failed')

    return data
