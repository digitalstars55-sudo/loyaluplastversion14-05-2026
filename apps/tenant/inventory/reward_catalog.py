"""
Каталог наград: выбор позиции и учёт выдач.

Реализует ТЗ RF-авторассылок §15.4 «Выбор подарка» в той части, которая
относится к САМОМУ каталогу:

    Подарок активен и доступен в периоде   → допустить к выбору
    Категория не выше разрешённой сценарием → допустить
    Себестоимость выше лимита              → исключить
    Исчерпан лимит выдач                   → исключить
    Подарок был среди трёх последних       → исключить (через exclude_ids)
    Нет ни одной доступной позиции         → вернуть None, НЕ падать
    Несколько позиций доступны             → взвешенный случайный выбор

Гостевые проверки (активный подарок на руках, баланс 3000+, антиспам,
история выдач конкретному гостю) сюда НЕ входят — их делает вызывающий код
RF-наград/авторассылок, а сюда передаёт готовый exclude_ids. Модуль ничего
не выдаёт гостям: он только выбирает позицию и атомарно считает выдачи.
"""

import logging
import random

from django.db.models import F, Q
from django.utils import timezone

from .models import RewardCatalogItem, RewardTier

logger = logging.getLogger(__name__)

__all__ = [
    'available_items',
    'pick_reward',
    'register_issue',
    'release_issue',
]


def _tier_list(tier):
    """
    Нормализует тир к списку строк.

    Сценарии ТЗ бывают как одиночными («G1 на 7 дней»), так и составными
    («G1/G2 на 10 дней», «G2/G3 на 14 дней»), поэтому принимаем и строку,
    и любую последовательность.
    """
    if tier is None:
        return []
    if isinstance(tier, str):
        candidates = [tier]
    else:
        candidates = list(tier)

    allowed = set(RewardTier.values)
    out = []
    for value in candidates:
        value = getattr(value, 'value', value)
        if value in allowed and value not in out:
            out.append(value)
    return out


def available_items(tier, branch=None, *, exclude_ids=None, max_cost_price=None, at=None):
    """
    Позиции каталога, которые сейчас разрешено выдать.

    tier          — 'G1' | 'G2' | 'G3' либо список тиров (составные сценарии).
    branch        — точка, для которой выбираем. Позиция подходит, если она
                    привязана к этой точке ИЛИ является сетевой (branch пуст).
                    Если branch не передан — берутся ТОЛЬКО сетевые позиции,
                    потому что точечную нельзя выдать «в никуда».
    exclude_ids   — id позиций, которые нельзя предлагать (например три
                    последних подарка гостя из ТЗ §15.4).
    max_cost_price — потолок себестоимости для бюджета кампании.
    at            — момент времени для проверки периода доступности.

    Возвращает список объектов (каталог маленький — 7-10 позиций по ТЗ).
    """
    tiers = _tier_list(tier)
    if not tiers:
        return []

    at = at or timezone.now()

    qs = RewardCatalogItem.objects.filter(
        tier__in=tiers,
        is_active=True,
        is_archived=False,
        available_for_rfm=True,
    ).select_related('product', 'branch')

    # Точечные позиции нужной точки + сетевые. Без точки — только сетевые.
    if branch is not None:
        qs = qs.filter(Q(branch=branch) | Q(branch__isnull=True))
    else:
        qs = qs.filter(branch__isnull=True)

    # Период доступности.
    qs = qs.filter(Q(available_from__isnull=True) | Q(available_from__lte=at))
    qs = qs.filter(Q(available_to__isnull=True) | Q(available_to__gte=at))

    # Лимит выдач не исчерпан (сравнение делает БД — счётчик всегда свежий).
    qs = qs.filter(
        Q(activation_limit__isnull=True) | Q(issued_count__lt=F('activation_limit'))
    )

    if exclude_ids:
        qs = qs.exclude(pk__in=list(exclude_ids))

    items = list(qs)

    # Себестоимость может наследоваться от подарка, поэтому фильтруем в Python.
    if max_cost_price is not None:
        items = [i for i in items if i.effective_cost_price <= max_cost_price]

    return items


def pick_reward(tier, branch=None, *, exclude_ids=None, max_cost_price=None, at=None, rng=None):
    """
    Взвешенный случайный выбор одной позиции каталога.

    Возвращает RewardCatalogItem или None, если подходящих позиций нет
    (вызывающий код в этом случае уходит на сообщение без подарка и пишет
    причину NO_GIFT_AVAILABLE — исключение отсюда не летит).

    Ничего не резервирует: чтобы занять место в лимите, вызовите
    register_issue() на возвращённой позиции.
    """
    items = available_items(
        tier,
        branch,
        exclude_ids=exclude_ids,
        max_cost_price=max_cost_price,
        at=at,
    )
    if not items:
        return None
    if len(items) == 1:
        return items[0]

    chooser = rng or random
    weights = [item.weight for item in items]

    # Все веса нулевые — вырождаемся в равномерный выбор, чтобы позиции
    # с весом 0 всё же работали как «запасные», а не ломали выбор.
    if not any(weights):
        return chooser.choice(items)

    return chooser.choices(items, weights=weights, k=1)[0]


def register_issue(item) -> bool:
    """
    Атомарно занять одну выдачу у позиции каталога.

    Проверка лимита и инкремент делаются ОДНИМ UPDATE с условием в WHERE,
    поэтому параллельные выдачи не могут пробить activation_limit и блокировки
    строк не нужны. Значение лимита берётся из БД, а не из объекта в памяти —
    объект мог устареть.

    Возвращает True, если выдача зачтена, и False, если лимит уже исчерпан
    (или позиция удалена). Вызывающий код при False должен уйти на сообщение
    без подарка.
    """
    if item is None or not getattr(item, 'pk', None):
        return False

    updated = (
        RewardCatalogItem.objects
        .filter(pk=item.pk)
        .filter(Q(activation_limit__isnull=True) | Q(issued_count__lt=F('activation_limit')))
        .update(issued_count=F('issued_count') + 1)
    )
    if not updated:
        logger.info(
            'reward_catalog.register_issue: лимит исчерпан (item=%s)', item.pk,
        )
        return False

    # Держим объект в памяти согласованным с БД (best-effort).
    try:
        item.refresh_from_db(fields=['issued_count'])
    except Exception:
        item.issued_count = (item.issued_count or 0) + 1
    return True


def release_issue(item) -> bool:
    """
    Вернуть одну выдачу обратно в лимит — компенсация по ТЗ §15.6: если после
    register_issue() отправка сообщения или назначение подарка сорвались,
    место в лимите не должно сгореть.

    Возвращает True, если счётчик уменьшен. Ниже нуля не опускается.
    """
    if item is None or not getattr(item, 'pk', None):
        return False

    updated = (
        RewardCatalogItem.objects
        .filter(pk=item.pk, issued_count__gt=0)
        .update(issued_count=F('issued_count') - 1)
    )
    if not updated:
        return False

    try:
        item.refresh_from_db(fields=['issued_count'])
    except Exception:
        item.issued_count = max((item.issued_count or 0) - 1, 0)
    return True
