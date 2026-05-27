from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.shared.base import TimeStampedModel


# ── Quest ─────────────────────────────────────────────────────────────────────

class Quest(TimeStampedModel):
    """
    Задание для гостя, выполнив которое он получает баллы.
    Пример: «Опубликуй сторис с #суперкафе → 500 монет».

    Один квест может быть подключен сразу к нескольким торговым точкам через
    M2M `branches` (по аналогии с Product → branches). Старый FK `branch`
    оставлен как legacy-поле на случай отката; в новом коде использовать только
    `branches`.
    """

    # Legacy FK на одну точку (оставлен для отката миграции).
    # В новом коде использовать M2M `branches` ниже.
    branch = models.ForeignKey(
        'branch.Branch',
        on_delete=models.CASCADE,
        related_name='quests_legacy',
        verbose_name='Торговая точка (legacy)',
        null=True,
        blank=True,
        help_text='Устарело — точки задаются через M2M «Торговые точки».',
    )
    branches = models.ManyToManyField(
        'branch.Branch',
        through='QuestBranch',
        related_name='quests',
        blank=True,
        verbose_name='Торговые точки',
        help_text='Один квест может работать сразу на нескольких точках.',
    )
    name = models.CharField(max_length=255, verbose_name='Название')
    description = models.TextField(blank=True, verbose_name='Описание')
    reward = models.PositiveIntegerField(
        verbose_name='Награда (баллы)',
        help_text='Сколько баллов начисляется за выполнение.',
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name='Активен',
        help_text='Неактивный квест не виден гостям.',
    )
    ordering = models.PositiveIntegerField(
        default=0,
        verbose_name='Порядок',
        help_text='Меньшее значение отображается выше в списке гостей.',
        db_index=True,
    )

    def __str__(self):
        return f'{self.name} (+{self.reward} ★)'

    class Meta:
        verbose_name = 'Квест'
        verbose_name_plural = 'Квесты'
        ordering = ['ordering', 'name']


class QuestBranch(models.Model):
    """
    Подключение квеста к конкретной торговой точке (по аналогии с
    catalog.ProductBranch). Хранит per-branch настройки.
    """

    quest = models.ForeignKey(
        Quest,
        on_delete=models.CASCADE,
        related_name='branch_assignments',
        verbose_name='Квест',
    )
    branch = models.ForeignKey(
        'branch.Branch',
        on_delete=models.CASCADE,
        related_name='quest_assignments',
        verbose_name='Торговая точка',
    )
    ordering = models.PositiveIntegerField(
        default=0,
        verbose_name='Порядок',
        help_text='Меньшее значение отображается выше в списке гостей.',
        db_index=True,
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name='Активен на этой точке',
        help_text='Неактивный квест не виден гостям этой точки.',
    )

    def __str__(self):
        return f'{self.quest.name} → {self.branch}'

    class Meta:
        verbose_name = 'Назначение квеста в точку'
        verbose_name_plural = 'Назначения квестов в точки'
        unique_together = [('quest', 'branch')]
        ordering = ['ordering']
        indexes = [
            models.Index(
                fields=['branch', 'is_active', 'ordering'],
                name='qb_branch_active_idx',
            ),
        ]


# ── QuestSubmit ───────────────────────────────────────────────────────────────

class QuestSubmit(TimeStampedModel):
    """
    Попытка гостя выполнить квест.

    Жизненный цикл:
      pending  → complete  (официант подтвердил, гость ввёл код дня)
      pending  → expired   (истёк срок — activated_at + duration)

    Каждый гость может сделать одну попытку на квест (unique client + quest).
    После выполнения Transaction(source='quest') начисляет баллы.

    served_by — сотрудник (is_employee=True), который подтвердил выполнение.
    """

    client = models.ForeignKey(
        'branch.ClientBranch',
        on_delete=models.CASCADE,
        related_name='quest_submits',
        verbose_name='Гость',
    )
    quest = models.ForeignKey(
        Quest,
        on_delete=models.CASCADE,
        related_name='submits',
        verbose_name='Квест',
    )
    served_by = models.ForeignKey(
        'branch.ClientBranch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='confirmed_quest_submits',
        verbose_name='Официант',
        help_text='Сотрудник (is_employee=True), подтвердивший выполнение.',
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    activated_at = models.DateTimeField(
        verbose_name='Активирован',
        help_text='Момент активации квеста гостем. Отсчёт таймера.',
    )
    duration = models.PositiveIntegerField(
        default=40,
        verbose_name='Длительность (мин)',
        help_text='Минут на выполнение после активации.',
    )
    expires_at = models.DateTimeField(
        verbose_name='Истекает',
        help_text='Устанавливается автоматически: activated_at + duration.',
    )
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Выполнен',
    )

    # ── Computed state ────────────────────────────────────────────────────────

    @property
    def status(self) -> str:
        if self.completed_at:
            return 'complete'
        if timezone.now() >= self.expires_at:
            return 'expired'
        return 'pending'

    @property
    def is_complete(self) -> bool:
        return bool(self.completed_at)

    # ── Business methods ──────────────────────────────────────────────────────

    def complete(self) -> bool:
        """Mark the quest as completed. Returns False if not in pending state."""
        if self.status != 'pending':
            return False
        self.completed_at = timezone.now()
        self.save(update_fields=['completed_at'])
        return True

    # ── Lifecycle helpers ─────────────────────────────────────────────────────

    def save(self, *args, **kwargs):
        if not self.pk and not getattr(self, 'expires_at', None):
            self.expires_at = self.activated_at + timedelta(minutes=self.duration)
        super().save(*args, **kwargs)

    # ── Validation ────────────────────────────────────────────────────────────

    def clean(self):
        if self.served_by_id and not self.served_by.is_employee:
            raise ValidationError({'served_by': 'Выбранный профиль не является сотрудником.'})
        if self.client_id and self.served_by_id and self.client_id == self.served_by_id:
            raise ValidationError({'served_by': 'Гость не может указать себя как официанта.'})

    # ── Meta ──────────────────────────────────────────────────────────────────

    def __str__(self):
        return f'{self.client} → {self.quest.name}'

    class Meta:
        verbose_name = 'Выполнение квеста'
        verbose_name_plural = 'Выполнения квестов'
        unique_together = ('client', 'quest')
        ordering = ['-created_at']
        indexes = [
            models.Index(
                fields=['quest', 'completed_at'],
                name='qs_quest_complete_idx',
            ),
            models.Index(
                fields=['client', 'completed_at'],
                name='qs_client_complete_idx',
            ),
            models.Index(
                fields=['served_by'],
                name='qs_served_by_idx',
            ),
            models.Index(
                fields=['expires_at'],
                name='qs_expires_idx',
            ),
        ]
