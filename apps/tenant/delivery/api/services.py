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


# Сетевой резолв ищет код только среди свежих доставок (дней). Гость сканирует
# коробку в день заказа; ограничение убирает ложные коллизии со старым пулом.
NETWORK_RESOLVE_WINDOW_DAYS = 7


class AmbiguousDeliveryCode(Exception):
    """
    Один и тот же short_code (последние 5 цифр) оказался pending сразу у НЕСКОЛЬКИХ
    точек тенанта — по нему нельзя однозначно определить точку. Несёт список
    точек-кандидатов, чтобы фронт показал выбор. Крайне редкий случай (коллизия
    5 цифр), но должен обрабатываться, иначе активируем не ту доставку.
    """
    def __init__(self, branches):
        self.branches = branches


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
            client__vk_id=vk_id, branch__branch_id=branch_id, client__is_active=True,
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

    # Ретро-атрибуция источника подписки: типичный доставочный флоу — гость
    # подписался при онбординге, ПОТОМ ввёл код доставки. На момент подписки
    # активной доставки ещё не было → источник проставился cafe. Здесь, при
    # активации, перебиваем cafe→delivery, если подписка была в окне ±1 день.
    _reattribute_subscription_to_delivery(client_branch, delivery.activated_at)

    return delivery


@transaction.atomic
def resolve_and_activate_network_delivery(*, short_code: str, vk_id: int):
    """
    СЕТЕВАЯ активация кода доставки (для тенант-QR «один на всю сеть»).

    Гость вводит код БЕЗ выбора точки — точку определяем по самому коду: ищем
    pending-доставку с этим short_code среди ВСЕХ активных точек тенанта. Затем
    регистрируем гостя на найденной точке (source=delivery) и активируем — через
    ТОТ ЖЕ activate_delivery, что и обычный поток (одна логика, один анти-фрод).

    Возвращает (branch, delivery).

    Raises:
        DeliveryNotFound       — нет pending-доставки с таким кодом нигде в сети.
        AmbiguousDeliveryCode  — код pending сразу у нескольких точек (коллизия
                                 5 цифр) → фронт должен показать выбор точки.

    ⚠️ Старый пер-точечный activate_delivery/эндпоинт НЕ трогается — это отдельный
    аддитивный путь только для тенант-QR.
    """
    # Идемпотентность: этот гость уже активировал доставку с таким кодом —
    # возвращаем ту же точку, не пытаясь активировать заново.
    already = (
        Delivery.objects
        .select_related('branch')
        .filter(short_code=short_code, activated_by__client__vk_id=vk_id)
        .order_by('-activated_at')
        .first()
    )
    if already:
        return already.branch, already

    # ⚠️ ОКНО СВЕЖЕСТИ — критично для сетевого резолва. Код гостя всегда из
    # недавнего заказа (сканирует коробку в день доставки). Если искать по ВСЕЙ
    # истории, копящиеся годами pending-доставки (у Орла/Брянска 50-58 тысяч,
    # заполняют половину пространства 5 цифр) дают ложные коллизии last-5 между
    # точками: 13-19% по всей истории → <0.6% за 7 дней. Пер-точечный
    # activate_delivery окно НЕ использует (там точка фиксирована QR, коллизий нет).
    cutoff = timezone.now() - timedelta(days=NETWORK_RESOLVE_WINDOW_DAYS)
    pending = list(
        Delivery.objects
        .select_related('branch')
        .filter(
            short_code=short_code,
            activated_at__isnull=True,
            created_at__gte=cutoff,
            branch__is_active=True,
        )
    )
    branches = list({d.branch_id: d.branch for d in pending}.values())

    if not branches:
        raise DeliveryNotFound
    if len(branches) > 1:
        # Коллизия: один код у нескольких точек — пусть гость уточнит точку.
        raise AmbiguousDeliveryCode(branches)

    branch = branches[0]
    # Ленивый импорт — избегаем цикла branch.services ↔ delivery.services.
    from apps.tenant.branch.api.services import register_or_get_client
    # Гарантируем ClientBranch на этой точке (доставочный вход — визит НЕ пишется).
    register_or_get_client(vk_id=vk_id, branch_id=branch.branch_id, source='delivery')
    # Активируем через штатную функцию — та же идемпотентность и ретро-атрибуция.
    delivery = activate_delivery(short_code=short_code, vk_id=vk_id, branch_id=branch.branch_id)
    return branch, delivery


def _reattribute_subscription_to_delivery(client_branch, activated_at) -> None:
    """cafe→delivery для via_app-подписок гостя в окне ±1 день вокруг активации
    доставки (тот же визит). Не трогает story и уже-delivery."""
    from datetime import timedelta
    from apps.tenant.branch.models import ClientVKStatus, SubscriptionSource

    if not activated_at:
        return
    vk = ClientVKStatus.objects.filter(client=client_branch).first()
    if not vk:
        return
    lo = activated_at - timedelta(days=1)
    hi = activated_at + timedelta(days=1)
    fields = []
    if (vk.community_via_app and vk.community_source in (None, SubscriptionSource.CAFE)
            and vk.community_joined_at and lo <= vk.community_joined_at <= hi):
        vk.community_source = SubscriptionSource.DELIVERY
        fields.append('community_source')
    if (vk.newsletter_via_app and vk.newsletter_source in (None, SubscriptionSource.CAFE)
            and vk.newsletter_joined_at and lo <= vk.newsletter_joined_at <= hi):
        vk.newsletter_source = SubscriptionSource.DELIVERY
        fields.append('newsletter_source')
    if fields:
        vk.save(update_fields=fields)
