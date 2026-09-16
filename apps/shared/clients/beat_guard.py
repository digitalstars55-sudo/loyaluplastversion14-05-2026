"""
Гард фоновых задач по оплате и активности сети (волна 0, 16.09.2026).

────────────────────────────────────────────────────────────────────────
Зачем
────────────────────────────────────────────────────────────────────────
Все диспетчеры beat обходили сети одинаково: `TenantModel.objects
.exclude(schema_name='public')` — без оглядки на `Company.is_active` и
`Company.paid_until`. Гость такую сеть уже не видит (`CompanyExpired` в
`clients/api/services.py`), а рассылки, ИИ-разбор отзывов, RF-пересчёт и
сводки кодов дня продолжали идти: неоплатившая сеть жгла токены и писала
своим гостям от нашего имени. На 12.09 так жил asap-murom: выключен,
оплата истекла 22.06.

────────────────────────────────────────────────────────────────────────
Как включается
────────────────────────────────────────────────────────────────────────
`settings.BEAT_TENANT_GUARD` (переменная окружения `BEAT_TENANT_GUARD`):
  • `off` — как раньше, обходим всех кроме public.
  • `log` — обходим всех, но пишем в лог, кого бы пропустили. Режим по
    умолчанию: прод неотличим от прежнего, а по логам видно, что именно
    отрежет `on`.
  • `on`  — пропускаем `is_active=False` и сети, у которых `paid_until`
    старше `BEAT_PAID_UNTIL_GRACE_DAYS` дней (по умолчанию 7: чтобы сеть
    не отключалась из-за того, что владелец не успел проставить оплату
    в админке в день платежа). `paid_until` пустой — сеть считается
    оплаченной, её не трогаем.

Диспетчеры зовут `beat_tenants()` вместо прямого exclude — точка одна,
менять поведение можно в одном месте.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from django.conf import settings
from django.db.models import Q, QuerySet
from django.utils import timezone

logger = logging.getLogger(__name__)

MODES = ('off', 'log', 'on')
DEFAULT_GRACE_DAYS = 7


def guard_mode() -> str:
    """Режим гарда из настроек; незнакомое значение = `log` (безопасный)."""
    mode = str(getattr(settings, 'BEAT_TENANT_GUARD', 'log') or 'log').strip().lower()
    return mode if mode in MODES else 'log'


def grace_days() -> int:
    try:
        days = int(getattr(settings, 'BEAT_PAID_UNTIL_GRACE_DAYS', DEFAULT_GRACE_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_GRACE_DAYS
    return max(days, 0)


def skip_reason(is_active: bool, paid_until: date | None, today: date,
                grace: int = DEFAULT_GRACE_DAYS) -> str:
    """Почему сеть надо пропустить; пустая строка — сеть в порядке.

    Чистая функция, без БД и настроек — на ней держатся тесты.
    """
    if not is_active:
        return 'inactive'
    if paid_until is None:
        return ''
    if paid_until + timedelta(days=grace) < today:
        return f'paid_until={paid_until.isoformat()} (+{grace}d grace)'
    return ''


def _paid_cutoff(today: date, grace: int) -> date:
    """Последняя дата paid_until, при которой сеть ещё обслуживается."""
    return today - timedelta(days=grace)


def beat_tenants() -> QuerySet:
    """Сети, которые должен обойти диспетчер beat (без public).

    Возвращает QuerySet — вызывающие дописывают свои `select_related`.
    В режиме `log` дополнительно логирует кандидатов на пропуск; сам
    список при этом полный, как раньше.
    """
    from django_tenants.utils import get_tenant_model

    qs = get_tenant_model().objects.exclude(schema_name='public')
    mode = guard_mode()
    if mode == 'off':
        return qs

    today = timezone.localdate()
    grace = grace_days()
    cutoff = _paid_cutoff(today, grace)
    blocked = Q(is_active=False) | Q(paid_until__isnull=False, paid_until__lt=cutoff)

    if mode == 'on':
        return qs.exclude(blocked)

    # mode == 'log': ничего не режем, только показываем, кого бы отрезали.
    try:
        would_skip = list(qs.filter(blocked).values_list('schema_name', 'is_active', 'paid_until'))
    except Exception:  # БД молчит — гард не важнее самой задачи
        would_skip = []
    if would_skip:
        details = ', '.join(
            f'{schema}: {skip_reason(active, paid, today, grace)}'
            for schema, active, paid in would_skip
        )
        logger.warning('beat_guard[log]: режим on пропустил бы %d сетей → %s', len(would_skip), details)
    return qs
