"""
Бизнес-логика сервис-API лояльности.

Решения владельца (зафиксированы):
  • 1 балл = 1 ₽ (списание в баллах напрямую = рублёвая скидка).
  • Частичная оплата баллами — до 100% суммы (отдельного лимита нет; ограничение
    «не больше суммы заказа» делает чекаут на стороне BFF, здесь — только баланс).
  • Баланс гостя — СЕТЕВОЙ: агрегат по всем точкам тенанта (бренда). branch_id
    нужен лишь чтобы знать, где оформлен заказ (куда писать транзакцию).

Начисление: LoyalUP считает баллы сам — `floor(order_amount * ACCRUAL_PERCENT/100)`.
Процент кэшбэка берётся из настройки LOYALTY_ACCRUAL_PERCENT (env, дефолт 10).
BFF может прислать явный `points` — тогда берём его (override).

Сами монеты пишутся в branch.CoinTransaction (source='delivery'), чтобы
баланс/админка/мобайл видели их без изменений.
"""

from __future__ import annotations

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.shared.guest.models import Client
from apps.tenant.branch.models import (
    Branch,
    ClientBranch,
    CoinTransaction,
    TransactionSource,
    TransactionType,
)

from .models import LoyaltyIdempotencyKey, LoyaltyOrder, LoyaltyOrderStatus


# ── Доменные ошибки → коды из ТЗ ────────────────────────────────────────────
class LoyaltyError(Exception):
    code = 'error'
    http_status = 400

    def __init__(self, detail: str = '', **extra):
        self.detail = detail or self.code
        self.extra = extra
        super().__init__(self.detail)


class GuestNotFound(LoyaltyError):
    code = 'guest_not_found'
    http_status = 404


class BranchNotFound(LoyaltyError):
    code = 'branch_not_found'
    http_status = 404


class InvalidAmount(LoyaltyError):
    code = 'invalid_amount'
    http_status = 400


class InsufficientBalance(LoyaltyError):
    code = 'insufficient_balance'
    http_status = 409


class OrderNotFound(LoyaltyError):
    code = 'order_not_found'
    http_status = 404


def _accrual_percent() -> int:
    """Процент кэшбэка для начисления (env LOYALTY_ACCRUAL_PERCENT, дефолт 10)."""
    return int(getattr(settings, 'LOYALTY_ACCRUAL_PERCENT', 10) or 0)


# ── Резолверы ───────────────────────────────────────────────────────────────
def get_client(vk_id: int) -> Client:
    client = Client.objects.filter(vk_id=vk_id).first()
    if client is None or not client.is_active:
        raise GuestNotFound()
    return client


def get_branch(branch_id: int) -> Branch:
    branch = Branch.objects.filter(pk=branch_id).first()
    if branch is None:
        raise BranchNotFound()
    return branch


def _client_branch(client: Client, branch: Branch) -> ClientBranch:
    cb, _ = ClientBranch.objects.get_or_create(client=client, branch=branch)
    return cb


def network_balance(client: Client) -> int:
    """Сетевой баланс: Σ income − Σ expense по всем профилям гостя в тенанте."""
    agg = CoinTransaction.objects.filter(client__client=client).aggregate(
        income=Sum('amount', filter=Q(type=TransactionType.INCOME)),
        expense=Sum('amount', filter=Q(type=TransactionType.EXPENSE)),
    )
    return (agg['income'] or 0) - (agg['expense'] or 0)


# ── Идемпотентность ─────────────────────────────────────────────────────────
def _with_idempotency(key: str, fn):
    """
    Выполнить мутирующую операцию идемпотентно.

    Возвращает (response_dict, replayed: bool). Повтор с тем же key возвращает
    сохранённый ответ. Запись ключа — В ТОЙ ЖЕ транзакции, что и движения монет:
    при гонке второй вызов ловит IntegrityError, откатывает свои монеты и
    отдаёт ответ первого.
    """
    existing = LoyaltyIdempotencyKey.objects.filter(key=key).first()
    if existing is not None:
        return existing.response, True
    try:
        with transaction.atomic():
            result = fn()
            LoyaltyIdempotencyKey.objects.create(key=key, response=result)
        return result, False
    except IntegrityError:
        existing = LoyaltyIdempotencyKey.objects.filter(key=key).first()
        if existing is not None:
            return existing.response, True
        raise


# ── Операции ────────────────────────────────────────────────────────────────
def accrue(vk_id: int, branch_id: int, order_id: str, order_total: int,
           idempotency_key: str, points: int | None = None) -> tuple[dict, bool]:
    """Зафиксировать заказ (для статусов) и начислить баллы."""
    if not order_id:
        raise InvalidAmount('order_id обязателен')
    if order_total is None or int(order_total) < 0:
        raise InvalidAmount('order_total должен быть ≥ 0')
    order_total = int(order_total)

    client = get_client(vk_id)
    branch = get_branch(branch_id)

    if points is not None:
        points_earned = max(0, int(points))
    else:
        points_earned = (order_total * _accrual_percent()) // 100

    def _do():
        cb = _client_branch(client, branch)
        order = LoyaltyOrder.objects.create(
            client=client, branch=branch, external_order_id=str(order_id),
            order_amount=order_total, points_earned=points_earned,
            status=LoyaltyOrderStatus.ACTIVE,
        )
        tx_id = None
        if points_earned > 0:
            tx = CoinTransaction.objects.create(
                client=cb, type=TransactionType.INCOME,
                source=TransactionSource.DELIVERY, amount=points_earned,
                description=f'Заказ {order_id} (начисление)',
            )
            tx_id = tx.pk
        return {
            'transaction_id': tx_id,
            'points_earned': points_earned,
            'balance': network_balance(client),
            'order_id': str(order_id),
        }

    return _with_idempotency(idempotency_key, _do)


def redeem(vk_id: int, branch_id: int, order_id: str, amount: int,
           idempotency_key: str) -> tuple[dict, bool]:
    """Списать баллы (частичная оплата). Проверка — по сетевому балансу."""
    if amount is None or int(amount) <= 0:
        raise InvalidAmount('amount должен быть > 0')
    amount = int(amount)

    client = get_client(vk_id)
    branch = get_branch(branch_id)

    def _do():
        cb = _client_branch(client, branch)
        # Блокируем все профили гостя в тенанте → сериализуем конкурентные
        # списания (как create_transfer блокирует одну строку, но сетево).
        list(ClientBranch.objects.select_for_update()
             .filter(client=client).values_list('pk', flat=True))
        balance = network_balance(client)
        if balance < amount:
            raise InsufficientBalance(balance=balance)

        tx = CoinTransaction.objects.create(
            client=cb, type=TransactionType.EXPENSE,
            source=TransactionSource.DELIVERY, amount=amount,
            description=f'Заказ {order_id} (списание)',
        )
        # Привяжем к лоялти-заказу, если accrue уже был; иначе заведём запись.
        order = LoyaltyOrder.objects.filter(external_order_id=str(order_id)).first()
        if order is not None:
            order.points_redeemed = order.points_redeemed + amount
            order.save(update_fields=['points_redeemed'])
        return {
            'transaction_id': tx.pk,
            'points_spent': amount,
            'balance': network_balance(client),
            'order_id': str(order_id),
        }

    return _with_idempotency(idempotency_key, _do)


def refund(vk_id: int, branch_id: int, order_id: str,
           idempotency_key: str) -> tuple[dict, bool]:
    """
    Откат заказа: реверс начисления (expense) и возврат списанных баллов
    (income). Заказ помечается refunded и исключается из трат для статусов.
    """
    client = get_client(vk_id)

    def _do():
        order = LoyaltyOrder.objects.select_for_update().filter(
            external_order_id=str(order_id), client=client,
        ).first()
        if order is None:
            raise OrderNotFound()
        if order.status == LoyaltyOrderStatus.REFUNDED:
            # Уже возвращён — отдаём текущий снимок (идемпотентность по смыслу).
            return {
                'reversed_earned': 0, 'restored_spent': 0,
                'balance': network_balance(client), 'order_id': str(order_id),
            }

        cb = _client_branch(client, order.branch)
        reversed_earned = order.points_earned
        restored_spent = order.points_redeemed

        # Реверс начисления: списываем начисленные баллы. НЕ через create_transfer
        # (он блокирует уход в минус) — откат корректен, даже если гость уже
        # потратил эти баллы; баланс может временно стать отрицательным.
        if reversed_earned > 0:
            CoinTransaction.objects.create(
                client=cb, type=TransactionType.EXPENSE,
                source=TransactionSource.DELIVERY, amount=reversed_earned,
                description=f'Заказ {order_id} (откат начисления)',
            )
        # Возврат списанных баллов гостю.
        if restored_spent > 0:
            CoinTransaction.objects.create(
                client=cb, type=TransactionType.INCOME,
                source=TransactionSource.DELIVERY, amount=restored_spent,
                description=f'Заказ {order_id} (возврат списанных)',
            )

        order.status = LoyaltyOrderStatus.REFUNDED
        order.refunded_at = timezone.now()
        order.save(update_fields=['status', 'refunded_at'])

        return {
            'reversed_earned': reversed_earned,
            'restored_spent': restored_spent,
            'balance': network_balance(client),
            'order_id': str(order_id),
        }

    return _with_idempotency(idempotency_key, _do)


def spend_total(vk_id: int, period_days: int) -> dict:
    """Сумма трат (₽) по активным заказам за период — для расчёта статусов."""
    client = get_client(vk_id)
    qs = LoyaltyOrder.objects.filter(client=client, status=LoyaltyOrderStatus.ACTIVE)
    if period_days and period_days > 0:
        since = timezone.now() - timezone.timedelta(days=period_days)
        qs = qs.filter(created_at__gte=since)
    total = qs.aggregate(s=Sum('order_amount'))['s'] or 0
    return {'vk_id': vk_id, 'spend_total': total, 'period_days': period_days or 0}
