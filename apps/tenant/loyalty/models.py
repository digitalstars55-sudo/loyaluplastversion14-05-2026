"""
Модели сервис-API лояльности (схема тенанта).

Движения монет остаются в `branch.CoinTransaction` (единый ledger, на котором
держится баланс/админка/мобайл). Эти две модели добавляют поверх него только
то, чего там нет:

  • LoyaltyOrder — связка «заказ ordering-BFF → лоялти» (сколько начислено /
    списано за заказ, рублёвая сумма для статусов, признак возврата).
    Позволяет: считать траты за период (статусы) и корректно откатывать заказ.

  • LoyaltyIdempotencyKey — журнал идемпотентности. Любой повтор мутирующего
    вызова с тем же ключом возвращает СОХРАНЁННЫЙ ответ и второй раз НЕ
    применяется (анти двойное списание/начисление при ретраях оплаты).
"""

from __future__ import annotations

from django.db import models


class LoyaltyOrderStatus(models.TextChoices):
    ACTIVE   = 'active',   'Активен'
    REFUNDED = 'refunded', 'Возвращён'


class LoyaltyOrder(models.Model):
    """
    Лоялти-состояние одного заказа из ordering-BFF.

    `external_order_id` — идентификатор заказа на стороне BFF (уникален в схеме
    тенанта). По нему заказ находится при списании/возврате и исключается из
    трат при возврате.
    """

    client = models.ForeignKey(
        'guest.Client',
        on_delete=models.CASCADE,
        related_name='loyalty_orders',
        verbose_name='Гость',
    )
    branch = models.ForeignKey(
        'branch.Branch',
        on_delete=models.PROTECT,
        related_name='loyalty_orders',
        verbose_name='Точка',
        help_text='Где оформлен заказ. Баланс сетевой, точка — для аналитики/записи.',
    )
    external_order_id = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        verbose_name='ID заказа (BFF)',
    )
    order_amount = models.PositiveIntegerField(
        verbose_name='Сумма заказа, ₽',
        help_text='Рублёвая сумма заказа. Идёт в зачёт трат для статусов (если не возвращён).',
    )
    points_earned = models.PositiveIntegerField(
        default=0,
        verbose_name='Начислено баллов',
    )
    points_redeemed = models.PositiveIntegerField(
        default=0,
        verbose_name='Списано баллов',
    )
    status = models.CharField(
        max_length=10,
        choices=LoyaltyOrderStatus,
        default=LoyaltyOrderStatus.ACTIVE,
        db_index=True,
        verbose_name='Статус',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Создан')
    refunded_at = models.DateTimeField(null=True, blank=True, verbose_name='Возвращён')

    def __str__(self):
        return f'{self.external_order_id} | {self.order_amount}₽ | {self.get_status_display()}'

    class Meta:
        verbose_name = 'Лоялти-заказ'
        verbose_name_plural = 'Лоялти-заказы'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', 'status', 'created_at'], name='loyorder_client_stat_idx'),
        ]


class LoyaltyIdempotencyKey(models.Model):
    """
    Журнал идемпотентности мутирующих вызовов (accrue/redeem/refund).

    `key` — `idempotency_key` из запроса (BFF генерит из order_id, напр.
    `accrue:BFF-555`). `response` — JSON прежнего успешного ответа: при повторе
    отдаём его как есть, операцию повторно НЕ выполняем.
    """

    key = models.CharField(max_length=128, unique=True, verbose_name='Ключ идемпотентности')
    response = models.JSONField(verbose_name='Сохранённый ответ')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Создан')

    def __str__(self):
        return self.key

    class Meta:
        verbose_name = 'Ключ идемпотентности'
        verbose_name_plural = 'Ключи идемпотентности'
        ordering = ['-created_at']
