from datetime import date, timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.tenant.branch.models import (
    ClientBranch,
    Cooldown, CooldownFeature,
    DailyCode, DailyCodePurpose,
    current_code_date,
)
from apps.tenant.catalog.models import Product
from ..models import (
    AcquisitionSource, InventoryItem, ItemStatus,
    SuperPrizeEntry, SuperPrizeTrigger,
)

# ── Exceptions ────────────────────────────────────────────────────────────────

BIRTHDAY_WINDOW_DAYS    = 5    # ±days around the birthday
BIRTHDAY_MIN_AGE_DAYS   = 30   # birth_date must have been set at least this many days ago


class ClientNotFound(Exception):
    pass


class InventoryItemNotFound(Exception):
    pass


class SuperPrizeNotFound(Exception):
    pass


class ProductNotFound(Exception):
    pass


class AlreadyClaimed(Exception):
    pass


class AlreadyActivated(Exception):
    pass


class NotBirthdayWindow(Exception):
    pass


class BirthdayTooRecent(Exception):
    """birth_date was set less than BIRTHDAY_MIN_AGE_DAYS ago — anti-abuse guard."""
    pass


class InvalidCode(Exception):
    pass


class InventoryCooldownActive(Exception):
    """Carries the active cooldown so the view can return seconds_remaining."""
    def __init__(self, cooldown: Cooldown):
        self.cooldown = cooldown


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_client_branch(vk_id: int, branch_id: int) -> ClientBranch:
    try:
        return ClientBranch.objects.select_related('branch').get(
            client__vk_id=vk_id, branch__branch_id=branch_id,
        )
    except ClientBranch.DoesNotExist:
        raise ClientNotFound


def _is_in_birthday_window(birth_date: date, today: date, window_days: int | None = None) -> bool:
    """
    True if today is within ±window_days of the birthday (year-agnostic).
    window_days=None → use module-level fallback BIRTHDAY_WINDOW_DAYS.
    """
    if window_days is None:
        window_days = BIRTHDAY_WINDOW_DAYS
    bd = birth_date.replace(year=today.year)
    delta = (today - bd).days
    # Handle year-wrap (e.g. birthday Dec 28, today Jan 2)
    if delta < -180:
        delta += 365
    elif delta > 180:
        delta -= 365
    return abs(delta) <= window_days


def _network_birthday_window() -> int | None:
    """Сетевое значение окна ДР из ClientConfig текущего тенанта (или None)."""
    try:
        from django.db import connection
        from apps.shared.config.models import ClientConfig
        company = getattr(connection, 'tenant', None)
        if company is None:
            return None
        cfg = ClientConfig.objects.filter(company=company).first()
        return cfg.birthday_window_days if cfg else None
    except Exception:
        return None


def _branch_birthday_window(cb: ClientBranch) -> int:
    """
    Резолв окна подарка ДР (LU-13): точка → сеть → хардкод.
    - BranchConfig.birthday_window_days (если задано на точке)
    - ClientConfig.birthday_window_days (сетевое значение)
    - BIRTHDAY_WINDOW_DAYS (константа-фолбэк)
    """
    cfg = getattr(cb.branch, 'config', None)
    if cfg is not None and getattr(cfg, 'birthday_window_days', None) is not None:
        return cfg.birthday_window_days
    net = _network_birthday_window()
    if net is not None:
        return net
    return BIRTHDAY_WINDOW_DAYS


def _birth_date_is_established(cb: ClientBranch) -> bool:
    """
    True if birth_date has been set for at least BIRTHDAY_MIN_AGE_DAYS.

    NULL birth_date_set_at means the field was added after this profile was
    created (existing users) — we treat them as established (grandfathered).
    """
    if cb.birth_date_set_at is None:
        return True
    return (timezone.localdate() - cb.birth_date_set_at).days >= BIRTHDAY_MIN_AGE_DAYS


def _check_birthday_eligibility(cb: ClientBranch) -> None:
    """
    Raises:
        NotBirthdayWindow  — birth_date not set or today not within ±5 days
        BirthdayTooRecent  — birth_date was set less than 30 days ago (anti-abuse)
    """
    if not cb.birth_date or not _is_in_birthday_window(
        cb.birth_date, timezone.localdate(), _branch_birthday_window(cb)
    ):
        raise NotBirthdayWindow
    if not _birth_date_is_established(cb):
        raise BirthdayTooRecent


def _validate_birthday_code(branch, code: str | None) -> None:
    if not code:
        raise InvalidCode
    daily = DailyCode.objects.filter(
        branch=branch,
        purpose=DailyCodePurpose.BIRTHDAY,
        valid_date=current_code_date(),
    ).first()
    if not daily or daily.code != code.upper().strip():
        raise InvalidCode


def _get_inventory_cooldown(client_branch: ClientBranch) -> Cooldown | None:
    return Cooldown.objects.filter(
        client=client_branch, feature=CooldownFeature.INVENTORY,
    ).first()


def _activate_inventory_cooldown(client_branch: ClientBranch) -> None:
    now = timezone.now()
    cooldown, created = Cooldown.objects.get_or_create(
        client=client_branch,
        feature=CooldownFeature.INVENTORY,
        defaults={
            'last_activated_at': now,
            'expires_at': now + timedelta(hours=18),
        },
    )
    if not created:
        cooldown.activate()


# ── Public service functions ──────────────────────────────────────────────────

def get_inventory(vk_id: int, branch_id: int):
    """All InventoryItems for the guest, most recent first."""
    cb = _get_client_branch(vk_id, branch_id)
    return (
        InventoryItem.objects
        .filter(client_branch=cb)
        .select_related('product')
        .order_by('-created_at')
    )


def get_super_prizes(vk_id: int, branch_id: int):
    """Pending SuperPrizeEntries for the guest (not claimed and not expired)."""
    cb = _get_client_branch(vk_id, branch_id)
    return (
        SuperPrizeEntry.objects
        .filter(client_branch=cb, claimed_at__isnull=True)
        .exclude(expires_at__lt=timezone.now())
        .select_related('product')
        .order_by('-created_at')
    )


@transaction.atomic
def claim_super_prize(vk_id: int, branch_id: int, product_id: int) -> SuperPrizeEntry:
    """
    Guest selects a product from their pending super prize pool.

    Finds the oldest pending SuperPrizeEntry and calls entry.claim(product).

    Raises:
        ClientNotFound    — no profile for (vk_id, branch_id)
        SuperPrizeNotFound — no pending SuperPrizeEntry for this client
        ProductNotFound   — product doesn't exist, is inactive, or not a super prize
    """
    try:
        cb = ClientBranch.objects.select_related('branch').get(
            client__vk_id=vk_id, branch__branch_id=branch_id,
        )
    except ClientBranch.DoesNotExist:
        raise ClientNotFound

    entry = (
        SuperPrizeEntry.objects
        .select_for_update()
        .filter(client_branch=cb, claimed_at__isnull=True)
        .exclude(expires_at__lt=timezone.now())  # skip expired
        .order_by('created_at')
        .first()
    )
    if entry is None:
        raise SuperPrizeNotFound

    try:
        product = Product.objects.get(
            pk=product_id, branch_assignments__branch=cb.branch, is_super_prize=True, is_archived=False,
        )
    except Product.DoesNotExist:
        raise ProductNotFound

    entry.claim(product)

    InventoryItem.objects.create(
        client_branch=cb,
        product=product,
        acquired_from=AcquisitionSource.SUPER_PRIZE,
    )

    return entry


def get_inventory_cooldown(vk_id: int, branch_id: int) -> Cooldown | None:
    """Current INVENTORY cooldown for the guest, or None if never activated."""
    return _get_inventory_cooldown(_get_client_branch(vk_id, branch_id))


@transaction.atomic
def activate_inventory_cooldown(vk_id: int, branch_id: int) -> Cooldown:
    """
    Manually creates or restarts the INVENTORY cooldown (admin / debug use).

    Raises:
        ClientNotFound — no profile for (vk_id, branch_id)
    """
    cb = _get_client_branch(vk_id, branch_id)
    _activate_inventory_cooldown(cb)
    return Cooldown.objects.get(client=cb, feature=CooldownFeature.INVENTORY)


@transaction.atomic
def activate_item(
    vk_id: int,
    branch_id: int,
    item_id: int,
    code: str | None = None,
) -> InventoryItem:
    """
    Activates a pending InventoryItem so the guest can show it to staff.

    Birthday items  → require today's birthday DailyCode; no cooldown applied.
    All other items → check INVENTORY cooldown is not active; set it after activate.

    Raises:
        ClientNotFound         — no profile for (vk_id, branch_id)
        InventoryItemNotFound  — item doesn't exist or belongs to another client
        AlreadyActivated       — item is not in PENDING state
        InvalidCode            — birthday item but code wrong or not supplied
        InventoryCooldownActive — non-birthday item and cooldown is still running
    """
    try:
        cb = (
            ClientBranch.objects
            .select_for_update()
            .select_related('branch')
            .get(client__vk_id=vk_id, branch__branch_id=branch_id)
        )
    except ClientBranch.DoesNotExist:
        raise ClientNotFound

    try:
        item = (
            InventoryItem.objects
            .select_for_update(of=('self',))
            .select_related('product')
            .get(pk=item_id, client_branch=cb)
        )
    except InventoryItem.DoesNotExist:
        raise InventoryItemNotFound

    if item.status != ItemStatus.PENDING:
        raise AlreadyActivated

    if item.acquired_from == AcquisitionSource.BIRTHDAY:
        _validate_birthday_code(cb.branch, code)
        item.activate()
    else:
        cooldown = _get_inventory_cooldown(cb)
        if cooldown and cooldown.is_active:
            raise InventoryCooldownActive(cooldown)
        item.activate()
        _activate_inventory_cooldown(cb)

    return InventoryItem.objects.select_related('product').get(pk=item.pk)


def get_birthday_status(vk_id: int, branch_id: int) -> dict:
    """
    Returns the guest's current birthday status for the frontend.

    Fields:
      is_birthday_window — today is within ±5 days of birth_date
      already_claimed    — a birthday InventoryItem exists for the current year
      can_claim          — all conditions met: window + not claimed + established

    The frontend should show the "Happy Birthday" banner when `is_birthday_window`
    is True and the claim button only when `can_claim` is True.
    """
    cb = _get_client_branch(vk_id, branch_id)

    if not cb.birth_date:
        return {'is_birthday_window': False, 'already_claimed': False, 'can_claim': False}

    today = timezone.localdate()
    in_window = _is_in_birthday_window(cb.birth_date, today, _branch_birthday_window(cb))
    already_claimed = InventoryItem.objects.filter(
        client_branch__client=cb.client,
        acquired_from=AcquisitionSource.BIRTHDAY,
        created_at__year=today.year,
    ).exists()
    established = _birth_date_is_established(cb)
    can_claim = in_window and not already_claimed and established

    return {
        'is_birthday_window': in_window,
        'already_claimed':    already_claimed,
        'can_claim':          can_claim,
    }


def get_birthday_products(vk_id: int, branch_id: int):
    """
    Returns the pool of birthday products available for this guest.

    Raises:
        ClientNotFound     — no profile
        NotBirthdayWindow  — not within ±5 days of birthday
        BirthdayTooRecent  — birth_date set less than 30 days ago (anti-abuse)
        AlreadyClaimed     — already claimed a birthday prize this year
    """
    cb = _get_client_branch(vk_id, branch_id)
    _check_birthday_eligibility(cb)

    today = timezone.localdate()
    if InventoryItem.objects.filter(
        client_branch__client=cb.client,
        acquired_from=AcquisitionSource.BIRTHDAY,
        created_at__year=today.year,
    ).exists():
        raise AlreadyClaimed

    return (
        Product.objects
        .filter(branch_assignments__branch=cb.branch, is_birthday_prize=True, is_archived=False)
        .annotate(branch_ordering=F('branch_assignments__ordering'))
        .order_by('branch_ordering', 'name')
    )


@transaction.atomic
def claim_birthday_prize(vk_id: int, branch_id: int, product_id: int) -> InventoryItem:
    """
    Creates a pending InventoryItem for the guest's birthday prize.

    The item starts in PENDING state.  The guest activates it in the café via
    activate_item() using the daily birthday code — no INVENTORY cooldown applied.

    Raises:
        ClientNotFound     — no profile
        NotBirthdayWindow  — not within ±5 days of birthday
        BirthdayTooRecent  — birth_date set less than 30 days ago (anti-abuse)
        AlreadyClaimed     — already claimed a birthday prize this year
        ProductNotFound    — product not found or not a birthday prize
    """
    try:
        cb = (
            ClientBranch.objects
            .select_for_update()
            .select_related('branch')
            .get(client__vk_id=vk_id, branch__branch_id=branch_id)
        )
    except ClientBranch.DoesNotExist:
        raise ClientNotFound

    _check_birthday_eligibility(cb)

    today = timezone.localdate()
    if InventoryItem.objects.filter(
        client_branch__client=cb.client,
        acquired_from=AcquisitionSource.BIRTHDAY,
        created_at__year=today.year,
    ).exists():
        raise AlreadyClaimed

    try:
        product = Product.objects.get(
            pk=product_id, branch_assignments__branch=cb.branch, is_birthday_prize=True, is_archived=False,
        )
    except Product.DoesNotExist:
        raise ProductNotFound

    return InventoryItem.objects.create(
        client_branch=cb,
        product=product,
        acquired_from=AcquisitionSource.BIRTHDAY,
    )
