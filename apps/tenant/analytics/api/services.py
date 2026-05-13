"""
Analytics services — atomic functions for each dashboard metric.

All functions accept:
  branch_ids: list[int] | None  — None means all branches in this tenant
  start_date / end_date: datetime.date  — inclusive range

Convention: every function does exactly one thing and is independently testable.
"""
from __future__ import annotations

from datetime import date
from collections import defaultdict

from django.db.models import Count, Min, Q
from django.db.models.functions import TruncDate
from apps.tenant.analytics.models import POSGuestCache


# ── Helpers ───────────────────────────────────────────────────────────────────

def _branch_filter(qs, branch_ids: list[int] | None, field: str = 'branch__in'):
    if branch_ids:
        return qs.filter(**{field: branch_ids})
    return qs


# ── Metric 1: QR scans ────────────────────────────────────────────────────────

def get_qr_scan_count(branch_ids: list[int] | None, start_date: date, end_date: date) -> int:
    """Count unique guests who scanned QR and interacted with the app in any way."""
    from django.db.models import Exists, OuterRef
    from apps.tenant.branch.models import (
        ClientBranch, ClientBranchVisit, CoinTransaction,
        TransactionType, TransactionSource, TestimonialMessage,
    )
    from apps.tenant.game.models import ClientAttempt
    from apps.tenant.delivery.models import Delivery

    visited_ids = ClientBranchVisit.objects.filter(
        visited_at__date__gte=start_date,
        visited_at__date__lte=end_date,
    )
    visited_ids = _branch_filter(visited_ids, branch_ids, 'client__branch__in').values('client_id')
    return ClientBranch.objects.filter(pk__in=visited_ids).filter(
        Q(vk_status__community_via_app=True)                        # Подписались на сообщество
        | Q(vk_status__newsletter_via_app=True)                     # Подписались на рассылку
        | Q(vk_status__is_story_uploaded=True)                      # Опубликовали сторис
        | Exists(ClientAttempt.objects.filter(                       # Сыграли в игру
            client=OuterRef('pk'),
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
        ))
        | Exists(TestimonialMessage.objects.filter(                  # Оставили отзыв
            conversation__client=OuterRef('pk'),
            source=TestimonialMessage.Source.APP,
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
        ))
        | Exists(Delivery.objects.filter(                            # Активировали код доставки
            activated_by=OuterRef('pk'),
            activated_at__date__gte=start_date,
            activated_at__date__lte=end_date,
        ))
        | Exists(CoinTransaction.objects.filter(                     # Потратили монеты в магазине
            client=OuterRef('pk'),
            type=TransactionType.EXPENSE,
            source=TransactionSource.SHOP,
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
        ))
    ).count()


# ── Metric 2: Total VK subscribers (all-time, no period) ─────────────────────

def get_total_vk_subscribers(branch_ids: list[int] | None) -> int:
    """Unique guests who subscribed to community OR newsletter via app (ever)."""
    from apps.tenant.branch.models import ClientVKStatus

    qs = ClientVKStatus.objects.filter(
        Q(community_via_app=True) | Q(newsletter_via_app=True)
    )
    return _branch_filter(qs, branch_ids, 'client__branch__in').count()


# ── Metric 3: New group+newsletter members who got their first gift ───────────

def get_new_group_with_first_gift(
    branch_ids: list[int] | None, start_date: date, end_date: date
) -> int:
    """
    Guests who in the period:
      1. Subscribed to community OR newsletter via app
      2. Received their very first SuperPrize (acquired_from=GAME)
    """
    from apps.tenant.branch.models import ClientVKStatus
    from apps.tenant.inventory.models import SuperPrizeEntry, SuperPrizeTrigger

    # Step 1: guests who subscribed via app in the period
    vk_qs = ClientVKStatus.objects.filter(
        Q(community_via_app=True, community_joined_at__date__gte=start_date,
          community_joined_at__date__lte=end_date) |
        Q(newsletter_via_app=True, newsletter_joined_at__date__gte=start_date,
          newsletter_joined_at__date__lte=end_date)
    )
    vk_qs = _branch_filter(vk_qs, branch_ids, 'client__branch__in')
    subscribed_cb_ids = set(vk_qs.values_list('client_id', flat=True))

    if not subscribed_cb_ids:
        return 0

    # Step 2: from those, who got their FIRST game prize in the period
    first_prizes = (
        SuperPrizeEntry.objects
        .filter(
            acquired_from=SuperPrizeTrigger.GAME,
            client_branch__in=subscribed_cb_ids,
        )
        .values('client_branch')
        .annotate(first_at=Min('created_at'))
        .filter(
            first_at__date__gte=start_date,
            first_at__date__lte=end_date,
        )
    )
    return first_prizes.count()


# ── Metric 4: Repeat game players ─────────────────────────────────────────────

def get_repeat_game_players(
    branch_ids: list[int] | None, start_date: date, end_date: date
) -> int:
    """Guests who played on at least 2 distinct calendar days in the period."""
    from apps.tenant.game.models import ClientAttempt

    qs = ClientAttempt.objects.filter(
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
    )
    qs = _branch_filter(qs, branch_ids, 'client__branch__in')

    # Collect distinct (client_id, date) pairs
    pairs = (
        qs.annotate(play_date=TruncDate('created_at'))
        .values_list('client_id', 'play_date')
        .distinct()
    )

    client_days: dict[int, set] = defaultdict(set)
    for client_id, play_date in pairs:
        client_days[client_id].add(play_date)

    return sum(1 for days in client_days.values() if len(days) >= 2)


# ── Metric 5: Coin purchasers ─────────────────────────────────────────────────

def get_coin_purchasers(
    branch_ids: list[int] | None, start_date: date, end_date: date
) -> int:
    """Unique guests who spent coins in the shop at least once in the period."""
    from apps.tenant.branch.models import CoinTransaction, TransactionType, TransactionSource

    qs = CoinTransaction.objects.filter(
        type=TransactionType.EXPENSE,
        source=TransactionSource.SHOP,
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
    )
    qs = _branch_filter(qs, branch_ids, 'client__branch__in')
    return qs.values('client').distinct().count()


# ── Metric 6: New VK community subscribers via app ───────────────────────────

def get_new_community_subscribers(
    branch_ids: list[int] | None, start_date: date, end_date: date
) -> int:
    """Guests who subscribed to VK community via app in the period (by community_joined_at)."""
    from apps.tenant.branch.models import ClientVKStatus

    qs = ClientVKStatus.objects.filter(
        community_via_app=True,
        community_joined_at__date__gte=start_date,
        community_joined_at__date__lte=end_date,
    )
    return _branch_filter(qs, branch_ids, 'client__branch__in').count()


# ── Metric 7: New VK newsletter subscribers via app ──────────────────────────

def get_new_newsletter_subscribers(
    branch_ids: list[int] | None, start_date: date, end_date: date
) -> int:
    """Guests who subscribed to VK newsletter via app in the period (by newsletter_joined_at)."""
    from apps.tenant.branch.models import ClientVKStatus

    qs = ClientVKStatus.objects.filter(
        newsletter_via_app=True,
        newsletter_joined_at__date__gte=start_date,
        newsletter_joined_at__date__lte=end_date,
    )
    return _branch_filter(qs, branch_ids, 'client__branch__in').count()


# ── Metric 7b: First gift receivers ──────────────────────────────────────────

def get_first_gift_receivers(
    branch_ids: list[int] | None, start_date: date, end_date: date
) -> int:
    """
    Unique guests whose very first InventoryItem (any acquisition source) was
    created within the period — i.e. they got their first-ever gift in this range.
    """
    from apps.tenant.inventory.models import InventoryItem

    qs = InventoryItem.objects.all()
    qs = _branch_filter(qs, branch_ids, 'client_branch__branch__in')

    first_items = (
        qs.values('client_branch')
        .annotate(first_at=Min('created_at'))
        .filter(
            first_at__date__gte=start_date,
            first_at__date__lte=end_date,
        )
    )
    return first_items.count()


# ── Metric 7c: Gift activators ───────────────────────────────────────────────

def get_gift_activators(
    branch_ids: list[int] | None, start_date: date, end_date: date
) -> int:
    """Unique guests who activated at least one InventoryItem in the period."""
    from apps.tenant.inventory.models import InventoryItem

    qs = InventoryItem.objects.filter(
        activated_at__date__gte=start_date,
        activated_at__date__lte=end_date,
    )
    qs = _branch_filter(qs, branch_ids, 'client_branch__branch__in')
    return qs.values('client_branch').distinct().count()


# ── Metric 8: Birthday greetings sent ────────────────────────────────────────

def get_birthday_greetings_sent(
    branch_ids: list[int] | None, start_date: date, end_date: date
) -> int:
    """
    Unique guests who received at least one birthday auto-broadcast in the period.
    Counts by distinct vk_id from AutoBroadcastLog (birthday triggers: 7d, 1d, day-of).
    Note: birthday sends bypass BroadcastRecipient and log directly to AutoBroadcastLog.
    """
    from apps.tenant.senler.models import AutoBroadcastLog, AutoBroadcastType

    BIRTHDAY_TRIGGERS = [
        AutoBroadcastType.BIRTHDAY_7_DAYS,
        AutoBroadcastType.BIRTHDAY_1_DAY,
        AutoBroadcastType.BIRTHDAY,
    ]
    qs = AutoBroadcastLog.objects.filter(
        trigger_type__in=BIRTHDAY_TRIGGERS,
        sent_at__date__gte=start_date,
        sent_at__date__lte=end_date,
    )
    if branch_ids:
        from apps.tenant.branch.models import ClientBranch
        vk_ids = ClientBranch.objects.filter(
            branch__in=branch_ids,
            client__vk_id__isnull=False,
        ).values_list('client__vk_id', flat=True)
        qs = qs.filter(vk_id__in=vk_ids)
    return qs.values('vk_id').distinct().count()


# ── Metric 9: Birthday celebrants (came to redeem birthday prize) ─────────────

# Минимум дней между установкой ДР и визитом, чтобы визит считался
# «пришёл отметить». Без этого фильтра гость, поставивший ДР=сегодня
# при первом входе и сразу пришедший, попадает в счётчик — хотя
# поздравления ему никогда не отправлялись (см. send_birthday_broadcasts_task,
# который применяет тот же 30-дневный антиабузный фильтр).
BIRTHDAY_SET_MIN_DAYS_BEFORE_VISIT = 30


def get_birthday_celebrants(
    branch_ids: list[int] | None, start_date: date, end_date: date
) -> int:
    """
    Unique guests who visited the cafe on their birthday (month+day of visit matches birth_date).
    Counts distinct guests from ClientBranchVisit where visit date matches their birthday.

    Гости, установившие ДР менее BIRTHDAY_SET_MIN_DAYS_BEFORE_VISIT дней
    до визита, не считаются — они не получали поздравительных рассылок
    и фактически использовали ДР как форму регистрации.
    """
    from datetime import timedelta
    from django.db.models import F, ExpressionWrapper, DateField
    from django.db.models.functions import ExtractMonth, ExtractDay, TruncDate
    from apps.tenant.branch.models import ClientBranchVisit

    # Cutoff в DateField (без TZ): TruncDate(visited_at) − 30 дней.
    # Сравниваем DateField с DateField, чтобы исключить TZ-сдвиги на границе
    # суток (USE_TZ=True): иначе UTC-полночь visited_at могла бы попасть
    # в предыдущий локальный день и фильтр работал нестабильно.
    visit_minus_grace = ExpressionWrapper(
        TruncDate('visited_at') - timedelta(days=BIRTHDAY_SET_MIN_DAYS_BEFORE_VISIT),
        output_field=DateField(),
    )

    qs = ClientBranchVisit.objects.filter(
        visited_at__date__gte=start_date,
        visited_at__date__lte=end_date,
        client__birth_date__isnull=False,
        client__birth_date_set_at__isnull=False,
    ).annotate(
        visit_month=ExtractMonth('visited_at'),
        visit_day=ExtractDay('visited_at'),
        birth_month=ExtractMonth('client__birth_date'),
        birth_day=ExtractDay('client__birth_date'),
        visit_minus_grace=visit_minus_grace,
    ).filter(
        visit_month=F('birth_month'),
        visit_day=F('birth_day'),
        client__birth_date_set_at__lte=F('visit_minus_grace'),
    )
    qs = _branch_filter(qs, branch_ids, 'client__branch__in')
    return qs.values('client').distinct().count()


# ── Metric 10: Message open rate ─────────────────────────────────────────────

def get_message_open_rate(
    branch_ids: list[int] | None, start_date: date, end_date: date
) -> float:
    """
    % of sent VK messages that were read in the period.

    Counts messages from all three outgoing channels:
    - BroadcastRecipient (рассылки и авторассылки)
    - TestimonialMessage(source=ADMIN_REPLY) (ответы на отзывы)

    read_at is populated by the hourly Celery task `check_read_status_task`
    which polls VK API `messages.getConversationsById`.

    Returns 0.0 if no messages were sent in the period.
    """
    from apps.tenant.senler.models import BroadcastRecipient, RecipientStatus
    from apps.tenant.branch.models import TestimonialMessage

    # ── Broadcasts & auto-broadcasts ─────────────────────────────────────────
    bc_qs = BroadcastRecipient.objects.filter(
        status=RecipientStatus.SENT,
        sent_at__date__gte=start_date,
        sent_at__date__lte=end_date,
    )
    if branch_ids:
        bc_qs = bc_qs.filter(client_branch__branch__in=branch_ids)

    bc_sent = bc_qs.count()
    bc_read = bc_qs.filter(read_at__isnull=False).count()

    # ── Admin replies (ответы на отзывы) ──────────────────────────────────────
    reply_qs = TestimonialMessage.objects.filter(
        source=TestimonialMessage.Source.ADMIN_REPLY,
        vk_message_id__gt='',
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
    )
    if branch_ids:
        reply_qs = reply_qs.filter(conversation__branch__in=branch_ids)

    reply_sent = reply_qs.count()
    reply_read = reply_qs.filter(read_at__isnull=False).count()

    total_sent = bc_sent + reply_sent
    total_read = bc_read + reply_read
    if total_sent == 0:
        return 0.0, 0, 0

    return round(total_read / total_sent * 100, 1), total_sent, total_read


# ── Metric 11 & 12: VK stories (not yet implemented) ─────────────────────────

def get_vk_stories_publishers(
    branch_ids: list[int] | None, start_date: date, end_date: date
) -> int:
    """
    Unique guests who published a VK story via the app in the period.
    Filtered by story_uploaded_at date range (first upload date).
    """
    from apps.tenant.branch.models import ClientVKStatus

    qs = ClientVKStatus.objects.filter(
        is_story_uploaded=True,
        story_uploaded_at__date__gte=start_date,
        story_uploaded_at__date__lte=end_date,
    )
    return _branch_filter(qs, branch_ids, 'client__branch__in').count()


def get_stories_referrals(
    branch_ids: list[int] | None, start_date: date, end_date: date
) -> int:
    """
    New guests who registered via a referral link from someone's VK story.
    Counts ClientBranch records where invited_by is set and created_at is in period.
    """
    from apps.tenant.branch.models import ClientBranch

    qs = ClientBranch.objects.filter(
        invited_by__isnull=False,
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
    )
    return _branch_filter(qs, branch_ids, 'branch__in').count()


# ── Metric: Delivery activators ──────────────────────────────────────────────

def get_delivery_activators_count(
    branch_ids: list[int] | None, start_date: date, end_date: date
) -> int:
    """
    Уникальные гости, активировавшие код доставки (Delivery.activate)
    в указанном периоде.

    Гости попадают в это множество ↔ они уже учтены в get_qr_scan_count
    (тот включает Exists(Delivery.activated_by)). Это позволяет
    дашборду/отчёту показывать «из кафе» vs «из доставки» как
    непересекающиеся доли:
        from_cafe     = qr_scans − delivery_activators
        from_delivery = delivery_activators
        сумма         = qr_scans
    """
    from apps.tenant.delivery.models import Delivery

    qs = Delivery.objects.filter(
        activated_at__isnull=False,
        activated_at__date__gte=start_date,
        activated_at__date__lte=end_date,
        activated_by__isnull=False,
    )
    qs = _branch_filter(qs, branch_ids, 'activated_by__branch__in')
    return qs.values('activated_by').distinct().count()


# ── Metric 13: POS guests ────────────────────────────────────────────────────

def get_pos_guests_count(
    branch_ids: list[int] | None, start_date: date, end_date: date
) -> int:
    """
    Guest count from POS systems (IIKO/Dooglys) for the period.
    1. Reads from POSGuestCache (populated by hourly Celery task for today,
       daily at 02:00 for yesterday).
    2. If today is in range but not yet cached, supplements cached total with
       a live POS API call for today only.
    3. If the cache is completely empty, falls back to a live POS API call for
       the full range.
    Returns 0 if POS is not configured or fetch fails.
    """
    import logging
    from datetime import date as _date
    from django.db.models import Sum

    today = _date.today()

    qs = POSGuestCache.objects.filter(date__gte=start_date, date__lte=end_date)
    if branch_ids:
        qs = qs.filter(branch__in=branch_ids)
    result = qs.aggregate(total=Sum('guest_count'))
    cached_total = result['total'] or 0

    # Check whether today is in the requested range but missing from the cache
    today_in_range = start_date <= today <= end_date
    today_cached = today_in_range and qs.filter(date=today).exists()

    if cached_total and (not today_in_range or today_cached):
        # Cache is complete for the requested range
        return cached_total

    # Need live data: either the cache is empty, or today is missing
    try:
        from django.db import connection
        from apps.shared.config.models import POSType
        from apps.tenant.analytics.pos_service import sync_get_guests_for_period
        from apps.tenant.branch.models import Branch

        config = getattr(connection.tenant, 'config', None)
        if not config or getattr(config, 'pos_type', POSType.NONE) == POSType.NONE:
            return cached_total

        branches_qs = Branch.objects.filter(is_active=True)
        if branch_ids:
            branches_qs = branches_qs.filter(id__in=branch_ids)
        branches = list(branches_qs)
        if not branches:
            return cached_total

        if cached_total and today_in_range and not today_cached:
            # Cache has past days; only fetch today live
            live_results = sync_get_guests_for_period(config, today, today, branches=branches)
        else:
            # Cache is completely empty — fetch the full range
            live_results = sync_get_guests_for_period(config, start_date, end_date, branches=branches)

        live_total = sum(live_results.values()) if live_results else 0
        return cached_total + live_total

    except Exception:
        logging.getLogger(__name__).exception('get_pos_guests_count: live POS fetch failed')
        return cached_total


# ── Metric 14: Scan index ────────────────────────────────────────────────────

def get_scan_index(
    branch_ids: list[int] | None, start_date: date, end_date: date
) -> float:
    """QR scans ÷ POS guests × 100%. Returns 0.0 if no POS data."""
    scans = get_qr_scan_count(branch_ids, start_date, end_date)
    pos = get_pos_guests_count(branch_ids, start_date, end_date)
    if not pos:
        return 0.0
    return round(scans / pos * 100, 1)


# ── Main aggregate ────────────────────────────────────────────────────────────

def get_general_stats(
    branch_ids: list[int] | None, start_date: date, end_date: date,
    skip_slow: bool = False,
) -> dict:
    """All general-stats metrics in a single dict for the API response.

    Pass skip_slow=True to omit POS-dependent metrics (pos_guests, scan_index)
    that may require a live external API call. Use the /api/v1/analytics/stats/slow/
    endpoint to load those asynchronously after the page renders.
    """
    scans = get_qr_scan_count(branch_ids, start_date, end_date)

    if skip_slow:
        pos        = None
        scan_index = None
    else:
        pos        = get_pos_guests_count(branch_ids, start_date, end_date)
        scan_index = round(scans / pos * 100, 1) if pos else 0.0

    return {
        'qr_scans':                  scans,
        'total_vk_subscribers':      get_total_vk_subscribers(branch_ids),
        'new_group_with_gift':       get_new_group_with_first_gift(branch_ids, start_date, end_date),
        'repeat_game_players':       get_repeat_game_players(branch_ids, start_date, end_date),
        'coin_purchasers':           get_coin_purchasers(branch_ids, start_date, end_date),
        'new_community_subscribers': get_new_community_subscribers(branch_ids, start_date, end_date),
        'new_newsletter_subscribers': get_new_newsletter_subscribers(branch_ids, start_date, end_date),
        'first_gift_receivers':      get_first_gift_receivers(branch_ids, start_date, end_date),
        'gift_activators':           get_gift_activators(branch_ids, start_date, end_date),
        'birthday_greetings_sent':   get_birthday_greetings_sent(branch_ids, start_date, end_date),
        'birthday_celebrants':       get_birthday_celebrants(branch_ids, start_date, end_date),
        **dict(zip(
            ('message_open_rate', 'message_total_sent', 'message_total_read'),
            get_message_open_rate(branch_ids, start_date, end_date),
        )),
        'vk_stories_publishers':     get_vk_stories_publishers(branch_ids, start_date, end_date),
        'stories_referrals':         get_stories_referrals(branch_ids, start_date, end_date),
        'pos_guests':                pos,
        'scan_index':                scan_index,
    }


# ── Chart data ────────────────────────────────────────────────────────────────

def get_chart_data(
    branch_ids: list[int] | None, start_date: date, end_date: date
) -> dict:
    """
    Returns data for all dashboard donut charts.

    Все донат-графики (повторы / подарки / истории / задания) увязаны
    с тем же знаменателем, что показывают карточки ключевых показателей —
    в первую очередь «Отсканировали QR-код». Это исключает рассогласования
    между графиком и числом в карточке: цифры на одной странице больше
    не противоречат друг другу.

    Соответствие карточкам:
      Повторы           → repeat_game_players      vs qr_scans
      Подарки           → gift_activators / coin_purchasers
      Задания           → QuestSubmit (uniq guests) vs qr_scans
      Истории ВК        → vk_stories_publishers     vs qr_scans
    """
    from apps.tenant.game.models import ClientAttempt
    from apps.tenant.quest.models import QuestSubmit

    # ── Знаменатель для всех графиков: «отсканировали QR-код» ────────────────
    qr_scans = get_qr_scan_count(branch_ids, start_date, end_date)

    # ── 1. Повторные визиты — увязано с «вернулись и сыграли повторно» ───────
    # «Повторно» = repeat_game_players (≥2 разных дня игры)
    # «Разово»   = qr_scans − repeat_game_players
    # Сумма      = qr_scans
    repeat_players = get_repeat_game_players(branch_ids, start_date, end_date)
    once_visits = max(0, qr_scans - repeat_players)

    # ── 2. Подарки — формула работодателя ────────────────────────────────────
    # Бесплатный   = «Активировали подарок» (gift_activators) — целиком
    # За баллы     = «Купили подарки за баллы» (coin_purchasers)
    # Не забрали   = qr_scans − gift_activators − coin_purchasers
    #
    # Замечание: gift_activators и coin_purchasers концептуально могут
    # пересекаться (гость, который и купил, и активировал). Здесь формула
    # принимает их как непересекающиеся группы — так, как читает их
    # клиент на дашборде. max(0, …) страхует визуализацию от отрицательного
    # «не забрали» при таких пересечениях.
    free_activators = get_gift_activators(branch_ids, start_date, end_date)
    coin_purchases  = get_coin_purchasers(branch_ids, start_date, end_date)
    no_gift_taken   = max(0, qr_scans - free_activators - coin_purchases)

    # ── 3. Привлечение персонала (без изменений) ─────────────────────────────
    attempts_qs = ClientAttempt.objects.filter(
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
    )
    attempts_qs = _branch_filter(attempts_qs, branch_ids, 'client__branch__in')
    served_count     = attempts_qs.filter(served_by__isnull=False).count()
    not_served_count = attempts_qs.filter(served_by__isnull=True).count()

    # ── 4. Задания — увязано с qr_scans ──────────────────────────────────────
    # Вошли и выполнили      = uniq client_id с completed_at IS NOT NULL
    # Вошли и не выполнили   = uniq client_id − completed
    # Не заходили в задания  = qr_scans − uniq client_id
    # Сумма                  = qr_scans
    quest_qs = QuestSubmit.objects.filter(
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
    )
    quest_qs = _branch_filter(quest_qs, branch_ids, 'client__branch__in')

    quest_entered_ids = set(quest_qs.values_list('client_id', flat=True).distinct())
    quest_completed_ids = set(
        quest_qs.filter(completed_at__isnull=False)
        .values_list('client_id', flat=True).distinct()
    )
    quests_done    = len(quest_completed_ids)
    quests_failed  = max(0, len(quest_entered_ids) - quests_done)
    quests_skipped = max(0, qr_scans - len(quest_entered_ids))

    # ── 5. Истории ВК — увязано с qr_scans ───────────────────────────────────
    # Опубликовали      = vk_stories_publishers (за период)
    # Не опубликовали   = qr_scans − опубликовали
    # Сумма             = qr_scans
    stories_uploaded     = get_vk_stories_publishers(branch_ids, start_date, end_date)
    stories_not_uploaded = max(0, qr_scans - stories_uploaded)

    return {
        'repeat_visits':     {'repeat': repeat_players,    'first_time': once_visits},
        'gift_sources':      {
            'free':       free_activators,
            'coins':      coin_purchases,
            'not_taken':  no_gift_taken,
        },
        'staff_involvement': {'served': served_count,      'not_served': not_served_count},
        'quests':            {
            'completed':   quests_done,
            'pending':     quests_failed,
            'not_entered': quests_skipped,
        },
        'vk_stories':        {'uploaded': stories_uploaded, 'not_uploaded': stories_not_uploaded},
    }


# ── RF helpers ────────────────────────────────────────────────────────────────

# ── Standard hints & strategies — ALWAYS override DB values ───────────────────
# This ensures every cafe shows the same short, correct tips regardless of
# whether the management command was run or what custom data is in the DB.
_STANDARD_SEGMENT_DATA: dict[str, dict[str, str]] = {
    'R3F1': {
        'strategy': 'Закрепить первый визит и привести ко второму заказу.',
        'hint': (
            'Цель: Закрепить первый визит и привести ко второму заказу\n'
            'Что отправлять: Приветственное сообщение + простой бонус на следующий заказ '
            '(подарок или небольшая выгода)\n'
            'Частота: 1–2 сообщения за 7–14 дней\n'
            'Если реакция слабая: Отправьте напоминание с тем же бонусом, '
            'затем пауза минимум 2 недели'
        ),
    },
    'R3F2': {
        'strategy': 'Сформировать привычку заказывать регулярно.',
        'hint': (
            'Цель: Сформировать привычку заказывать регулярно\n'
            'Что отправлять: Выгодное предложение или новинка с ограниченным сроком\n'
            'Частота: 1 сообщение раз в 7–10 дней\n'
            'Если реакция слабая: Смените повод (другой бонус/набор) и сделайте паузу 10–14 дней'
        ),
    },
    'R3F3': {
        'strategy': 'Удержать самых активных гостей без перегруза.',
        'hint': (
            'Цель: Удержать самых активных гостей без перегруза\n'
            'Что отправлять: Редкие «приятные» сообщения: подарок или благодарность '
            'без сложных условий\n'
            'Частота: 1 сообщение раз в 10–14 дней\n'
            'Если реакция слабая: Ничего не усиливайте — просто сделайте паузу '
            'и не учащайте рассылки'
        ),
    },
    'R2F1': {
        'strategy': 'Вернуть гостя на повторный заказ.',
        'hint': (
            'Цель: Вернуть гостя на повторный заказ\n'
            'Что отправлять: Простая акция или подарок с коротким сроком действия\n'
            'Частота: 1 сообщение раз в 10–14 дней\n'
            'Если реакция слабая: Через 3–5 дней отправьте другой повод, '
            'затем пауза 2–3 недели'
        ),
    },
    'R2F2': {
        'strategy': 'Поддерживать интерес и не дать «остыть».',
        'hint': (
            'Цель: Поддерживать интерес и не дать «остыть»\n'
            'Что отправлять: Выгодное предложение или напоминание о кафе без давления\n'
            'Частота: 1 сообщение раз в 14 дней\n'
            'Если реакция слабая: Сделайте паузу 2–3 недели, не усиливая бонус'
        ),
    },
    'R2F3': {
        'strategy': 'Не потерять самых ценных гостей.',
        'hint': (
            'Цель: Не потерять самых ценных гостей\n'
            'Что отправлять: Только VIP-поводы: подарок, благодарность, особое предложение\n'
            'Частота: 1 сообщение в месяц (максимум 2)\n'
            'Если реакция слабая: Ничего не отправляйте дополнительно — лучше пауза, '
            'чем лишнее сообщение'
        ),
    },
    'R1F1': {
        'strategy': 'Вернуть до перехода в «потерянные».',
        'hint': (
            'Цель: Вернуть до перехода в «потерянные»\n'
            'Что отправлять: Мягкое напоминание + бонус на возвращение\n'
            'Частота: 2 сообщения за 7–10 дней\n'
            'Если реакция слабая: Если нет отклика — пауза 3–4 недели'
        ),
    },
    'R1F2': {
        'strategy': 'Срочно вернуть ранее постоянных гостей.',
        'hint': (
            'Цель: Срочно вернуть ранее постоянных гостей\n'
            'Что отправлять: Сильное, но понятное предложение с ограниченным сроком\n'
            'Частота: 2 сообщения за 10–14 дней\n'
            'Если реакция слабая: После второго сообщения — пауза минимум 1 месяц'
        ),
    },
    'R1F3': {
        'strategy': 'Аккуратно вернуть VIP-гостя.',
        'hint': (
            'Цель: Аккуратно вернуть VIP-гостя\n'
            'Что отправлять: Личное сообщение + подарок без условий\n'
            'Частота: 1 сообщение + напоминание через 10–14 дней\n'
            'Если реакция слабая: Дальше только пауза, не чаще 1 раза в 1–2 месяца'
        ),
    },
    'R0F1': {
        'strategy': 'Попробовать реактивировать без давления.',
        'hint': (
            'Цель: Попробовать реактивировать без давления\n'
            'Что отправлять: Одно камбэк-предложение или новинка\n'
            'Частота: 1 сообщение раз в 1–2 месяца\n'
            'Если реакция слабая: Если реакции нет — увеличить паузу или исключить рассылки'
        ),
    },
    'R0F2': {
        'strategy': 'Вернуть сильных гостей прошлого.',
        'hint': (
            'Цель: Вернуть сильных гостей прошлого\n'
            'Что отправлять: Персональный повод вернуться с хорошей выгодой\n'
            'Частота: 2 попытки за 6 недель\n'
            'Если реакция слабая: Дальше только редкие сообщения раз в 2–3 месяца'
        ),
    },
    'R0F3': {
        'strategy': 'Финальная попытка вернуть VIP.',
        'hint': (
            'Цель: Финальная попытка вернуть VIP\n'
            'Что отправлять: Очень уважительное предложение или личное обращение\n'
            'Частота: 1 сообщение раз в 6–8 недель\n'
            'Если реакция слабая: Если нет реакции — полностью прекратить рассылки'
        ),
    },
}

# Map (r_score, f_score) → segment code for lookup
_RF_TO_CODE = {
    (4, 1): 'R3F1', (4, 2): 'R3F2', (4, 3): 'R3F3',
    (3, 1): 'R2F1', (3, 2): 'R2F2', (3, 3): 'R2F3',
    (2, 1): 'R1F1', (2, 2): 'R1F2', (2, 3): 'R1F3',
    (1, 1): 'R0F1', (1, 2): 'R0F2', (1, 3): 'R0F3',
}

# Reverse: segment code → (r_score, f_score)
_CODE_TO_RF: dict[str, tuple[int, int]] = {v: k for k, v in _RF_TO_CODE.items()}


def _apply_standard_hints(cell: dict) -> None:
    """Override hint & strategy with hardcoded standard values."""
    code = _RF_TO_CODE.get((cell['r_score'], cell['f_score']), '')
    std = _STANDARD_SEGMENT_DATA.get(code)
    if std:
        cell['segment_hint'] = std['hint']
        cell['segment_strategy'] = std['strategy']


# Fixed R/F level labels (r_score=4 → R3 = most recent, r_score=1 → R0 = lost)
# ВАЖНО: эти словари — fallback'и для дефолтных порогов. Реальные значения
# для админ-панели строятся на лету через _build_r_meta() / _build_f_meta()
# на основании актуальных порогов выбранной области (точка / Все точки / дефолт).
_R_META = {
    4: {'label': 'R3', 'name': 'Свежий',    'range': '0–14 дн.'},
    3: {'label': 'R2', 'name': 'Тёплый',    'range': '15–30 дн.'},
    2: {'label': 'R1', 'name': 'Остывший',  'range': '31–60 дн.'},
    1: {'label': 'R0', 'name': 'Холодный',  'range': '>61 дн.'},
}
_F_META = {
    1: {'label': 'F1', 'name': 'Редко',     'range': '1–3 виз.'},
    2: {'label': 'F2', 'name': 'Умеренно',  'range': '4–5 виз.'},
    3: {'label': 'F3', 'name': 'Часто',     'range': '6+ виз.'},
}

# Representative recency/frequency values used to look up segments by r/f score.
# Используется только как fallback при поиске сегмента по статичным RFSegment-границам.
_R_REPRESENTATIVE = {4: 7, 3: 22, 2: 45, 1: 90}
_F_REPRESENTATIVE = {1: 2, 2: 4, 3: 7}


def _build_r_meta(thresholds: dict) -> dict:
    """
    Строит метаданные R-уровней (label / name / range) исходя из
    актуальных порогов выбранной области.
    """
    fresh = thresholds['r_fresh_max']
    warm  = thresholds['r_warm_max']
    cool  = thresholds['r_cooling_max']
    return {
        4: {'label': 'R3', 'name': 'Свежий',    'range': f'0–{fresh} дн.'},
        3: {'label': 'R2', 'name': 'Тёплый',    'range': f'{fresh + 1}–{warm} дн.'},
        2: {'label': 'R1', 'name': 'Остывший',  'range': f'{warm + 1}–{cool} дн.'},
        1: {'label': 'R0', 'name': 'Холодный',  'range': f'>{cool} дн.'},
    }


def _build_f_meta(thresholds: dict) -> dict:
    """
    Строит метаданные F-уровней (label / name / range) исходя из
    актуальных порогов выбранной области.
    """
    rare = thresholds['f_rare_max']
    mod  = thresholds['f_moderate_max']

    f1_range = f'1–{rare} виз.' if rare > 1 else '1 виз.'
    f2_range = f'{rare + 1}–{mod} виз.' if mod > rare + 1 else f'{rare + 1} виз.'
    f3_range = f'{mod + 1}+ виз.'

    return {
        1: {'label': 'F1', 'name': 'Редко',     'range': f1_range},
        2: {'label': 'F2', 'name': 'Умеренно',  'range': f2_range},
        3: {'label': 'F3', 'name': 'Часто',     'range': f3_range},
    }


def _build_r_representative(thresholds: dict) -> dict:
    """
    Репрезентативные значения дней-давности для каждого R-балла.
    Используются только для разрешения сегмента «по умолчанию», когда
    в RFSegment настроены свои recency_min/max границы.
    Берём «середину» каждого диапазона.
    """
    fresh = thresholds['r_fresh_max']
    warm  = thresholds['r_warm_max']
    cool  = thresholds['r_cooling_max']
    return {
        4: max(0, fresh // 2),
        3: (fresh + 1 + warm) // 2,
        2: (warm + 1 + cool) // 2,
        1: cool + 30,
    }


def _build_f_representative(thresholds: dict) -> dict:
    rare = thresholds['f_rare_max']
    mod  = thresholds['f_moderate_max']
    return {
        1: max(1, rare),
        2: (rare + 1 + mod) // 2 if mod > rare else rare + 1,
        3: mod + 1,
    }


def get_rf_thresholds(branch_ids: list[int] | None) -> dict[str, int]:
    """
    Возвращает актуальные R/F-пороги для выбранной области.

    Приоритет:
      1) ровно одна точка в выборке и для неё есть RFSettings → её пороги;
      2) запись RFSettings с branch=NULL («Все точки») → её пороги;
      3) RF_DEFAULT_THRESHOLDS — захардкоженные дефолты.

    См. RFSettings.resolve_for_scope() в модели для деталей.
    """
    from apps.tenant.analytics.models import RFSettings
    return RFSettings.thresholds_for_scope(branch_ids)


def _r_score(recency_days: int, thresholds: dict | None = None) -> int:
    """
    R-балл по давности. По умолчанию использует RF_DEFAULT_THRESHOLDS,
    если пороги не переданы (для обратной совместимости с местами,
    которым не нужно знать про область).
    """
    if thresholds is None:
        from apps.tenant.analytics.models import RF_DEFAULT_THRESHOLDS
        thresholds = RF_DEFAULT_THRESHOLDS
    if recency_days <= thresholds['r_fresh_max']:    return 4   # R3
    if recency_days <= thresholds['r_warm_max']:     return 3   # R2
    if recency_days <= thresholds['r_cooling_max']:  return 2   # R1
    return 1                                                     # R0


def _f_score(frequency: int, thresholds: dict | None = None) -> int:
    """
    F-балл по частоте. По умолчанию использует RF_DEFAULT_THRESHOLDS.
    """
    if thresholds is None:
        from apps.tenant.analytics.models import RF_DEFAULT_THRESHOLDS
        thresholds = RF_DEFAULT_THRESHOLDS
    if frequency > thresholds['f_moderate_max']: return 3   # F3
    if frequency > thresholds['f_rare_max']:     return 2   # F2
    return 1                                                  # F1


def _get_score_model(mode: str):
    from apps.tenant.analytics.models import GuestRFScore, GuestRFScoreDelivery
    return GuestRFScoreDelivery if mode == 'delivery' else GuestRFScore


def _get_migration_model(mode: str):
    from apps.tenant.analytics.models import RFMigrationLog, RFMigrationLogDelivery
    return RFMigrationLogDelivery if mode == 'delivery' else RFMigrationLog


def _get_snapshot_model(mode: str):
    from apps.tenant.analytics.models import BranchSegmentSnapshot, BranchSegmentSnapshotDelivery
    return BranchSegmentSnapshotDelivery if mode == 'delivery' else BranchSegmentSnapshot


def _build_code_to_segment(branch_ids: list[int] | None) -> dict:
    """
    Возвращает {code: RFSegment} с приоритетом per-branch → global.

    Если выбрана одна точка — её сегменты переопределяют общие.
    Иначе используются только общие (branch=NULL) сегменты.
    """
    from apps.tenant.analytics.models import RFSegment

    global_map = {seg.code: seg for seg in RFSegment.objects.filter(branch__isnull=True)}
    if branch_ids and len(branch_ids) == 1:
        branch_map = {seg.code: seg for seg in RFSegment.objects.filter(branch_id=branch_ids[0])}
        return {**global_map, **branch_map}
    return global_map


# ── RF Matrix ─────────────────────────────────────────────────────────────────

def get_rf_matrix(
    branch_ids: list[int] | None,
    mode: str = 'restaurant',
    client_ids: list[int] | None = None,
    client_branch_ids: list[int] | None = None,
    thresholds: dict | None = None,
) -> dict:
    """
    Build the RF matrix for the given mode (restaurant | delivery).

    client_branch_ids: optional list of ClientBranch PKs (per-branch профили) для
                       подсчёта. Используется чтобы согласовать total матрицы
                       с QR-сканами общей аналитики (один гость в N кафе = N
                       профилей). RF score для каждого профиля берётся из
                       client.rf_score (один на гостя).
    client_ids: legacy — список guest.Client PKs (используется когда матрица
                рисуется без периода). Один Client = одна запись в total.
    thresholds: optional thresholds dict; if omitted, derives from RFSettings
                via get_rf_thresholds(branch_ids).

    ВАЖНО: r/f-баллы пересчитываются «на лету» из сохранённых
    recency_days/frequency, чтобы матрица всегда отражала актуальные
    пороги выбранной области (точка / Все точки), даже если последний
    recalculate_rf_scores выполнялся с другими порогами.

    Returns {
      total: int,
      r_levels: [...],    # sorted desc (R3→R0), labels/ranges на основе порогов
      f_levels: [...],    # sorted asc (F1→F3), labels/ranges на основе порогов
      cells: {            # key: "r_f"
        "4_1": {segment_code, segment_name, emoji, color, count, pct},
        ...
      },
      thresholds: {...},  # пороги, использованные для построения
      thresholds_source: 'branch' | 'global' | 'default',
    }
    """
    from apps.tenant.analytics.models import RFSegment, RFSettings

    # 1) Определяем пороги и их источник.
    if thresholds is None:
        _, thresholds, source = RFSettings.resolve_for_scope(branch_ids)
    else:
        source = 'explicit'

    # 2) Достаём «сырые» recency/frequency и пересчитываем r/f.
    #    Если передан client_branch_ids — считаем per-profile (как QR-сканы):
    #    один ClientBranch = одна строка, RF score достаётся из client.rf_score.
    #    Иначе — legacy режим: per-Client из ScoreModel напрямую.
    if client_branch_ids is not None:
        from apps.tenant.branch.models import ClientBranch
        score_field_base = (
            'client__rf_score_delivery' if mode == 'delivery' else 'client__rf_score'
        )
        cb_qs = ClientBranch.objects.filter(
            pk__in=client_branch_ids,
            **{f'{score_field_base}__isnull': False},
        )
        rows = list(cb_qs.values(
            f'{score_field_base}__recency_days',
            f'{score_field_base}__frequency',
        ))
        # Унифицируем ключи под единый формат
        rows = [
            {
                'recency_days': r[f'{score_field_base}__recency_days'],
                'frequency':    r[f'{score_field_base}__frequency'],
            }
            for r in rows
        ]
    else:
        ScoreModel = _get_score_model(mode)
        qs = ScoreModel.objects.all()
        qs = _branch_filter(qs, branch_ids, 'client__branch_profiles__branch__in')
        if branch_ids:
            qs = qs.distinct()
        if client_ids is not None:
            qs = qs.filter(client_id__in=client_ids)
        rows = list(qs.values('id', 'recency_days', 'frequency'))

    total = len(rows)

    cell_counts: dict[tuple[int, int], int] = defaultdict(int)
    for row in rows:
        rs = _r_score(row['recency_days'], thresholds)
        fs = _f_score(row['frequency'],     thresholds)
        cell_counts[(rs, fs)] += 1

    # 3) Подтягиваем метаданные сегментов по коду (R3F1 и т.п.) — это
    #    стабильно при изменении границ конкретных RFSegment-записей.
    #    Per-branch версии (если есть) переопределяют общие.
    code_to_segment = _build_code_to_segment(branch_ids)

    r_vals = [4, 3, 2, 1]
    f_vals = [1, 2, 3]

    cells: dict[str, dict] = {}
    for r in r_vals:
        for f in f_vals:
            key  = f'{r}_{f}'
            code = _RF_TO_CODE.get((r, f), '')
            seg  = code_to_segment.get(code)
            count = cell_counts.get((r, f), 0)
            cells[key] = {
                'r_score':          r,
                'f_score':          f,
                'segment_id':       seg.pk       if seg else None,
                'segment_code':     seg.code     if seg else code,
                'segment_name':     seg.name     if seg else '—',
                'segment_emoji':    seg.emoji    if seg else '',
                'segment_color':    seg.color    if seg else '#e8e8e8',
                'segment_strategy': seg.strategy if seg else '',
                'segment_hint':     seg.hint     if seg else '',
                'count':            count,
                'pct':              round(count / total * 100, 1) if total else 0.0,
            }

    # ── Always override hints/strategy with hardcoded standard values ─────
    # This guarantees every cafe shows the same correct tips,
    # regardless of what's stored in the DB.
    for cell in cells.values():
        _apply_standard_hints(cell)

    # 4) Заголовки строк/колонок строим из актуальных порогов.
    r_meta = _build_r_meta(thresholds)
    f_meta = _build_f_meta(thresholds)

    return {
        'total':    total,
        'r_levels': [{'r_score': r, **r_meta.get(r, {'label': f'R{r-1}', 'name': '', 'range': ''})} for r in r_vals],
        'f_levels': [{'f_score': f, **f_meta.get(f, {'label': f'F{f}',   'name': '', 'range': ''})} for f in f_vals],
        'cells':    cells,
        'thresholds':        thresholds,
        'thresholds_source': source,
    }


# ── Active guest IDs for a period ────────────────────────────────────────────

def _get_active_client_ids(
    branch_ids: list[int] | None, start_date: date, end_date: date, mode: str,
) -> list[int]:
    """
    Return guest.Client PKs with at least one visit/delivery in [start_date, end_date].
    Used to scope the RF matrix to guests active in the selected period.

    Note: используется только legacy-путями. Новый код предпочитает
    _get_active_client_branch_ids(), который согласует подсчёт с QR-сканами.
    """
    if mode == 'restaurant':
        from apps.tenant.branch.models import ClientBranchVisit
        qs = ClientBranchVisit.objects.filter(
            visited_at__date__gte=start_date,
            visited_at__date__lte=end_date,
        )
        if branch_ids:
            qs = qs.filter(client__branch__in=branch_ids)
        return list(qs.values_list('client__client_id', flat=True).distinct())
    else:
        from apps.tenant.delivery.models import Delivery
        qs = Delivery.objects.filter(
            activated_at__date__gte=start_date,
            activated_at__date__lte=end_date,
            activated_by__isnull=False,
        )
        if branch_ids:
            qs = qs.filter(activated_by__branch__in=branch_ids)
        return list(qs.values_list('activated_by__client_id', flat=True).distinct())


def _get_active_client_branch_ids(
    branch_ids: list[int] | None, start_date: date, end_date: date, mode: str,
) -> list[int]:
    """
    Return ClientBranch PKs (per-branch профили) активные в периоде —
    та же логика, что у get_qr_scan_count(), чтобы итоги RF матрицы совпадали
    с «отсканировали QR» из общей аналитики.

    Для mode='restaurant':
      ClientBranch с визитом в периоде И с любым из:
        - community_via_app / newsletter_via_app / is_story_uploaded
        - сыграл в игру
        - оставил отзыв в приложении
        - активировал delivery-код
        - потратил монеты в магазине

    Для mode='delivery':
      ClientBranch с активированным delivery в периоде.
    """
    from django.db.models import Exists, OuterRef
    from apps.tenant.branch.models import (
        ClientBranch, ClientBranchVisit, CoinTransaction,
        TransactionType, TransactionSource, TestimonialMessage,
    )
    from apps.tenant.game.models import ClientAttempt
    from apps.tenant.delivery.models import Delivery

    if mode == 'delivery':
        qs = Delivery.objects.filter(
            activated_at__date__gte=start_date,
            activated_at__date__lte=end_date,
            activated_by__isnull=False,
        )
        if branch_ids:
            qs = qs.filter(activated_by__branch__in=branch_ids)
        return list(qs.values_list('activated_by_id', flat=True).distinct())

    visited_ids = ClientBranchVisit.objects.filter(
        visited_at__date__gte=start_date,
        visited_at__date__lte=end_date,
    )
    visited_ids = _branch_filter(visited_ids, branch_ids, 'client__branch__in').values('client_id')

    cb_qs = ClientBranch.objects.filter(pk__in=visited_ids).filter(
        Q(vk_status__community_via_app=True)
        | Q(vk_status__newsletter_via_app=True)
        | Q(vk_status__is_story_uploaded=True)
        | Exists(ClientAttempt.objects.filter(
            client=OuterRef('pk'),
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
        ))
        | Exists(TestimonialMessage.objects.filter(
            conversation__client=OuterRef('pk'),
            source=TestimonialMessage.Source.APP,
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
        ))
        | Exists(Delivery.objects.filter(
            activated_by=OuterRef('pk'),
            activated_at__date__gte=start_date,
            activated_at__date__lte=end_date,
        ))
        | Exists(CoinTransaction.objects.filter(
            client=OuterRef('pk'),
            type=TransactionType.EXPENSE,
            source=TransactionSource.SHOP,
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
        ))
    )
    return list(cb_qs.values_list('pk', flat=True))


# ── RF Matrix from snapshot (period-based) ───────────────────────────────────

def get_rf_matrix_from_snapshot(
    branch_ids: list[int] | None, start_date: date, end_date: date,
    mode: str = 'restaurant',
) -> dict:
    """
    Build the RF matrix from BranchSegmentSnapshot for a given period.

    Takes the most recent snapshot date within [start_date, end_date] and
    sums guest counts per segment across branches.
    Falls back to get_rf_matrix() (live GuestRFScore) when no snapshot data exists.

    ВАЖНО: пороги отображения (заголовки строк/колонок и labels) берутся
    для текущей выбранной области (точка / Все точки / дефолт). Сами
    числа гостей в ячейках построены из снапшота, который писался при
    последнем recalculate; при смене порогов рекомендуется пересчёт.
    """
    from django.db.models import Max, Sum
    from apps.tenant.analytics.models import RFSegment, RFSettings

    _, thresholds, source = RFSettings.resolve_for_scope(branch_ids)

    SnapshotModel = _get_snapshot_model(mode)
    qs = SnapshotModel.objects.filter(date__gte=start_date, date__lte=end_date)
    if branch_ids:
        qs = qs.filter(branch__in=branch_ids)

    last_date = qs.aggregate(m=Max('date'))['m']
    if not last_date:
        return get_rf_matrix(branch_ids, mode=mode, thresholds=thresholds)

    rows = list(
        qs.filter(date=last_date)
        .values(
            'segment__id', 'segment__code', 'segment__name',
            'segment__emoji', 'segment__color',
            'segment__strategy', 'segment__hint',
        )
        .annotate(count=Sum('guests_count'))
    )

    total = sum((r['count'] or 0) for r in rows) or 0

    cell_lookup: dict[str, dict] = {}
    for row in rows:
        code = row['segment__code'] or ''
        rf = _CODE_TO_RF.get(code)
        if not rf:
            continue
        r, f = rf
        cell_lookup[f'{r}_{f}'] = {
            'r_score':          r,
            'f_score':          f,
            'segment_id':       row['segment__id'],
            'segment_code':     code,
            'segment_name':     row['segment__name'] or '—',
            'segment_emoji':    row['segment__emoji'] or '',
            'segment_color':    row['segment__color'] or '#e0e0e0',
            'segment_strategy': row['segment__strategy'] or '',
            'segment_hint':     row['segment__hint'] or '',
            'count':            row['count'] or 0,
            'pct':              round((row['count'] or 0) / total * 100, 1) if total else 0.0,
        }

    # Подтягиваем актуальные метаданные сегментов по коду
    # (per-branch версии переопределяют общие).
    code_to_segment = _build_code_to_segment(branch_ids)

    r_vals = [4, 3, 2, 1]
    f_vals = [1, 2, 3]

    cells: dict[str, dict] = {}
    for r in r_vals:
        for f in f_vals:
            key  = f'{r}_{f}'
            code = _RF_TO_CODE.get((r, f), '')
            seg  = code_to_segment.get(code)
            if key in cell_lookup:
                cells[key] = cell_lookup[key]
                # Перетираем display-поля, чтобы сегмент отображался
                # с актуальными name/emoji/color из RFSegment.
                if seg:
                    cells[key].update({
                        'segment_id':       seg.pk,
                        'segment_code':     seg.code,
                        'segment_name':     seg.name,
                        'segment_emoji':    seg.emoji,
                        'segment_color':    seg.color,
                        'segment_strategy': seg.strategy,
                        'segment_hint':     seg.hint,
                    })
            else:
                cells[key] = {
                    'r_score':          r,
                    'f_score':          f,
                    'segment_id':       seg.pk       if seg else None,
                    'segment_code':     seg.code     if seg else code,
                    'segment_name':     seg.name     if seg else '—',
                    'segment_emoji':    seg.emoji    if seg else '',
                    'segment_color':    seg.color    if seg else '#e8e8e8',
                    'segment_strategy': seg.strategy if seg else '',
                    'segment_hint':     seg.hint     if seg else '',
                    'count': 0, 'pct': 0.0,
                }

    for cell in cells.values():
        _apply_standard_hints(cell)

    r_meta = _build_r_meta(thresholds)
    f_meta = _build_f_meta(thresholds)

    return {
        'total':    total,
        'r_levels': [{'r_score': r, **r_meta.get(r, {'label': f'R{r-1}', 'name': '', 'range': ''})} for r in r_vals],
        'f_levels': [{'f_score': f, **f_meta.get(f, {'label': f'F{f}',   'name': '', 'range': ''})} for f in f_vals],
        'cells':    cells,
        'thresholds':        thresholds,
        'thresholds_source': source,
    }


# ── RF Summary stats ──────────────────────────────────────────────────────────

def get_rf_summary_stats(branch_ids: list[int] | None, mode: str = 'restaurant') -> dict:
    """
    4 summary cards for the RF analysis header:
    - total:        all digitised guests
    - vip_f3:       guests with f_score == max (frequent visitors)
    - at_risk_r1:   guests with r_score == 2 (cooling)
    - lost_r0:      guests with r_score == 1 (lost/cold)

    Подсчёт ведётся «на лету» из стораженых recency_days/frequency
    с учётом актуальных порогов выбранной области, чтобы цифры были
    согласованы с матрицей.
    """
    from apps.tenant.analytics.models import RFSettings
    _, thresholds, _ = RFSettings.resolve_for_scope(branch_ids)

    ScoreModel = _get_score_model(mode)
    qs = ScoreModel.objects.all()
    qs = _branch_filter(qs, branch_ids, 'client__branch_profiles__branch__in')
    if branch_ids:
        qs = qs.distinct()

    total = vip_f3 = at_risk = lost_r0 = 0
    for row in qs.values('recency_days', 'frequency'):
        r = _r_score(row['recency_days'], thresholds)
        f = _f_score(row['frequency'],     thresholds)
        total += 1
        if f == 3: vip_f3  += 1
        if r == 2: at_risk += 1
        if r == 1: lost_r0 += 1

    return {
        'total':    total,
        'vip_f3':   vip_f3,
        'at_risk':  at_risk,
        'lost_r0':  lost_r0,
    }


# ── RF Segment guests list ────────────────────────────────────────────────────

def get_rf_segment_guests(
    branch_ids: list[int] | None, r_score: int, f_score: int,
    mode: str = 'restaurant', limit: int = 50,
    client_branch_ids: list[int] | None = None,
) -> list[dict]:
    """
    Guest list for a specific RF segment cell.

    Фильтрация выполняется по «свежим» r/f-баллам, пересчитанным на лету
    из стораженых recency_days/frequency с учётом актуальных порогов
    выбранной области, чтобы клик по ячейке всегда совпадал с тем,
    что нарисовано в матрице.

    Если передан client_branch_ids — список строится per-ClientBranch (та же
    логика, что в QR-сканах общей аналитики). Иначе — legacy per-Client.
    """
    from apps.tenant.analytics.models import RFSettings
    _, thresholds, _ = RFSettings.resolve_for_scope(branch_ids)

    from django.db.models import Max, Sum, Q as _Q
    from apps.tenant.branch.models import ClientBranch, ClientBranchVisit, CoinTransaction

    score_attr = 'rf_score_delivery' if mode == 'delivery' else 'rf_score'

    if client_branch_ids is not None:
        cb_qs = (
            ClientBranch.objects
            .select_related('client', f'client__{score_attr}', 'branch')
            .filter(
                pk__in=client_branch_ids,
                **{f'client__{score_attr}__isnull': False},
            )
        )

        matching: list[ClientBranch] = []
        for cb in cb_qs.iterator():
            rf = getattr(cb.client, score_attr, None)
            if rf is None:
                continue
            if (_r_score(rf.recency_days, thresholds) == r_score
                    and _f_score(rf.frequency, thresholds) == f_score):
                matching.append(cb)
                if len(matching) >= limit:
                    break

        if not matching:
            return []

        cb_pks = [cb.pk for cb in matching]
        last_visit_map = {
            r['client_id']: r['last']
            for r in ClientBranchVisit.objects
            .filter(client_id__in=cb_pks)
            .values('client_id')
            .annotate(last=Max('visited_at'))
        }
        balance_map = {
            r['client_id']: (r['income'] or 0) - (r['expense'] or 0)
            for r in CoinTransaction.objects
            .filter(client_id__in=cb_pks)
            .values('client_id')
            .annotate(
                income=Sum('amount', filter=_Q(type='income')),
                expense=Sum('amount', filter=_Q(type='expense')),
            )
        }

        result = []
        for cb in matching:
            rf = getattr(cb.client, score_attr)
            guest = cb.client
            last_visit = last_visit_map.get(cb.pk)
            result.append({
                'id':           guest.pk,
                'vk_id':        guest.vk_id,
                'first_name':   guest.first_name,
                'last_name':    guest.last_name,
                'branch':       cb.branch.name if cb.branch_id else '',
                'recency_days': rf.recency_days,
                'frequency':    rf.frequency,
                'r_score':      r_score,
                'f_score':      f_score,
                'last_visit':   last_visit.strftime('%d.%m.%Y') if last_visit else '—',
                'coins':        balance_map.get(cb.pk, 0),
            })
        return result

    # Legacy per-Client path (без периода): сохранён для обратной совместимости.
    ScoreModel = _get_score_model(mode)
    qs = ScoreModel.objects.select_related('client', 'segment').all()
    qs = _branch_filter(qs, branch_ids, 'client__branch_profiles__branch__in')
    if branch_ids:
        qs = qs.distinct()

    matching_scores = []
    for s in qs.iterator():
        if (_r_score(s.recency_days, thresholds) == r_score
                and _f_score(s.frequency, thresholds) == f_score):
            matching_scores.append(s)
            if len(matching_scores) >= limit:
                break

    if not matching_scores:
        return []

    guest_ids = [s.client_id for s in matching_scores]

    last_visit_map = {
        r['client__client_id']: r['last']
        for r in ClientBranchVisit.objects
        .filter(client__client_id__in=guest_ids)
        .values('client__client_id')
        .annotate(last=Max('visited_at'))
    }

    balance_map = {
        r['client__client_id']: (r['income'] or 0) - (r['expense'] or 0)
        for r in CoinTransaction.objects
        .filter(client__client_id__in=guest_ids)
        .values('client__client_id')
        .annotate(
            income=Sum('amount', filter=_Q(type='income')),
            expense=Sum('amount', filter=_Q(type='expense')),
        )
    }

    result = []
    for score in matching_scores:
        guest = score.client
        last_visit = last_visit_map.get(guest.pk)
        result.append({
            'id':           guest.pk,
            'vk_id':        guest.vk_id,
            'first_name':   guest.first_name,
            'last_name':    guest.last_name,
            'recency_days': score.recency_days,
            'frequency':    score.frequency,
            'r_score':      r_score,
            'f_score':      f_score,
            'last_visit':   last_visit.strftime('%d.%m.%Y') if last_visit else '—',
            'coins':        balance_map.get(guest.pk, 0),
        })
    return result


# ── RF snapshot trend ─────────────────────────────────────────────────────────

def get_rf_snapshot_trend(
    branch_ids: list[int] | None, days: int = 30, mode: str = 'restaurant',
    start_date: date | None = None, end_date: date | None = None,
) -> list[dict]:
    """Historical segment trend over a date range (or last N days if no dates given)."""
    from datetime import date as date_type, timedelta
    from django.db.models import Sum

    if start_date is None or end_date is None:
        end_date   = date_type.today()
        start_date = end_date - timedelta(days=days)

    SnapshotModel = _get_snapshot_model(mode)
    base = SnapshotModel.objects.filter(date__gte=start_date, date__lte=end_date)
    if branch_ids:
        base = base.filter(branch__in=branch_ids)
    qs = (
        base
        .values('date', 'segment__code', 'segment__color', 'segment__name')
        .annotate(guests=Sum('guests_count'))
        .order_by('date', 'segment__code')
    )

    by_date: dict[str, list] = defaultdict(list)
    for row in qs:
        by_date[str(row['date'])].append({
            'code':   row['segment__code'],
            'name':   row['segment__name'],
            'color':  row['segment__color'],
            'guests': row['guests'] or 0,
        })

    return [{'date': d, 'segments': segs} for d, segs in sorted(by_date.items())]


# ── RF Migration summary ──────────────────────────────────────────────────────

def get_rf_migration_summary(
    branch_ids: list[int] | None, days: int = 30, mode: str = 'restaurant',
    start_date: date | None = None, end_date: date | None = None,
) -> list[dict]:
    """Top migration flows sorted by count descending."""
    from datetime import date as date_type, timedelta

    if start_date is None or end_date is None:
        end_date   = date_type.today()
        start_date = end_date - timedelta(days=days)

    MigModel = _get_migration_model(mode)
    qs = MigModel.objects.filter(
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
        to_segment__isnull=False,           # skip records where target segment was deleted
    )
    if branch_ids:
        qs = qs.filter(client__branch_profiles__branch__in=branch_ids)

    # Use select_related to avoid INNER JOIN problem with nullable from_segment
    # (Django .values('from_segment__code') uses INNER JOIN which drops NULL rows)
    counts: dict[tuple, int] = {}
    meta: dict[tuple, dict] = {}

    for mig in qs.select_related('from_segment', 'to_segment').iterator():
        fs = mig.from_segment
        ts = mig.to_segment
        key = (
            fs.code  if fs else '',
            fs.name  if fs else '—',
            fs.emoji if fs else '',
            fs.color if fs else '#94a3b8',
            ts.code,
            ts.name,
            ts.emoji or '',
            ts.color or '#94a3b8',
        )
        counts[key] = counts.get(key, 0) + 1
        meta[key] = {
            'from_code':  key[0], 'from_name':  key[1],
            'from_emoji': key[2], 'from_color': key[3],
            'to_code':    key[4], 'to_name':    key[5],
            'to_emoji':   key[6], 'to_color':   key[7],
        }

    result = sorted(
        [{'count': v, **meta[k]} for k, v in counts.items()],
        key=lambda x: -x['count'],
    )
    return result[:30]


# ── Migration effectiveness ───────────────────────────────────────────────────

def get_migration_effectiveness(
    branch_ids: list[int] | None, days: int = 30, mode: str = 'restaurant'
) -> dict:
    """
    Computes 4 migration KPIs for the period:
    - growth:      moved to segment with lower recency (more recent)
    - cooling:     r_score dropped but not to R0
    - lost_to_r0:  moved to r_score=1 (R0)
    - reactivated: moved FROM r_score=1 to higher r_score
    """
    from datetime import date as date_type, timedelta
    from apps.tenant.analytics.models import RFSegment

    since = date_type.today() - timedelta(days=days)
    MigModel = _get_migration_model(mode)
    qs = MigModel.objects.filter(
        created_at__date__gte=since,
        from_segment__isnull=False,
        to_segment__isnull=False,
    )
    if branch_ids:
        qs = qs.filter(client__branch__in=branch_ids)

    # Map segment pk → r_score (higher = more recent; 1 = R0/lost)
    # Используем стабильное отображение по коду сегмента (R3F1 → r_score=4),
    # которое не зависит от изменения границ recency_min/recency_max
    # на конкретном RFSegment и от текущих порогов.
    seg_r_score: dict[int, int] = {}
    # Включает и общие, и per-branch сегменты — pk у каждого свой,
    # но r_score определяется только кодом.
    for s in RFSegment.objects.all():
        rf = _CODE_TO_RF.get(s.code or '')
        if rf:
            seg_r_score[s.pk] = rf[0]

    growth = cooling = lost_to_r0 = reactivated = 0

    for mig in qs.values('from_segment_id', 'to_segment_id'):
        from_r = seg_r_score.get(mig['from_segment_id'], 0)
        to_r   = seg_r_score.get(mig['to_segment_id'],   0)

        if not from_r or not to_r or from_r == to_r:
            continue                        # segment deleted or no change

        if to_r > from_r:                   # moved to fresher segment → growth
            growth += 1
        elif to_r < from_r:
            if to_r == 1:                   # landed in R0 (lost)
                lost_to_r0 += 1
            else:
                cooling += 1

        if from_r == 1 and to_r > 1:       # came back from R0 → reactivated
            reactivated += 1

    return {
        'growth':      growth,
        'cooling':     cooling,
        'lost_to_r0':  lost_to_r0,
        'reactivated': reactivated,
    }


# ── Combined RF stats ─────────────────────────────────────────────────────────

def get_rf_stats(
    branch_ids: list[int] | None,
    mode: str = 'restaurant',
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    """All RF analysis data in one dict, optionally filtered by period."""
    from datetime import date as date_type, timedelta

    today = date_type.today()
    if start_date is None:
        start_date = today - timedelta(days=29)
    if end_date is None:
        end_date = today

    # Считаем матрицу по тем же ClientBranch профилям, что попадают в QR-сканы
    # общей аналитики (per-branch профиль = строка матрицы). Это гарантирует,
    # что total RF матрицы и QR-сканы в общей аналитике совпадают.
    active_cb_ids = _get_active_client_branch_ids(branch_ids, start_date, end_date, mode)
    matrix = get_rf_matrix(branch_ids, mode=mode, client_branch_ids=active_cb_ids)

    # Derive summary cards directly from the period matrix
    total   = matrix['total']
    vip_f3  = sum(c['count'] for c in matrix['cells'].values() if c['f_score'] == 3)
    at_risk = sum(c['count'] for c in matrix['cells'].values() if c['r_score'] == 2)
    lost_r0 = sum(c['count'] for c in matrix['cells'].values() if c['r_score'] == 1)

    return {
        'matrix':     matrix,
        'summary':    {'total': total, 'vip_f3': vip_f3, 'at_risk': at_risk, 'lost_r0': lost_r0},
        'trend':      get_rf_snapshot_trend(branch_ids, mode=mode, start_date=start_date, end_date=end_date),
        'migrations': get_rf_migration_summary(branch_ids, mode=mode, start_date=start_date, end_date=end_date),
    }


# ── Migration history page ────────────────────────────────────────────────────

def get_migration_history(
    branch_ids: list[int] | None, days: int = 30, mode: str = 'restaurant',
    segment_code: str | None = None,
) -> dict:
    """Full migration history data for the migration history page."""
    flows = get_rf_migration_summary(branch_ids, days, mode)
    effectiveness = get_migration_effectiveness(branch_ids, days, mode)

    # Filter by segment if specified
    if segment_code:
        flows = [
            f for f in flows
            if f['from_code'] == segment_code or f['to_code'] == segment_code
        ]

    from apps.tenant.analytics.models import RFSegment
    all_segments = list(
        RFSegment.objects.filter(branch__isnull=True)
        .values('code', 'name', 'emoji')
        .order_by('recency_min', 'frequency_min')
    )

    return {
        'flows':         flows,
        'effectiveness': effectiveness,
        'all_segments':  all_segments,
    }


# ── RF Recalculation ──────────────────────────────────────────────────────────


def recalculate_rf_scores(
    branch_ids: list[int] | None = None,
    mode: str = 'restaurant',
) -> dict:
    """
    Synchronously recalculate RF scores for all guests in the specified branches.

    For each ClientBranch:
      - Computes recency_days and frequency from visits (restaurant) or
        delivery activations (delivery).
      - Derives r_score / f_score from per-scope thresholds (RFSettings).
      - Matches the guest to an RFSegment by (r,f) → code → RFSegment.
      - Creates or updates GuestRFScore[Delivery].
      - Logs a migration entry if the segment changed.
    After scoring, refreshes today's BranchSegmentSnapshot[Delivery].

    Пороги выбираются по правилу resolve_for_scope(branch_ids):
      • точечные настройки выбранной точки (если ровно одна и они есть);
      • иначе — настройки «Все точки» (RFSettings с branch=NULL);
      • иначе — RF_DEFAULT_THRESHOLDS.

    Returns summary dict: {updated, created, migrations, branches, duration_ms,
                            thresholds, thresholds_source}
    """
    import time
    from datetime import timedelta

    from django.db import transaction
    from django.db.models import Count, Max
    from django.utils import timezone

    from apps.tenant.analytics.models import (
        RFSegment,
        GuestRFScore, GuestRFScoreDelivery,
        RFMigrationLog, RFMigrationLogDelivery,
        BranchSegmentSnapshot, BranchSegmentSnapshotDelivery,
        RFSettings,
    )
    from apps.tenant.branch.models import Branch, ClientBranch, ClientBranchVisit

    t0 = time.monotonic()
    today = timezone.localdate()

    ScoreModel    = GuestRFScoreDelivery if mode == 'delivery' else GuestRFScore
    MigModel      = RFMigrationLogDelivery if mode == 'delivery' else RFMigrationLog
    SnapshotModel = BranchSegmentSnapshotDelivery if mode == 'delivery' else BranchSegmentSnapshot

    # ── Scope-aware thresholds ────────────────────────────────────────
    _, thresholds, source = RFSettings.resolve_for_scope(branch_ids)

    # ── Segment lookup: by code (R3F1, …) — стабильно при изменении границ. ──
    # Используем только общие сегменты (branch=NULL): GuestRFScore.segment
    # ссылается на единый набор сегментов. Per-branch сегменты применяются
    # только при отображении матрицы.
    segments = list(RFSegment.objects.filter(branch__isnull=True))
    code_to_segment = {seg.code: seg for seg in segments}

    def find_segment(recency_days: int, frequency: int):
        rs = _r_score(recency_days, thresholds)
        fs = _f_score(frequency,     thresholds)
        code = _RF_TO_CODE.get((rs, fs), '')
        return code_to_segment.get(code)

    branch_qs = Branch.objects.filter(is_active=True)
    if branch_ids:
        branch_qs = branch_qs.filter(pk__in=branch_ids)

    # ── Global analysis period: max across selected scope ─────────────
    # ВАЖНО: per-branch RFSettings имеют приоритет над «Все точки».
    # Запись «Все точки» (branch=NULL) подтягивается ТОЛЬКО как fallback
    # для тех точек выборки, у которых нет своих RFSettings. Без этого
    # частная настройка с меньшим периодом могла бы быть перебита бóльшим
    # глобальным периодом (что меняло бы число визитов в подсчёте F).
    if branch_ids:
        per_branch_qs = RFSettings.objects.filter(branch__pk__in=branch_ids)
        covered_branch_ids = set(per_branch_qs.values_list('branch_id', flat=True))
        # Включаем глобальную запись только если хотя бы одна выбранная
        # точка не имеет собственных настроек.
        need_global_fallback = any(
            bid not in covered_branch_ids for bid in branch_ids
        )
        if need_global_fallback:
            settings_qs = RFSettings.objects.filter(
                Q(branch__pk__in=branch_ids) | Q(branch__isnull=True)
            )
        else:
            settings_qs = per_branch_qs
    else:
        # branch_ids=None — режим «Все точки»: учитываем все записи.
        settings_qs = RFSettings.objects.all()

    agg = settings_qs.aggregate(max_period=Max('analysis_period'))
    analysis_period = agg['max_period'] or 365

    reset_date = None
    latest_reset = settings_qs.exclude(stats_reset_date=None).order_by('-stats_reset_date').first()
    if latest_reset:
        reset_date = latest_reset.stats_reset_date

    since = today - timedelta(days=analysis_period)
    if reset_date:
        reset_day = reset_date.date() if hasattr(reset_date, 'date') else reset_date
        if reset_day > since:
            since = reset_day

    # ── Aggregate visit/delivery data per unique guest.Client ─────────
    if mode == 'restaurant':
        from django.db.models import Exists, OuterRef, Q as _Q
        from apps.tenant.branch.models import (
            TestimonialMessage, CoinTransaction, TransactionType, TransactionSource,
        )
        from apps.tenant.game.models import ClientAttempt

        # Qualifying guest.Client IDs: those who did at least one meaningful action
        # within the analysis period (mirrors get_qr_scan_count logic).
        # VK status flags are timeless (once subscribed = always subscribed).
        # Game/review/shop actions are restricted to since..today.
        qualifying_guest_ids = ClientBranch.objects.filter(
            _Q(vk_status__community_via_app=True)
            | _Q(vk_status__newsletter_via_app=True)
            | _Q(vk_status__is_story_uploaded=True)
            | Exists(ClientAttempt.objects.filter(
                client=OuterRef('pk'),
                created_at__date__gte=since,
            ))
            | Exists(TestimonialMessage.objects.filter(
                conversation__client=OuterRef('pk'),
                source=TestimonialMessage.Source.APP,
                created_at__date__gte=since,
            ))
            | Exists(CoinTransaction.objects.filter(
                client=OuterRef('pk'),
                type=TransactionType.EXPENSE,
                source=TransactionSource.SHOP,
                created_at__date__gte=since,
            ))
        ).values('client_id')

        visit_qs = ClientBranchVisit.objects.filter(
            visited_at__date__gte=since,
            client__client_id__in=qualifying_guest_ids,
        )
        if branch_ids:
            visit_qs = visit_qs.filter(client__branch__pk__in=branch_ids)
        rows = (
            visit_qs
            .values('client__client_id')
            .annotate(freq=Count('id'), last_at=Max('visited_at'))
        )
        visit_map = {
            r['client__client_id']: {'frequency': r['freq'], 'last_at': r['last_at']}
            for r in rows
        }
    else:
        from apps.tenant.delivery.models import Delivery
        delivery_qs = Delivery.objects.filter(
            activated_at__isnull=False,
            activated_at__date__gte=since,
        )
        if branch_ids:
            delivery_qs = delivery_qs.filter(activated_by__branch__pk__in=branch_ids)
        rows = (
            delivery_qs
            .values('activated_by__client_id')
            .annotate(freq=Count('id'), last_at=Max('activated_at'))
        )
        visit_map = {
            r['activated_by__client_id']: {'frequency': r['freq'], 'last_at': r['last_at']}
            for r in rows
        }

    if not visit_map:
        return {
            'updated': 0, 'created': 0, 'migrations': 0,
            'branches': branch_qs.count(),
            'duration_ms': int((time.monotonic() - t0) * 1000),
            'thresholds':        thresholds,
            'thresholds_source': source,
        }

    # ── Load existing scores ──────────────────────────────────────────
    existing_scores = {
        s.client_id: s
        for s in ScoreModel.objects.filter(
            client_id__in=visit_map.keys(),
        ).select_related('segment')
    }

    total_updated = total_created = total_migrations = 0

    # ── Score each unique guest ───────────────────────────────────────
    with transaction.atomic():
        for guest_id, data in visit_map.items():
            last_at      = data['last_at']
            last_date    = last_at.date() if hasattr(last_at, 'date') else last_at
            recency_days = (today - last_date).days
            frequency    = data['frequency']
            r            = _r_score(recency_days, thresholds)
            f            = _f_score(frequency,     thresholds)
            segment      = find_segment(recency_days, frequency)

            existing = existing_scores.get(guest_id)
            if existing:
                old_segment = existing.segment
                existing.recency_days = recency_days
                existing.frequency    = frequency
                existing.r_score      = r
                existing.f_score      = f
                existing.segment      = segment
                existing.save(update_fields=[
                    'recency_days', 'frequency', 'r_score', 'f_score', 'segment',
                ])
                total_updated += 1
                if old_segment != segment:
                    MigModel.objects.create(
                        client_id=guest_id,
                        from_segment=old_segment,
                        to_segment=segment,
                    )
                    total_migrations += 1
            else:
                ScoreModel.objects.update_or_create(
                    client_id=guest_id,
                    defaults={
                        'recency_days': recency_days,
                        'frequency':    frequency,
                        'r_score':      r,
                        'f_score':      f,
                        'segment':      segment,
                    },
                )
                total_created += 1

        # ── Refresh today's snapshot per branch ───────────────────────
        for branch in branch_qs:
            for seg in segments:
                count = (
                    ScoreModel.objects
                    .filter(segment=seg, client__branch_profiles__branch=branch)
                    .distinct()
                    .count()
                )
                SnapshotModel.objects.update_or_create(
                    branch=branch,
                    segment=seg,
                    date=today,
                    defaults={'guests_count': count},
                )

    return {
        'updated':           total_updated,
        'created':           total_created,
        'migrations':        total_migrations,
        'branches':          branch_qs.count(),
        'duration_ms':       int((time.monotonic() - t0) * 1000),
        'thresholds':        thresholds,
        'thresholds_source': source,
    }


# ── Branch list helper ────────────────────────────────────────────────────────

def get_branches_list() -> list[dict]:
    """All branches for the filter UI."""
    from apps.tenant.branch.models import Branch

    return list(
        Branch.objects.filter(is_active=True).values('id', 'name').order_by('name')
    )


# ── Stat detail: returns ClientBranch queryset for a given metric ─────────────

def get_stat_clients(
    metric: str,
    branch_ids: list[int] | None,
    start_date: date,
    end_date: date,
):
    """
    Returns a ClientBranch queryset whose members contributed to `metric`
    in the given period. Used by StatsDetailView to render the drilldown list.

    Unsupported or non-client metrics return an empty queryset.
    """
    from django.db.models import Q as _Q
    from apps.tenant.branch.models import (
        ClientBranch, ClientBranchVisit, ClientVKStatus, CoinTransaction,
        TransactionType, TransactionSource,
    )

    base = ClientBranch.objects.select_related('client', 'branch', 'vk_status').order_by(
        'client__first_name', 'client__last_name'
    )
    if branch_ids:
        base = base.filter(branch__in=branch_ids)

    if metric == 'qr_scans':
        from django.db.models import Exists, OuterRef
        from apps.tenant.game.models import ClientAttempt
        from apps.tenant.branch.models import TestimonialMessage
        from apps.tenant.delivery.models import Delivery

        qs = ClientBranchVisit.objects.filter(
            visited_at__date__gte=start_date,
            visited_at__date__lte=end_date,
        )
        if branch_ids:
            qs = qs.filter(client__branch__in=branch_ids)
        return base.filter(
            pk__in=qs.values('client_id'),
        ).filter(
            _Q(vk_status__community_via_app=True)
            | _Q(vk_status__newsletter_via_app=True)
            | _Q(vk_status__is_story_uploaded=True)
            | Exists(ClientAttempt.objects.filter(
                client=OuterRef('pk'),
                created_at__date__gte=start_date,
                created_at__date__lte=end_date,
            ))
            | Exists(TestimonialMessage.objects.filter(
                conversation__client=OuterRef('pk'),
                source=TestimonialMessage.Source.APP,
                created_at__date__gte=start_date,
                created_at__date__lte=end_date,
            ))
            | Exists(Delivery.objects.filter(
                activated_by=OuterRef('pk'),
                activated_at__date__gte=start_date,
                activated_at__date__lte=end_date,
            ))
            | Exists(CoinTransaction.objects.filter(
                client=OuterRef('pk'),
                type=TransactionType.EXPENSE,
                source=TransactionSource.SHOP,
                created_at__date__gte=start_date,
                created_at__date__lte=end_date,
            ))
        )

    if metric == 'total_vk_subscribers':
        qs = ClientVKStatus.objects.filter(
            _Q(community_via_app=True) | _Q(newsletter_via_app=True)
        )
        if branch_ids:
            qs = qs.filter(client__branch__in=branch_ids)
        return base.filter(pk__in=qs.values('client_id'))

    if metric == 'new_community_subscribers':
        qs = ClientVKStatus.objects.filter(
            community_via_app=True,
            community_joined_at__date__gte=start_date,
            community_joined_at__date__lte=end_date,
        )
        if branch_ids:
            qs = qs.filter(client__branch__in=branch_ids)
        return base.filter(pk__in=qs.values('client_id'))

    if metric == 'new_newsletter_subscribers':
        qs = ClientVKStatus.objects.filter(
            newsletter_via_app=True,
            newsletter_joined_at__date__gte=start_date,
            newsletter_joined_at__date__lte=end_date,
        )
        if branch_ids:
            qs = qs.filter(client__branch__in=branch_ids)
        return base.filter(pk__in=qs.values('client_id'))

    if metric == 'first_gift_receivers':
        from apps.tenant.inventory.models import InventoryItem

        qs = InventoryItem.objects.all()
        if branch_ids:
            qs = qs.filter(client_branch__branch__in=branch_ids)
        first_items = (
            qs.values('client_branch')
            .annotate(first_at=Min('created_at'))
            .filter(
                first_at__date__gte=start_date,
                first_at__date__lte=end_date,
            )
        )
        cb_ids = [r['client_branch'] for r in first_items]
        return base.filter(pk__in=cb_ids)

    if metric == 'gift_activators':
        from apps.tenant.inventory.models import InventoryItem

        qs = InventoryItem.objects.filter(
            activated_at__date__gte=start_date,
            activated_at__date__lte=end_date,
        )
        if branch_ids:
            qs = qs.filter(client_branch__branch__in=branch_ids)
        return base.filter(pk__in=qs.values('client_branch_id'))

    if metric == 'coin_purchasers':
        qs = CoinTransaction.objects.filter(
            type=TransactionType.EXPENSE,
            source=TransactionSource.SHOP,
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
        )
        if branch_ids:
            qs = qs.filter(client__branch__in=branch_ids)
        return base.filter(pk__in=qs.values('client_id'))

    if metric == 'repeat_game_players':
        from apps.tenant.game.models import ClientAttempt
        from django.db.models.functions import TruncDate as _TruncDate

        qs = ClientAttempt.objects.filter(
            created_at__date__gte=start_date,
            created_at__date__lte=end_date,
        )
        if branch_ids:
            qs = qs.filter(client__branch__in=branch_ids)

        pairs = (
            qs.annotate(play_date=_TruncDate('created_at'))
            .values_list('client_id', 'play_date')
            .distinct()
        )
        client_days: dict = {}
        for cb_id, play_date in pairs:
            client_days.setdefault(cb_id, set()).add(play_date)
        cb_ids = [k for k, v in client_days.items() if len(v) >= 2]
        return base.filter(pk__in=cb_ids)

    if metric == 'new_group_with_gift':
        from apps.tenant.inventory.models import SuperPrizeEntry, SuperPrizeTrigger

        vk_qs = ClientVKStatus.objects.filter(
            _Q(community_via_app=True, community_joined_at__date__gte=start_date,
               community_joined_at__date__lte=end_date) |
            _Q(newsletter_via_app=True, newsletter_joined_at__date__gte=start_date,
               newsletter_joined_at__date__lte=end_date)
        )
        if branch_ids:
            vk_qs = vk_qs.filter(client__branch__in=branch_ids)
        sub_ids = set(vk_qs.values_list('client_id', flat=True))
        if not sub_ids:
            return base.none()

        first_prizes = (
            SuperPrizeEntry.objects
            .filter(
                acquired_from=SuperPrizeTrigger.GAME,
                client_branch__in=sub_ids,
            )
            .values('client_branch')
            .annotate(first_at=Min('created_at'))
            .filter(
                first_at__date__gte=start_date,
                first_at__date__lte=end_date,
            )
        )
        cb_ids = [r['client_branch'] for r in first_prizes]
        return base.filter(pk__in=cb_ids)

    if metric == 'birthday_celebrants':
        from datetime import timedelta
        from django.db.models import F, ExpressionWrapper, DateField
        from django.db.models.functions import ExtractMonth, ExtractDay, TruncDate
        from apps.tenant.branch.models import ClientBranchVisit

        visit_minus_grace = ExpressionWrapper(
            TruncDate('visited_at') - timedelta(days=BIRTHDAY_SET_MIN_DAYS_BEFORE_VISIT),
            output_field=DateField(),
        )

        visits_qs = ClientBranchVisit.objects.filter(
            visited_at__date__gte=start_date,
            visited_at__date__lte=end_date,
            client__birth_date__isnull=False,
            client__birth_date_set_at__isnull=False,
        ).annotate(
            visit_month=ExtractMonth('visited_at'),
            visit_day=ExtractDay('visited_at'),
            birth_month=ExtractMonth('client__birth_date'),
            birth_day=ExtractDay('client__birth_date'),
            visit_minus_grace=visit_minus_grace,
        ).filter(
            visit_month=F('birth_month'),
            visit_day=F('birth_day'),
            client__birth_date_set_at__lte=F('visit_minus_grace'),
        )
        if branch_ids:
            visits_qs = visits_qs.filter(client__branch__in=branch_ids)
        cb_ids = visits_qs.values_list('client_id', flat=True).distinct()
        return base.filter(pk__in=cb_ids)

    if metric == 'vk_stories_publishers':
        qs = ClientVKStatus.objects.filter(
            is_story_uploaded=True,
            story_uploaded_at__date__gte=start_date,
            story_uploaded_at__date__lte=end_date,
        )
        if branch_ids:
            qs = qs.filter(client__branch__in=branch_ids)
        return base.filter(pk__in=qs.values('client_id'))

    return base.none()