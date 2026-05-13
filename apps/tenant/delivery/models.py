from datetime import timedelta

from django.db import models
from django.utils import timezone

from apps.shared.base import TimeStampedModel


# Sentinel "no expiry" window: код активен пока не активирован.
# 100 лет — практический эквивалент бесконечности, без миграции на nullable.
_NO_EXPIRY_DAYS = 365 * 100


class OrderSource(models.TextChoices):
    IIKO    = 'iiko',    'iiko'
    DOOGLYS = 'dooglys', 'Dooglys'


class Delivery(TimeStampedModel):
    """
    Код доставки, полученный от POS-системы через webhook.

    Жизненный цикл:
      pending   → activated  (клиент ввёл последние 5 цифр кода в приложении)
      pending   → expired    (истёк duration часов с момента создания)

    Поиск по коду: branch + short_code (последние 5 символов code).
    Индекс (branch, short_code) обеспечивает быстрый lookup без full-scan.

    После активации клиент получает доступ к игре и другим механикам.
    """

    branch = models.ForeignKey(
        'branch.Branch',
        on_delete=models.CASCADE,
        related_name='deliveries',
        verbose_name='Торговая точка',
    )

    # ── Code ──────────────────────────────────────────────────────────────────

    code = models.CharField(
        max_length=512,
        unique=True,
        verbose_name='Код доставки',
        help_text='Полный код из POS-системы.',
    )
    short_code = models.CharField(
        max_length=5,
        verbose_name='Короткий код',
        help_text='Последние 5 символов кода. Устанавливается автоматически.',
        editable=False,
    )

    # ── Order info ────────────────────────────────────────────────────────────

    order_source = models.CharField(
        max_length=20,
        choices=OrderSource,
        verbose_name='Источник заказа',
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    duration = models.PositiveIntegerField(
        default=8,
        verbose_name='Длительность (ч)',
        help_text='Историческое поле: больше не ограничивает время на активацию '
                  '(коды действуют до первого использования).',
    )
    expires_at = models.DateTimeField(
        verbose_name='Действителен до',
        help_text='Технический маркер: ставится на 100 лет вперёд, чтобы код был '
                  'активен пока его не активируют; обнуляется в now() при «гашении» '
                  'активной доставки.',
    )
    activated_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Активирован',
    )
    activated_by = models.ForeignKey(
        'branch.ClientBranch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='activated_deliveries',
        verbose_name='Активировал',
    )

    # ── Computed state ────────────────────────────────────────────────────────

    @property
    def status(self) -> str:
        if self.activated_at:
            return 'activated'
        if timezone.now() >= self.expires_at:
            return 'expired'
        return 'pending'

    @property
    def is_active_window(self) -> bool:
        """True if activated and the game/play window is still open."""
        if not self.activated_at:
            return False
        return timezone.now() < self.expires_at

    # ── Business methods ──────────────────────────────────────────────────────

    def activate(self, client_branch) -> bool:
        """Client enters the short code — activates the delivery."""
        if self.status != 'pending':
            return False
        self.activated_at = timezone.now()
        self.activated_by = client_branch
        self.save(update_fields=['activated_at', 'activated_by'])
        return True

    # ── Save hook ─────────────────────────────────────────────────────────────

    def save(self, *args, **kwargs):
        if not self.pk:
            self.short_code = self.code[-5:]
            if not self.expires_at:
                self.expires_at = timezone.now() + timedelta(days=_NO_EXPIRY_DAYS)
        super().save(*args, **kwargs)

    # ── Meta ──────────────────────────────────────────────────────────────────

    def __str__(self):
        return f'…{self.short_code} [{self.branch}]'

    class Meta:
        verbose_name = 'Доставка'
        verbose_name_plural = 'Доставки'
        ordering = ['-created_at']
        indexes = [
            models.Index(
                fields=['branch', 'short_code'],
                name='delivery_branch_short_idx',
            ),
            models.Index(
                fields=['expires_at'],
                name='delivery_expires_idx',
            ),
            models.Index(
                fields=['order_source', 'created_at'],
                name='delivery_source_time_idx',
            ),
            models.Index(
                fields=['activated_by'],
                name='delivery_activated_by_idx',
            ),
        ]
