from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.tenant.branch.models import (
    ClientBranch,
    CoinTransaction, TransactionType, TransactionSource,
    Cooldown, CooldownFeature,
    DailyCode, DailyCodePurpose,
    current_code_date,
)
from ..models import Quest, QuestSubmit


# ── Exceptions ────────────────────────────────────────────────────────────────

class ClientNotFound(Exception):
    pass


class QuestNotFound(Exception):
    pass


class QuestSubmitNotFound(Exception):
    pass


class QuestCooldownActive(Exception):
    """Carries the active cooldown so the view can return seconds_remaining."""
    def __init__(self, cooldown: Cooldown):
        self.cooldown = cooldown


class QuestAlreadyCompleted(Exception):
    pass


class QuestAlreadyActivated(Exception):
    """Submit is already pending — carries the existing submit for idempotent response."""
    def __init__(self, submit: QuestSubmit):
        self.submit = submit


class QuestExpired(Exception):
    pass


class InvalidCode(Exception):
    pass


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_client_branch(vk_id: int, branch_id: int) -> ClientBranch:
    try:
        return ClientBranch.objects.select_related('branch').get(
            client__vk_id=vk_id, branch__branch_id=branch_id,
        )
    except ClientBranch.DoesNotExist:
        raise ClientNotFound


def _get_quest_cooldown(client_branch: ClientBranch) -> Cooldown | None:
    return Cooldown.objects.filter(
        client=client_branch, feature=CooldownFeature.QUEST,
    ).first()


def _activate_quest_cooldown(client_branch: ClientBranch) -> None:
    now = timezone.now()
    cooldown, created = Cooldown.objects.get_or_create(
        client=client_branch,
        feature=CooldownFeature.QUEST,
        defaults={
            'last_activated_at': now,
            'expires_at': now + timedelta(hours=18),
        },
    )
    if not created:
        cooldown.activate()


def _validate_quest_code(branch, code: str | None) -> None:
    if not code:
        raise InvalidCode
    daily = DailyCode.objects.filter(
        branch=branch,
        purpose=DailyCodePurpose.QUEST,
        valid_date=current_code_date(),
    ).first()
    if not daily or daily.code != code.upper().strip():
        raise InvalidCode


# ── Public service functions ──────────────────────────────────────────────────

def get_quests(vk_id: int, branch_id: int) -> list[dict]:
    """
    All active quests for the branch, each annotated with a per-guest
    'completed' flag.

    Returns:
        [{'quest': Quest, 'completed': bool}, ...]
    """
    cb = _get_client_branch(vk_id, branch_id)
    quests = (
        Quest.objects
        .filter(branches=cb.branch, is_active=True)
        .order_by('ordering', 'name')
        .distinct()
    )
    completed_ids = set(
        QuestSubmit.objects
        .filter(client=cb, completed_at__isnull=False)
        .values_list('quest_id', flat=True)
    )
    return [{'quest': q, 'completed': q.pk in completed_ids} for q in quests]


def get_active_quest(vk_id: int, branch_id: int) -> QuestSubmit | None:
    """
    Returns the guest's current pending (not expired, not complete) quest
    submit, or None if there is no active quest.
    """
    cb = _get_client_branch(vk_id, branch_id)
    submit = (
        QuestSubmit.objects
        .select_related('quest')
        .filter(client=cb, completed_at__isnull=True)
        .order_by('-created_at')
        .first()
    )
    if submit is None or submit.status == 'expired':
        return None
    return submit


@transaction.atomic
def activate_quest(vk_id: int, branch_id: int, quest_id: int) -> QuestSubmit:
    """
    Starts a quest for the guest.

    Idempotent for an already-pending submit: raises QuestAlreadyActivated so
    the view can return 200 with the existing submit without blocking the caller
    behind the cooldown guard (e.g. on a network retry).

    Raises:
        ClientNotFound        — no profile for (vk_id, branch_id)
        QuestCooldownActive   — still within the 18-hour QUEST cooldown window
        QuestNotFound         — quest doesn't exist or is inactive for this branch
        QuestAlreadyCompleted — guest already completed this quest (one attempt per quest)
        QuestExpired          — previous attempt timed out; quest can't be re-activated
        QuestAlreadyActivated — submit is already pending (carries the QuestSubmit)
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

    cooldown = _get_quest_cooldown(cb)
    if cooldown and cooldown.is_active:
        raise QuestCooldownActive(cooldown)

    try:
        quest = Quest.objects.get(pk=quest_id, branches=cb.branch, is_active=True)
    except Quest.DoesNotExist:
        raise QuestNotFound

    # unique_together (client, quest) → one attempt per guest per quest
    existing = QuestSubmit.objects.filter(client=cb, quest=quest).first()
    if existing:
        if existing.is_complete:
            raise QuestAlreadyCompleted
        if existing.status == 'expired':
            raise QuestExpired
        raise QuestAlreadyActivated(existing)

    submit = QuestSubmit.objects.create(
        client=cb,
        quest=quest,
        activated_at=timezone.now(),
    )
    _activate_quest_cooldown(cb)
    return submit


@transaction.atomic
def submit_quest(
    vk_id: int,
    branch_id: int,
    quest_id: int,
    code: str,
    employee_id: int | None = None,
) -> QuestSubmit:
    """
    Completes a pending quest using the daily QUEST code.

    Awards the quest's coin reward and refreshes the QUEST cooldown.
    employee_id is the ClientBranch.pk of the confirming employee (optional).

    Raises:
        ClientNotFound      — no profile for (vk_id, branch_id)
        InvalidCode         — code missing, wrong, or no QUEST code for today
        QuestSubmitNotFound — no pending submit for this quest
        QuestExpired        — quest time window has elapsed
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

    _validate_quest_code(cb.branch, code)

    try:
        submit = (
            QuestSubmit.objects
            .select_for_update()
            .select_related('quest')
            .get(client=cb, quest_id=quest_id, completed_at__isnull=True)
        )
    except QuestSubmit.DoesNotExist:
        raise QuestSubmitNotFound

    if submit.status == 'expired':
        raise QuestExpired

    served_by = None
    if employee_id and employee_id != cb.pk:
        served_by = (
            ClientBranch.objects
            .filter(pk=employee_id, branch=cb.branch, is_employee=True)
            .first()
        )

    submit.completed_at = timezone.now()
    submit.served_by = served_by
    submit.save(update_fields=['completed_at', 'served_by'])

    CoinTransaction.objects.create_transfer(
        client_branch=cb,
        amount=submit.quest.reward,
        type=TransactionType.INCOME,
        source=TransactionSource.QUEST,
        description=f'Задание: {submit.quest.name}',
    )
    _activate_quest_cooldown(cb)

    return QuestSubmit.objects.select_related('quest').get(pk=submit.pk)


def get_quest_cooldown(vk_id: int, branch_id: int) -> Cooldown | None:
    """Current QUEST cooldown for the guest, or None if never activated."""
    return _get_quest_cooldown(_get_client_branch(vk_id, branch_id))


@transaction.atomic
def activate_quest_cooldown(vk_id: int, branch_id: int) -> Cooldown:
    """
    Manually creates or restarts the QUEST cooldown (admin / debug use).

    Raises:
        ClientNotFound — no profile for (vk_id, branch_id)
    """
    cb = _get_client_branch(vk_id, branch_id)
    _activate_quest_cooldown(cb)
    return Cooldown.objects.get(client=cb, feature=CooldownFeature.QUEST)
