from datetime import timedelta

from django.db import models
from django.utils import timezone

from apps.shared.base import TimeStampedModel


# ── SuperPrizeEntry ───────────────────────────────────────────────────────────

class SuperPrizeTrigger(models.TextChoices):
    GAME     = 'game',     'Игра'
    MANUAL   = 'manual',   'В ручную'
    BIRTHDAY = 'birthday', 'День Рождения'


class SuperPrizeEntry(TimeStampedModel):
    """
    Ваучер на суперприз — право гостя выбрать один приз из пула is_super_prize.

    Жизненный цикл:
      pending → claimed  (гость выбирает приз: product заполняется, claimed_at фиксируется)
      claimed → issued   (официант подтверждает выдачу)
      pending → expired  (гость не сделал выбор до expires_at)

    До момента выбора product=NULL.
    """

    client_branch = models.ForeignKey(
        'branch.ClientBranch',
        on_delete=models.CASCADE,
        related_name='super_prizes',
        verbose_name='Гость',
    )
    acquired_from = models.CharField(
        max_length=20,
        choices=SuperPrizeTrigger,
        verbose_name='Источник получения',
    )
    description = models.TextField(
        blank=True,
        verbose_name='Заметка',
        help_text='Например: «Достигнуто 10 посещений». Только для внутреннего использования.',
    )

    # ── Prize selection ───────────────────────────────────────────────────────

    product = models.ForeignKey(
        'catalog.Product',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='super_prize_claims',
        verbose_name='Выбранный приз',
        help_text='Заполняется когда гость делает выбор из пула суперпризов.',
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Действителен до',
        help_text='Срок, до которого гость должен сделать выбор. Пусто — бессрочно.',
    )
    claimed_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Приз выбран',
    )
    issued_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Выдан',
    )

    # ── Computed state ────────────────────────────────────────────────────────

    @property
    def status(self) -> str:
        if self.issued_at:
            return 'issued'
        if self.claimed_at:
            return 'claimed'
        if self.expires_at and timezone.now() >= self.expires_at:
            return 'expired'
        return 'pending'

    @property
    def is_claimable(self) -> bool:
        return self.status == 'pending'

    # ── Business methods ──────────────────────────────────────────────────────

    def claim(self, product) -> bool:
        """Guest selects a product from the super prize pool."""
        if self.status != 'pending':
            return False
        self.product = product
        self.claimed_at = timezone.now()
        self.save(update_fields=['product', 'claimed_at'])
        return True

    def mark_issued(self) -> bool:
        """Staff confirms the prize was handed out."""
        if self.status != 'claimed':
            return False
        self.issued_at = timezone.now()
        self.save(update_fields=['issued_at'])
        return True

    def __str__(self):
        prize = self.product.name if self.product else '(не выбран)'
        return f'Суперприз {prize} — {self.client_branch}'

    class Meta:
        verbose_name = 'Суперприз'
        verbose_name_plural = 'Суперпризы'
        ordering = ['-created_at']
        indexes = [
            models.Index(
                fields=['client_branch', 'issued_at'],
                name='sp_client_issued_idx',
            ),
            models.Index(
                fields=['client_branch', 'claimed_at'],
                name='sp_client_claimed_idx',
            ),
            models.Index(
                fields=['expires_at'],
                name='sp_expires_idx',
            ),
            models.Index(
                fields=['product', 'acquired_from'],
                name='sp_product_trigger_idx',
            ),
        ]


class AcquisitionSource(models.TextChoices):
    PURCHASE    = 'purchase',    'Покупка за баллы'
    SUPER_PRIZE = 'super_prize', 'Суперприз'
    BIRTHDAY    = 'birthday',    'Подарок на ДР'
    MANUAL      = 'manual',      'Выдано вручную'


class ItemStatus(models.TextChoices):
    PENDING = 'pending', 'Ожидает активации'
    ACTIVE  = 'active',  'Активирован'
    EXPIRED = 'expired', 'Истёк'
    USED    = 'used',    'Использован'


class InventoryItem(TimeStampedModel):
    """
    Приз, выданный гостю.

    Жизненный цикл:
      pending → active   (гость нажимает «Активировать» в приложении)
      active  → used     (официант подтверждает выдачу)
      active  → expired  (срок действия вышел до подтверждения)

    Поле duration задаёт окно (в минутах), в течение которого активированный
    приз считается действительным. 0 — без ограничения времени.
    """

    client_branch = models.ForeignKey(
        'branch.ClientBranch',
        on_delete=models.CASCADE,
        related_name='inventory',
        verbose_name='Гость',
    )
    product = models.ForeignKey(
        'catalog.Product',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inventory_items',
        verbose_name='Приз',
    )

    acquired_from = models.CharField(
        max_length=20,
        choices=AcquisitionSource,
        verbose_name='Способ получения',
    )
    description = models.TextField(
        blank=True,
        verbose_name='Заметка',
        help_text='Только для внутреннего использования.',
    )

    # Duration in minutes; 0 means the prize never expires after activation
    duration = models.PositiveIntegerField(
        default=40,
        verbose_name='Длительность (мин)',
        help_text='Сколько минут действителен приз после активации. 0 — без ограничения.',
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    activated_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Активирован',
    )
    expires_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Истекает',
        help_text='Устанавливается автоматически при активации.',
    )
    used_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Использован',
    )

    # ── Computed state ────────────────────────────────────────────────────────

    @property
    def status(self) -> str:
        if self.used_at:
            return ItemStatus.USED
        if self.activated_at:
            if self.expires_at and timezone.now() >= self.expires_at:
                return ItemStatus.EXPIRED
            return ItemStatus.ACTIVE
        return ItemStatus.PENDING

    @property
    def is_valid(self) -> bool:
        """True only when the prize is active (usable right now)."""
        return self.status == ItemStatus.ACTIVE

    # ── Business methods ──────────────────────────────────────────────────────

    def activate(self) -> bool:
        """Mark the prize as activated. Returns False if already activated."""
        if self.status != ItemStatus.PENDING:
            return False
        self.activated_at = timezone.now()
        self.expires_at = (
            self.activated_at + timedelta(minutes=self.duration)
            if self.duration
            else None
        )
        self.save(update_fields=['activated_at', 'expires_at'])
        return True

    def mark_used(self) -> bool:
        """Confirm prize was issued by staff. Returns False if not active."""
        if self.status != ItemStatus.ACTIVE:
            return False
        self.used_at = timezone.now()
        self.save(update_fields=['used_at'])
        return True

    # ── Meta ──────────────────────────────────────────────────────────────────

    def __str__(self):
        name = self.product.name if self.product else '(удалён)'
        return f'{name} — {self.client_branch}'

    class Meta:
        verbose_name = 'Приз гостя'
        verbose_name_plural = 'Призы гостей'
        ordering = ['-created_at']
        indexes = [
            models.Index(
                fields=['client_branch', 'used_at'],
                name='inventory_client_used_idx',
            ),
            models.Index(
                fields=['client_branch', 'activated_at'],
                name='inventory_client_act_idx',
            ),
            models.Index(
                fields=['product', 'acquired_from'],
                name='inventory_prod_source_idx',
            ),
            models.Index(
                fields=['expires_at'],
                name='inventory_expires_idx',
            ),
        ]


# ── StoryGiftEntry ────────────────────────────────────────────────────────────

class StoryStatus(models.TextChoices):
    """Технические статусы подарка из сториз (ТЗ §4)."""
    AVAILABLE_TO_PLAY  = 'available_to_play',          'Доступна игра'
    GAME_PLAYED        = 'story_game_played',          'Игра через сториз пройдена'
    GIFT_SELECTED      = 'story_gift_selected',        'Подарок выбран'
    WAITING_CAFE_VISIT = 'waiting_cafe_visit',         'Ожидает визита в кафе'
    ACTIVATED          = 'gift_activated_story',       'Подарок активирован через сториз'
    EXPIRED            = 'expired_after_activation',   'Истекло время активации'
    USED               = 'used',                       'Использован (выдан сотрудником)'


class StoryGiftEntry(TimeStampedModel):
    """
    Подарок, выигранный внешним пользователем в игре ИЗ СТОРИЗ.

    Отдельная сущность от InventoryItem — у неё свой жизненный цикл с
    блокировкой активации до визита в кафе. Существующий инвентарь НЕ
    затрагивается. Один экземпляр на client_branch (= один на VK ID на
    точку) — это и есть лимит «одна игра через сториз на VK ID на кафе».

    Жизненный цикл (ТЗ §4):
      available_to_play → story_game_played   (сыграл story-игру)
                        → story_gift_selected (нажал «Забрать» и выбрал подарок)
                        → waiting_cafe_visit   (подарок сохранён в «Мои подарки» — метрика «Получили»)
                        → gift_activated_story (в кафе ввёл код дня и подтвердил — метрика «Активировали»)
                        → expired_after_activation (вышло время после активации)
                        → used                 (сотрудник подтвердил выдачу)

    Атрибуция «из чьей сторис» — через client_branch.invited_by.
    Фактическая активация (activated_at) ставится ТОЛЬКО после ввода кода дня
    в кафе. Домашнее «Активировать» activated_at НЕ заполняет (ТЗ §9).
    """

    client_branch = models.OneToOneField(
        'branch.ClientBranch',
        on_delete=models.CASCADE,
        related_name='story_gift',
        verbose_name='Гость',
    )

    # ── Игра ────────────────────────────────────────────────────────────────
    played_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Сыграл story-игру',
    )

    # ── Выбор подарка ───────────────────────────────────────────────────────
    product = models.ForeignKey(
        'catalog.Product',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='story_gift_claims',
        verbose_name='Выбранный подарок',
        help_text='Из набора подарков для сториз (is_story_prize=True).',
    )
    selected_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Подарок выбран',
    )
    received_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Сохранён в «Мои подарки»',
        help_text='Момент фиксации метрики «Получили подарок через сториз».',
    )

    # ── Снимок условий на момент получения ──────────────────────────────────
    duration = models.PositiveIntegerField(
        default=40,
        verbose_name='Длительность после активации (мин)',
        help_text='Сколько минут действует подарок после активации в кафе.',
    )
    min_order_amount = models.PositiveIntegerField(
        default=0,
        verbose_name='Мин. сумма заказа, ₽',
        help_text='Снимок минимальной суммы заказа на момент получения.',
    )

    # ── Фактическая активация в кафе ────────────────────────────────────────
    activated_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Активирован в кафе',
        help_text='Заполняется ТОЛЬКО после ввода кода дня в кафе. Метрика «Активировали через сториз».',
    )
    expires_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Истекает',
        help_text='activated_at + duration. Ставится автоматически при активации.',
    )
    used_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Выдан сотрудником',
    )

    # ── Источник/кампания (на будущее, для аналитики) ───────────────────────
    campaign_key = models.CharField(
        max_length=64, blank=True, default='',
        verbose_name='Ключ кампании/сториз',
        help_text='Опционально: идентификатор кампании или сториз для аналитики.',
    )

    # ── Computed state ──────────────────────────────────────────────────────

    @property
    def status(self) -> str:
        if self.used_at:
            return StoryStatus.USED
        if self.activated_at:
            if self.expires_at and timezone.now() >= self.expires_at:
                return StoryStatus.EXPIRED
            return StoryStatus.ACTIVATED
        if self.received_at:
            return StoryStatus.WAITING_CAFE_VISIT
        if self.selected_at:
            return StoryStatus.GIFT_SELECTED
        if self.played_at:
            return StoryStatus.GAME_PLAYED
        return StoryStatus.AVAILABLE_TO_PLAY

    @property
    def status_label(self) -> str:
        return StoryStatus(self.status).label

    @property
    def is_valid(self) -> bool:
        """True только когда подарок активирован и ещё действует."""
        return self.status == StoryStatus.ACTIVATED

    # ── Business methods ────────────────────────────────────────────────────

    def mark_played(self) -> None:
        """Фиксирует прохождение story-игры (идемпотентно)."""
        if not self.played_at:
            self.played_at = timezone.now()
            self.save(update_fields=['played_at'])

    def select_gift(self, product, *, min_order_amount: int = 0, duration: int = 40) -> bool:
        """
        Гость выбрал подарок из набора сториз → сохраняем в «Мои подарки».
        Возвращает False, если подарок уже был выбран.
        """
        if self.selected_at:
            return False
        now = timezone.now()
        self.product = product
        self.min_order_amount = min_order_amount
        self.duration = duration
        self.selected_at = now
        self.received_at = now
        self.save(update_fields=['product', 'min_order_amount', 'duration', 'selected_at', 'received_at'])
        return True

    def activate(self) -> bool:
        """
        Фактическая активация в кафе (после проверки кода дня в сервисе).
        Возвращает False, если ещё не получен или уже активирован.
        """
        if self.status != StoryStatus.WAITING_CAFE_VISIT:
            return False
        self.activated_at = timezone.now()
        self.expires_at = (
            self.activated_at + timedelta(minutes=self.duration)
            if self.duration
            else None
        )
        self.save(update_fields=['activated_at', 'expires_at'])
        return True

    def mark_used(self) -> bool:
        """Сотрудник подтвердил выдачу. Возвращает False, если не активен."""
        if self.status != StoryStatus.ACTIVATED:
            return False
        self.used_at = timezone.now()
        self.save(update_fields=['used_at'])
        return True

    def __str__(self):
        name = self.product.name if self.product else '(не выбран)'
        return f'Story-подарок {name} — {self.client_branch}'

    class Meta:
        verbose_name = 'Подарок из сториз'
        verbose_name_plural = 'Подарки из сториз'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['received_at'],  name='story_received_idx'),
            models.Index(fields=['activated_at'], name='story_activated_idx'),
            models.Index(fields=['expires_at'],   name='story_expires_idx'),
        ]
