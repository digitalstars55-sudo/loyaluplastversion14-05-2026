from django.core.exceptions import ValidationError
from django.db import models

from apps.shared.base import TimeStampedModel


class ClientAttempt(TimeStampedModel):
    """
    Факт одной игровой сессии гостя.

    Награды за попытку хранятся в других таблицах:
      Первая попытка → SuperPrizeEntry(acquired_from='game') без срока
      Остальные      → Transaction(source='game') с начислением баллов

    served_by — сотрудник (is_employee=True), которого гость выбрал после игры.
    Используется для статистики по официантам.
    """

    client = models.ForeignKey(
        'branch.ClientBranch',
        on_delete=models.CASCADE,
        related_name='game_attempts',
        verbose_name='Гость',
    )
    served_by = models.ForeignKey(
        'branch.ClientBranch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='served_game_attempts',
        verbose_name='Официант',
        help_text='Сотрудник (is_employee=True), которого гость выбрал после игры.',
    )
    delivery = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name='Игра с доставки',
        help_text='True — сессия начата гостем доставки (а не по QR в кафе). '
                  'Источник сканирования для аналитики: кафе vs доставка.',
    )

    # ── Validation ────────────────────────────────────────────────────────────

    def clean(self):
        if self.served_by_id and not self.served_by.is_employee:
            raise ValidationError({'served_by': 'Выбранный профиль не является сотрудником.'})
        if self.client_id and self.served_by_id and self.client_id == self.served_by_id:
            raise ValidationError({'served_by': 'Гость не может указать себя как официанта.'})

    # ── Meta ──────────────────────────────────────────────────────────────────

    def __str__(self):
        served = f' (официант: {self.served_by})' if self.served_by_id else ''
        return f'{self.client}{served}'

    class Meta:
        verbose_name = 'Попытка'
        verbose_name_plural = 'Попытки'
        ordering = ['-created_at']
        indexes = [
            models.Index(
                fields=['client', 'created_at'],
                name='game_client_time_idx',
            ),
            models.Index(
                fields=['served_by'],
                name='game_served_by_idx',
            ),
        ]
