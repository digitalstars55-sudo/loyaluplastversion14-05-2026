import math
from datetime import timedelta

from django.db import models, transaction
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
    # Гость так и не дошёл в кафе за отведённый срок (claim_expires_at) —
    # подарок сгорел ДО активации. Не путать с EXPIRED (истёк таймер ПОСЛЕ активации).
    CLAIM_EXPIRED      = 'claim_expired',              'Сгорел (не забрали вовремя)'
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

    # ── Источник/кампания (для аналитики по источникам) ─────────────────────
    source = models.CharField(
        max_length=16,
        default='story',
        db_index=True,
        verbose_name='Источник входа',
        help_text='story — вход из сториз; website — вход по QR с сайта клиента.',
    )
    campaign_key = models.CharField(
        max_length=64, blank=True, default='',
        verbose_name='Метка источника/сайта',
        help_text='Для website — метка сайта (напр. tula), чтобы различать сайты в аналитике.',
    )
    # ── Сетевой подарок: где фактически забрали ─────────────────────────────
    activated_branch = models.ForeignKey(
        'branch.Branch',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='story_gifts_activated_here',
        verbose_name='Точка активации (сетевой подарок)',
        help_text='Точка, где забрали подарок по её коду дня. Подарок сетевой — '
                  'забрать можно в любой точке, независимо от источника. '
                  'Пусто — у подарков, активированных до перехода на сетевую схему.',
    )

    # ── Срок, за который надо ЗАБРАТЬ подарок в кафе ────────────────────────
    claim_expires_at = models.DateTimeField(
        null=True, blank=True,
        db_index=True,
        verbose_name='Забрать до',
        help_text='received_at + срок жизни (настройка сети). Если не активировали '
                  'до этого момента — подарок сгорает. Пусто — бессрочно (подарки, '
                  'выданные до включения срока).',
    )
    reminder_sent_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Напоминание отправлено',
        help_text='Когда ушла авторассылка «подарок не забран». Дедуп: одно на подарок.',
    )

    # ── Computed state ──────────────────────────────────────────────────────

    @property
    def is_claim_expired(self) -> bool:
        """Срок забора вышел, а в кафе так и не активировали → подарок сгорел."""
        return bool(
            self.claim_expires_at
            and not self.activated_at
            and timezone.now() >= self.claim_expires_at
        )

    @property
    def days_left_to_claim(self) -> int | None:
        """
        Сколько дней осталось забрать подарок (округляем вверх — «остался 1 день»
        показываем до самого конца последних суток). None — бессрочный или уже
        активирован. 0 — сгорел.
        """
        if not self.claim_expires_at or self.activated_at:
            return None
        delta = self.claim_expires_at - timezone.now()
        if delta.total_seconds() <= 0:
            return 0
        return math.ceil(delta.total_seconds() / 86400)

    @property
    def status(self) -> str:
        if self.used_at:
            return StoryStatus.USED
        if self.activated_at:
            if self.expires_at and timezone.now() >= self.expires_at:
                return StoryStatus.EXPIRED
            return StoryStatus.ACTIVATED
        # Не активирован и срок забора вышел → сгорел.
        if self.is_claim_expired:
            return StoryStatus.CLAIM_EXPIRED
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

    def select_gift(
        self, product, *, min_order_amount: int = 0, duration: int = 40,
        lifetime_days: int = 0,
    ) -> bool:
        """
        Гость выбрал подарок из набора сториз → сохраняем в «Мои подарки».
        Возвращает False, если подарок уже был выбран.

        lifetime_days — сколько дней есть на забор подарка в кафе (снимок настройки
        сети на момент получения). 0 — бессрочно (claim_expires_at остаётся пустым).
        """
        if self.selected_at:
            return False
        now = timezone.now()
        self.product = product
        self.min_order_amount = min_order_amount
        self.duration = duration
        self.selected_at = now
        self.received_at = now
        fields = ['product', 'min_order_amount', 'duration', 'selected_at', 'received_at']
        if lifetime_days and lifetime_days > 0:
            self.claim_expires_at = now + timedelta(days=lifetime_days)
            fields.append('claim_expires_at')
        self.save(update_fields=fields)
        return True

    def activate(self, activated_branch=None) -> bool:
        """
        Фактическая активация в кафе (после проверки кода дня в сервисе).
        Возвращает False, если ещё не получен или уже активирован.

        activated_branch — точка, где забрали (для website-подарка, который
        активируется по коду дня любой точки сети). Для сториз — None.
        """
        if self.status != StoryStatus.WAITING_CAFE_VISIT:
            return False
        self.activated_at = timezone.now()
        self.expires_at = (
            self.activated_at + timedelta(minutes=self.duration)
            if self.duration
            else None
        )
        update_fields = ['activated_at', 'expires_at']
        if activated_branch is not None:
            self.activated_branch = activated_branch
            update_fields.append('activated_branch')
        self.save(update_fields=update_fields)
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


# ── GiftCostEvent ─────────────────────────────────────────────────────────────

class GiftCostEvent(models.Model):
    """
    Снимок себестоимости подарка на момент ФАКТИЧЕСКОЙ активации гостем.

    Пишется один раз при активации InventoryItem или StoryGiftEntry. Хранит
    КОПИЮ себестоимости (cost_rub) на тот момент — чтобы прошлые отчёты
    «Экономики клиента» не менялись, если менеджер позже поправит себестоимость
    в карточке подарка (ТЗ §3.2, §12). В расчёт затрат за период попадают только
    события, activated_at которых входит в выбранный период. branch
    денормализован для быстрой фильтрации затрат по точке.
    """

    class Kind(models.TextChoices):
        INVENTORY = 'inventory', 'Приз гостя'
        STORY     = 'story',     'Подарок из сториз'

    client_branch = models.ForeignKey(
        'branch.ClientBranch',
        on_delete=models.CASCADE,
        related_name='gift_cost_events',
        verbose_name='Гость',
    )
    product = models.ForeignKey(
        'catalog.Product',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='gift_cost_events',
        verbose_name='Подарок',
    )
    branch = models.ForeignKey(
        'branch.Branch',
        on_delete=models.CASCADE,
        related_name='gift_cost_events',
        verbose_name='Точка',
        help_text='Точка активации (для сетевого website-подарка — где забрали).',
    )
    kind = models.CharField(
        max_length=16,
        choices=Kind.choices,
        verbose_name='Тип подарка',
    )
    cost_rub = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        verbose_name='Себестоимость на момент активации, ₽',
    )
    activated_at = models.DateTimeField(
        db_index=True,
        verbose_name='Активирован',
    )

    @classmethod
    def record(cls, *, client_branch, product, branch, kind, activated_at=None):
        """
        Best-effort снимок затрат при активации подарка. Не должен ронять саму
        активацию — оборачиваем в savepoint и глушим любые ошибки.
        """
        try:
            cost = getattr(product, 'cost_price_rub', None) or 0
            with transaction.atomic():
                cls.objects.create(
                    client_branch=client_branch,
                    product=product,
                    branch=branch,
                    kind=kind,
                    cost_rub=cost,
                    activated_at=activated_at or timezone.now(),
                )
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                'GiftCostEvent.record failed (cb=%s, kind=%s)',
                getattr(client_branch, 'pk', None), kind,
            )

    def __str__(self):
        return f'{self.cost_rub}₽ — {self.client_branch} ({self.kind})'

    class Meta:
        verbose_name = 'Затрата на подарок'
        verbose_name_plural = 'Затраты на подарки'
        ordering = ['-activated_at']
        indexes = [
            models.Index(fields=['branch', 'activated_at'], name='giftcost_branch_act_idx'),
            models.Index(fields=['activated_at'],           name='giftcost_act_idx'),
        ]


# ── RewardCatalogItem ─────────────────────────────────────────────────────────

class RewardTier(models.TextChoices):
    """
    Категория (тир) награды из ТЗ RF-авторассылок §7.

    Определяет «вес» подарка в экономике: базовый срок жизни и потолок
    себестоимости относительно минимальной суммы заказа.
      G1 — 7 дней,    себестоимость ≤ 8%  минимального заказа
      G2 — 10-14 дней, ≤ 10%
      G3 — 14-21 день, ≤ 12% + отдельный месячный бюджет
    """

    G1 = 'G1', 'G1 · Лёгкий (низкая себестоимость)'
    G2 = 'G2', 'G2 · Средний'
    G3 = 'G3', 'G3 · Ценный (VIP)'


class RewardCatalogItem(TimeStampedModel):
    """
    Позиция «Каталога наград» — справочник призов для RFM-кампаний и
    RF-авторассылок (ТЗ RFM §4, ТЗ авторассылок §7 и §15.4).

    Это НЕ второй независимый справочник подарков. Сам подарок остаётся
    сущностью catalog.Product; позиция каталога — тонкая надстройка над ним,
    которая добавляет то, чего у Product нет: тир G1/G2/G3, вес выбора,
    лимит активаций и срок жизни по умолчанию. Поле product необязательно:
      • product заполнен  → название/картинка/описание/себестоимость
                            наследуются от подарка, если не переопределены здесь;
      • product пуст      → позиция описывается собственными полями
                            (позиция «на бумаге», которой ещё нет в каталоге).

    Модель ничего не выдаёт гостям сама: она только описывает пул и считает
    выдачи. Выбор позиции и учёт лимита — в apps.tenant.inventory.reward_catalog.
    """

    # ── Связь с подарком ──────────────────────────────────────────────────────

    product = models.ForeignKey(
        'catalog.Product',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reward_catalog_entries',
        verbose_name='Подарок из каталога',
        help_text=(
            'Необязательно. Если выбран — название, изображение, описание и '
            'себестоимость подтягиваются из карточки подарка, когда поля ниже '
            'оставлены пустыми. Один подарок может участвовать в нескольких '
            'позициях каталога наград (например в разных тирах или точках).'
        ),
    )

    # ── Карточка позиции ──────────────────────────────────────────────────────

    name = models.CharField(
        max_length=255,
        blank=True,
        verbose_name='Название',
        help_text='Пусто — берётся название выбранного подарка.',
    )
    internal_code = models.CharField(
        max_length=64,
        blank=True,
        verbose_name='Внутренний код',
        help_text='Идентификатор для сотрудника и журнала выдачи. Необязателен.',
    )
    image = models.ImageField(
        upload_to='reward_catalog/',
        blank=True,
        null=True,
        verbose_name='Изображение',
        help_text='Пусто — берётся изображение выбранного подарка.',
    )
    description = models.TextField(
        blank=True,
        verbose_name='Описание',
        help_text='Пусто — берётся описание выбранного подарка.',
    )
    cost_price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name='Себестоимость, ₽',
        help_text=(
            '0 — берётся себестоимость выбранного подарка. Используется для '
            'бюджета кампаний и потолка по тиру. Фактические затраты по-прежнему '
            'считаются снимком на момент активации («Затраты на подарки»).'
        ),
    )
    min_order_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name='Минимальная сумма заказа, ₽',
        help_text='0 — без ограничения. Показывается гостю в сообщении и на экране подарка.',
    )

    # ── Правила выбора ────────────────────────────────────────────────────────

    tier = models.CharField(
        max_length=2,
        choices=RewardTier,
        default=RewardTier.G1,
        verbose_name='Категория (тир)',
        help_text='Сценарий рассылки задаёт тир, а система сама выбирает позицию внутри него.',
    )
    weight = models.PositiveIntegerField(
        default=1,
        verbose_name='Вес выбора',
        help_text=(
            'Вероятность выпадения внутри тира: чем больше вес, тем чаще позиция '
            'достаётся гостю. По умолчанию более дешёвым позициям ставят больший вес. '
            '0 — позиция участвует только если других не осталось.'
        ),
    )
    default_lifetime_days = models.PositiveSmallIntegerField(
        default=10,
        verbose_name='Срок действия по умолчанию, дней',
        help_text=(
            'Сколько дней у гостя есть на активацию, если сценарий не задал свой срок. '
            'Ориентиры ТЗ: G1 — 7 дней, G2 — 10-14, G3 — 14-21.'
        ),
    )

    # ── Лимиты и доступность ──────────────────────────────────────────────────

    activation_limit = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name='Лимит выдач',
        help_text='Пусто — без лимита. Когда счётчик выдач достигает лимита, позиция перестаёт выпадать.',
    )
    issued_count = models.PositiveIntegerField(
        default=0,
        verbose_name='Выдано',
        help_text='Счётчик выданных наград. Меняется системой, вручную не редактируется.',
    )
    available_from = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Доступна с',
        help_text='Пусто — без ограничения снизу.',
    )
    available_to = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Доступна до',
        help_text='Пусто — без ограничения сверху.',
    )
    branch = models.ForeignKey(
        'branch.Branch',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='reward_catalog_items',
        verbose_name='Торговая точка',
        help_text='Оставьте пустым, чтобы позиция была доступна всей сети.',
    )

    # ── Флаги ─────────────────────────────────────────────────────────────────

    is_active = models.BooleanField(
        default=True,
        verbose_name='Активна',
        help_text='Снимите галочку, чтобы временно убрать позицию из выдачи, не удаляя её.',
    )
    is_archived = models.BooleanField(
        default=False,
        verbose_name='Архивная',
        help_text='Скрытая позиция: не выпадает и не показывается в рабочих списках. История сохраняется.',
    )
    available_for_rfm = models.BooleanField(
        default=True,
        verbose_name='Доступна для RFM',
        help_text='Позицию можно назначить как награду RFM-сегменту и использовать в RF-авторассылках.',
    )

    # ── Наследование от подарка ───────────────────────────────────────────────

    @property
    def display_name(self) -> str:
        """Собственное название, иначе название связанного подарка."""
        if self.name:
            return self.name
        if self.product_id and self.product:
            return self.product.name
        return f'Награда #{self.pk}' if self.pk else 'Награда'

    @property
    def display_description(self) -> str:
        if self.description:
            return self.description
        if self.product_id and self.product:
            return self.product.description or ''
        return ''

    @property
    def display_image(self):
        """ImageFieldFile собственной картинки, иначе картинки подарка. Может быть None."""
        if self.image:
            return self.image
        if self.product_id and self.product and self.product.image:
            return self.product.image
        return None

    @property
    def effective_cost_price(self):
        """Себестоимость позиции: собственная, иначе унаследованная от подарка."""
        if self.cost_price:
            return self.cost_price
        if self.product_id and self.product:
            return self.product.cost_price_rub or 0
        return self.cost_price or 0

    # ── Лимиты ────────────────────────────────────────────────────────────────

    @property
    def is_limit_reached(self) -> bool:
        if self.activation_limit is None:
            return False
        return self.issued_count >= self.activation_limit

    @property
    def remaining_issues(self):
        """Сколько выдач осталось. None — лимита нет."""
        if self.activation_limit is None:
            return None
        return max(self.activation_limit - self.issued_count, 0)

    def is_within_period(self, at=None) -> bool:
        at = at or timezone.now()
        if self.available_from and at < self.available_from:
            return False
        if self.available_to and at > self.available_to:
            return False
        return True

    def is_available_now(self, at=None) -> bool:
        """Позиция в принципе может выпасть: активна, в периоде, лимит не исчерпан."""
        return (
            self.is_active
            and not self.is_archived
            and not self.is_limit_reached
            and self.is_within_period(at)
        )

    def __str__(self):
        return f'{self.display_name} · {self.tier}'

    class Meta:
        verbose_name = 'Награда из каталога'
        verbose_name_plural = 'Каталог наград'
        ordering = ['tier', 'name', 'pk']
        indexes = [
            models.Index(fields=['tier', 'is_active'],  name='rewardcat_tier_active_idx'),
            models.Index(fields=['available_for_rfm'],  name='rewardcat_rfm_idx'),
            models.Index(fields=['is_archived'],        name='rewardcat_archived_idx'),
        ]
