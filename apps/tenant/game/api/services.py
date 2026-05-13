from datetime import timedelta

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.tenant.branch.models import (
    ClientBranch, ClientVKStatus,
    CoinTransaction, TransactionType, TransactionSource,
    Cooldown, CooldownFeature,
    DailyCode, DailyCodePurpose,
)
from apps.tenant.inventory.models import SuperPrizeEntry, SuperPrizeTrigger
from ..models import ClientAttempt


# ── Exceptions ────────────────────────────────────────────────────────────────

class ClientNotFound(Exception):
    pass


class GameCooldownActive(Exception):
    """Carries the active cooldown so the view can return seconds_remaining."""
    def __init__(self, cooldown: Cooldown):
        self.cooldown = cooldown


class CodeRequired(Exception):
    pass


class InvalidCode(Exception):
    pass


class InvalidToken(Exception):
    pass


class DeliveryCodeNotActivated(Exception):
    pass


class VKSubscriptionRequired(Exception):
    """Guest must subscribe to VK community AND newsletter before claiming."""
    def __init__(
        self,
        is_community_member: bool,
        is_newsletter_subscriber: bool,
        prize_preview: dict,
    ):
        self.is_community_member = is_community_member
        self.is_newsletter_subscriber = is_newsletter_subscriber
        self.prize_preview = prize_preview


# ── Reward table ──────────────────────────────────────────────────────────────

# attempt_number → (coins, animation_score 1–10)
_COIN_REWARDS: dict[int, tuple[int, int]] = {
    2: (2000, 9),
    3: (700,  7),
    4: (300,  6),
}
_DEFAULT_COIN_REWARD: tuple[int, int] = (1000, 8)


def _coin_reward_for(attempt_num: int) -> tuple[int, int]:
    """Returns (coins, animation_score 1–10) for the given attempt number."""
    return _COIN_REWARDS.get(attempt_num, _DEFAULT_COIN_REWARD)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_client_branch(vk_id: int, branch_id: int) -> ClientBranch:
    try:
        return ClientBranch.objects.select_related('branch').get(
            client__vk_id=vk_id, branch__branch_id=branch_id,
        )
    except ClientBranch.DoesNotExist:
        raise ClientNotFound


def _get_game_cooldown(client_branch: ClientBranch) -> Cooldown | None:
    return Cooldown.objects.filter(
        client=client_branch, feature=CooldownFeature.GAME,
    ).first()


def _activate_game_cooldown(client_branch: ClientBranch) -> None:
    now = timezone.now()
    cooldown, created = Cooldown.objects.get_or_create(
        client=client_branch,
        feature=CooldownFeature.GAME,
        defaults={
            'last_activated_at': now,
            'expires_at': now + timedelta(hours=18),
        },
    )
    if not created:
        cooldown.activate()


def _validate_game_code(branch, code: str) -> None:
    today = timezone.localdate()
    daily = DailyCode.objects.filter(
        branch=branch,
        purpose=DailyCodePurpose.GAME,
        valid_date=today,
    ).first()
    if not daily or daily.code != code.upper().strip():
        raise InvalidCode


# ── Public service functions ──────────────────────────────────────────────────

def get_game_cooldown(vk_id: int, branch_id: int) -> Cooldown | None:
    """Current GAME cooldown for the guest, or None if never activated."""
    return _get_game_cooldown(_get_client_branch(vk_id, branch_id))


def reset_game_cooldown(vk_id: int, branch_id: int) -> None:
    """Delete the GAME cooldown so the guest can play again immediately."""
    cb = _get_client_branch(vk_id, branch_id)
    Cooldown.objects.filter(client=cb, feature=CooldownFeature.GAME).delete()


def start_game(vk_id: int, branch_id: int, code: str | None = None, delivery: bool = False) -> dict:
    """
    Phase 1 — validate eligibility and pre-determine the reward.

    Returns a short-lived signed token (10 min TTL) and an animation `score`
    (1–10).  The frontend uses `score` to drive the rocket animation height
    without knowing the actual reward.  Call claim_game() once the animation
    completes to reveal and record the reward.

    Reward table:
      attempt 1 → SuperPrize (score 10)
      attempt 2 → 2000 coins (score  9), no code needed
      attempt 3 → 700  coins (score  7), code required*
      attempt 4 → 300  coins (score  6), code required*
      attempt 5+ → 1000 coins (score 8), code required*
      * unless the guest activated a delivery code (delivery bypass)
        or delivery=True was passed as a query parameter

    Returns:
        {'session_token': str, 'score': int}

    Raises:
        ClientNotFound     — no profile for (vk_id, branch_id)
        GameCooldownActive — still within the 18-hour cooldown
        CodeRequired       — attempt ≥ 3, not a delivery user, code not supplied
        InvalidCode        — code supplied but doesn't match today's game code
    """
    client_branch = _get_client_branch(vk_id, branch_id)

    cooldown = _get_game_cooldown(client_branch)
    if cooldown and cooldown.is_active:
        raise GameCooldownActive(cooldown)

    attempt_count = ClientAttempt.objects.filter(client=client_branch).count()
    attempt_num   = attempt_count + 1

    # Attempt 1 → super prize
    # Idempotency guard: skip if a GAME super prize was already awarded
    already_has_super = client_branch.super_prizes.filter(
        acquired_from=SuperPrizeTrigger.GAME,
    ).exists()

    if attempt_num == 1 and not already_has_super:
        token = signing.dumps({
            'cbid':  client_branch.pk,
            'count': attempt_count,
            'rt':    'sp',          # reward type: super_prize
            'dl':    delivery,      # delivery flag
        })
        return {'session_token': token, 'score': 10}

    # Attempt 3+ → require daily code unless the guest came via delivery
    if attempt_num >= 3:
        has_active_delivery = client_branch.activated_deliveries.filter(
            expires_at__gt=timezone.now(),
        ).exists()
        is_delivery = delivery or has_active_delivery
        if not is_delivery:
            if not code:
                raise CodeRequired
            _validate_game_code(client_branch.branch, code)

    coins, score = _coin_reward_for(attempt_num)
    token = signing.dumps({
        'cbid':  client_branch.pk,
        'count': attempt_count,
        'rt':    'c',       # reward type: coin
        'ra':    coins,     # reward amount
        'dl':    delivery,  # delivery flag
    })
    return {'session_token': token, 'score': score}


@transaction.atomic
def claim_game(session_token: str, employee_id: int | None = None, delivery: bool = False) -> dict:
    """
    Phase 2 — reveal the reward, record the attempt, activate the cooldown.

    Replay-safe: the attempt count embedded in the token must match the
    current DB count (SELECT FOR UPDATE on ClientBranch prevents races when
    two concurrent requests carry the same token).

    Token expires after 10 minutes (signing.BadSignature raised if expired).

    If the session was started with delivery=True, the delivery code is
    activated separately by the guest via POST /api/v1/code/ after claiming.

    Returns:
        {'type': 'coin',        'reward': int}
        {'type': 'super_prize', 'reward': SuperPrizeEntry}

    Raises:
        InvalidToken — tampered, expired, already claimed, or count mismatch
    """
    try:
        payload = signing.loads(session_token, max_age=600)   # 10-minute TTL
    except signing.BadSignature:
        raise InvalidToken

    try:
        client_branch = (
            ClientBranch.objects
            .select_for_update()
            .select_related('branch')
            .get(pk=payload['cbid'])
        )
    except (ClientBranch.DoesNotExist, KeyError):
        raise InvalidToken

    # Replay protection: count must not have changed since start_game()
    current_count = ClientAttempt.objects.filter(client=client_branch).count()
    if current_count != payload.get('count'):
        raise InvalidToken

    # Resolve optional employee (must belong to the same branch)
    served_by = None
    if employee_id:
        served_by = (
            ClientBranch.objects
            .filter(pk=employee_id, branch=client_branch.branch, is_employee=True)
            .first()
        )

    # Delivery sessions require an activated delivery code before claiming.
    # Check both the token flag (set at start) and the claim-time flag (passed
    # explicitly by the client in case the token was started without delivery=True).
    # Активированный код одноразовый: «активная» = ещё не погашенная (expires_at в будущем).
    is_delivery_session = delivery or payload.get('dl')
    active_delivery = None
    if is_delivery_session:
        active_delivery = (
            client_branch.activated_deliveries
            .filter(expires_at__gt=timezone.now())
            .order_by('-activated_at')
            .first()
        )
        if active_delivery is None:
            raise DeliveryCodeNotActivated

    # VK subscription gate: guest must be subscribed to BOTH the community
    # and newsletter before the prize is awarded.  If not, we return early
    # WITHOUT recording the attempt or cooldown so the same token can be
    # retried once the guest subscribes (within the 10-minute TTL).
    vk_status = ClientVKStatus.objects.filter(client=client_branch).first()
    is_member = vk_status.is_community_member if vk_status else False
    is_subscriber = vk_status.is_newsletter_subscriber if vk_status else False
    if not (is_member and is_subscriber):
        reward_type = payload.get('rt')
        if reward_type == 'sp':
            prize_preview = {'type': 'prize'}
        else:
            prize_preview = {'type': 'coin', 'reward': int(payload.get('ra', 1000))}
        raise VKSubscriptionRequired(is_member, is_subscriber, prize_preview)

    ClientAttempt.objects.create(client=client_branch, served_by=served_by)
    _activate_game_cooldown(client_branch)

    # Гасим использованный delivery-код: ставим expires_at=now, чтобы он больше
    # не давал бонусы (одноразовость для пост-активационных привилегий).
    if active_delivery is not None:
        active_delivery.expires_at = timezone.now()
        active_delivery.save(update_fields=['expires_at'])

    reward_type = payload.get('rt')

    if reward_type == 'sp':
        entry = SuperPrizeEntry.objects.create(
            client_branch=client_branch,
            acquired_from=SuperPrizeTrigger.GAME,
        )
        return {'type': 'super_prize', 'reward': entry}

    amount = int(payload.get('ra', 1000))
    CoinTransaction.objects.create_transfer(
        client_branch=client_branch,
        amount=amount,
        type=TransactionType.INCOME,
        source=TransactionSource.GAME,
        description=f'Победа в игре (+{amount})',
    )
    return {'type': 'coin', 'reward': amount}
