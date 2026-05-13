import hmac
import os
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.tenant.branch.models import Branch, ClientBranch
from ..models import Delivery, OrderSource, _NO_EXPIRY_DAYS


# ── Exceptions ────────────────────────────────────────────────────────────────

class BranchNotFound(Exception):
    pass


class ClientNotFound(Exception):
    pass


class DeliveryNotFound(Exception):
    """No valid pending delivery: wrong code, expired, or already taken."""
    pass


# ── Auth ──────────────────────────────────────────────────────────────────────

def verify_webhook_signature(request) -> bool:
    """
    Validates the X-Webhook-Secret header against the DELIVERY_WEBHOOK_SECRET
    environment variable using constant-time comparison (timing-attack safe).

    If DELIVERY_WEBHOOK_SECRET is not set, verification is skipped and all
    requests are allowed — useful for local development.
    """
    secret = os.getenv('DELIVERY_WEBHOOK_SECRET', '')
    if not secret:
        return True
    received = request.headers.get('X-Webhook-Secret', '')
    return hmac.compare_digest(
        received.encode('utf-8'),
        secret.encode('utf-8'),
    )


# ── Public service functions ──────────────────────────────────────────────────

@transaction.atomic
def register_delivery(*, source: str, branch_id: str, code: str) -> tuple[Delivery, bool]:
    """
    Registers a delivery code received from a POS webhook.

    branch_id is the POS-system's own identifier:
      iiko    → Branch.iiko_organization_id (UUID string)
      dooglys → Branch.dooglys_branch_id    (integer)

    Returns:
        (delivery, True)  — new record created  (HTTP 201)
        (delivery, False) — code already exists (HTTP 200, idempotent)

    Raises:
        BranchNotFound — no Branch matches source + branch_id
    """
    try:
        if source == OrderSource.DOOGLYS:
            branch = Branch.objects.get(dooglys_branch_id=int(branch_id))
        else:  # OrderSource.IIKO — already validated as a ChoiceField
            branch = Branch.objects.get(iiko_organization_id=branch_id)
    except (Branch.DoesNotExist, ValueError, TypeError):
        raise BranchNotFound

    delivery, created = Delivery.objects.get_or_create(
        code=code,
        defaults={'branch': branch, 'order_source': source},
    )

    # Dooglys reuses the same code after expiry or activation.
    # Reset the delivery to pending so the new order is treated as fresh.
    if not created and delivery.status in ('expired', 'activated'):
        delivery.activated_at = None
        delivery.activated_by = None
        # Pending bez vremennogo limita: выставляем «бесконечное» окно.
        delivery.expires_at = timezone.now() + timedelta(days=_NO_EXPIRY_DAYS)
        delivery.save(update_fields=['activated_at', 'activated_by', 'expires_at'])
        return delivery, True

    return delivery, created


@transaction.atomic
def activate_delivery(*, short_code: str, vk_id: int, branch_id: int) -> Delivery:
    """
    Activates a pending delivery code for a guest.

    Idempotent: if the same client already activated this short_code on this
    branch, returns the existing delivery without error.

    SELECT FOR UPDATE on the Delivery row prevents two clients from
    activating the same code simultaneously (race condition).

    Raises:
        ClientNotFound   — no ClientBranch for (vk_id, branch_id)
        DeliveryNotFound — no valid pending delivery found; reasons:
                           wrong short_code / expired / already taken by
                           another client
    """
    try:
        client_branch = ClientBranch.objects.select_related('branch').get(
            client__vk_id=vk_id, branch__branch_id=branch_id,
        )
    except ClientBranch.DoesNotExist:
        raise ClientNotFound

    # Idempotency: same client re-submits the same code → return existing
    already = Delivery.objects.filter(
        branch=client_branch.branch,
        short_code=short_code,
        activated_by=client_branch,
    ).first()
    if already:
        return already

    # Lock the row to prevent double-activation under concurrent requests.
    # Одноразовость обеспечивается activated_at__isnull=True: после первой
    # активации повторная попытка не пройдёт фильтр. Окно по времени снято.
    delivery = (
        Delivery.objects
        .select_for_update()
        .filter(
            branch=client_branch.branch,
            short_code=short_code,
            activated_at__isnull=True,
        )
        .order_by('-created_at')
        .first()
    )

    if delivery is None:
        raise DeliveryNotFound

    delivery.activate(client_branch)
    return delivery
