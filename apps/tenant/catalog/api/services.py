from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.tenant.branch.models import (
    Branch, ClientBranch,
    Cooldown, CooldownFeature,
    CoinTransaction, TransactionType, TransactionSource,
)
from apps.tenant.inventory.models import AcquisitionSource, InventoryItem
from ..models import Product


# ── Exceptions ────────────────────────────────────────────────────────────────

class BranchNotFound(Exception):
    pass


class BranchInactive(Exception):
    pass


class ClientNotFound(Exception):
    pass


class ProductNotFound(Exception):
    pass


class ShopOnCooldown(Exception):
    pass


class InsufficientBalance(Exception):
    pass


# ── Internal helpers ──────────────────────────────────────────────────────────

def _image_url(field) -> str | None:
    return field.url if (field and field.name) else None


def _get_client_branch(vk_id: int, branch_id: int) -> ClientBranch:
    try:
        return ClientBranch.objects.select_related('branch').get(
            client__vk_id=vk_id, branch__branch_id=branch_id,
        )
    except ClientBranch.DoesNotExist:
        raise ClientNotFound


# ── Public service functions ──────────────────────────────────────────────────

def get_active_products(branch_id: int):
    """
    Returns active products for the branch, ordered by category/product ordering.

    Raises:
        BranchNotFound  — branch_id doesn't exist
        BranchInactive  — branch is disabled
    """
    try:
        branch = Branch.objects.get(branch_id=branch_id)
    except Branch.DoesNotExist:
        raise BranchNotFound

    if not branch.is_active:
        raise BranchInactive

    return (
        Product.objects
        .filter(branch_assignments__branch=branch, branch_assignments__is_active=True, is_archived=False)
        .annotate(
            branch_category_id=F('branch_assignments__category_id'),
            branch_category_name=F('branch_assignments__category__name'),
            branch_category_ordering=F('branch_assignments__category__ordering'),
            branch_ordering=F('branch_assignments__ordering'),
        )
        .order_by('branch_category_ordering', 'branch_ordering', 'name')
    )


def get_shop_cooldown(vk_id: int, branch_id: int) -> Cooldown | None:
    """
    Returns the SHOP Cooldown for the given guest, or None if never activated.

    Raises:
        ClientNotFound — no profile for (vk_id, branch_id)
    """
    client_branch = _get_client_branch(vk_id, branch_id)
    return Cooldown.objects.filter(
        client=client_branch, feature=CooldownFeature.SHOP,
    ).first()


@transaction.atomic
def activate_shop_cooldown(vk_id: int, branch_id: int) -> Cooldown:
    """
    Creates or restarts the SHOP cooldown for the given guest.

    Raises:
        ClientNotFound — no profile for (vk_id, branch_id)
    """
    client_branch = _get_client_branch(vk_id, branch_id)
    now = timezone.now()
    cooldown, created = Cooldown.objects.get_or_create(
        client=client_branch,
        feature=CooldownFeature.SHOP,
        defaults={'last_activated_at': now, 'expires_at': now + timedelta(hours=18)},
    )
    if not created:
        cooldown.activate()
    return cooldown


@transaction.atomic
def buy_product(vk_id: int, branch_id: int, product_id: int) -> InventoryItem:
    """
    Atomically purchases a product for a guest.

    Flow:
      1. Lock ClientBranch row (SELECT FOR UPDATE) — serializes concurrent purchases
      2. Check SHOP cooldown is not active
      3. Validate product: exists, active, belongs to the same branch
      4. Deduct coins via CoinTransactionManager (balance check inside)
      5. Create InventoryItem (acquired_from=PURCHASE)
      6. Create or restart SHOP cooldown

    Raises:
        ClientNotFound      — no profile for (vk_id, branch_id)
        ProductNotFound     — product doesn't exist, is inactive, or wrong branch
        ShopOnCooldown      — SHOP cooldown is still running
        InsufficientBalance — not enough coins (only when price > 0)
    """
    # 1. Lock the row — prevents concurrent purchases from the same client
    try:
        client_branch = (
            ClientBranch.objects
            .select_for_update()
            .select_related('branch')
            .get(client__vk_id=vk_id, branch__branch_id=branch_id)
        )
    except ClientBranch.DoesNotExist:
        raise ClientNotFound

    # 2. Check cooldown
    cooldown = Cooldown.objects.filter(
        client=client_branch, feature=CooldownFeature.SHOP,
    ).first()
    if cooldown and cooldown.is_active:
        raise ShopOnCooldown

    # 3. Validate product
    try:
        product = Product.objects.get(
            pk=product_id,
            branch_assignments__branch=client_branch.branch,
            branch_assignments__is_active=True,
            is_archived=False,
        )
    except Product.DoesNotExist:
        raise ProductNotFound

    # 4. Deduct coins (CoinTransactionManager checks balance internally)
    if product.price > 0:
        try:
            CoinTransaction.objects.create_transfer(
                client_branch=client_branch,
                amount=product.price,
                type=TransactionType.EXPENSE,
                source=TransactionSource.SHOP,
                description=str(product.pk),
            )
        except ValidationError:
            raise InsufficientBalance

    # 5. Create inventory item
    item = InventoryItem.objects.create(
        client_branch=client_branch,
        product=product,
        acquired_from=AcquisitionSource.PURCHASE,
    )

    # 6. Activate shop cooldown
    now = timezone.now()
    cooldown_obj, created = Cooldown.objects.get_or_create(
        client=client_branch,
        feature=CooldownFeature.SHOP,
        defaults={'last_activated_at': now, 'expires_at': now + timedelta(hours=18)},
    )
    if not created:
        cooldown_obj.activate()

    return InventoryItem.objects.select_related('product').get(pk=item.pk)
