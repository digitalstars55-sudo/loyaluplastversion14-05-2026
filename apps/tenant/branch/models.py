from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.shared.base import TimeStampedModel


class Branch(TimeStampedModel):
    """
    Физическая торговая точка (ресторан/кафе).
    Хранится в тенант-схеме; один тенант — много точек.
    """

    branch_id = models.PositiveIntegerField(
        unique=True,
        verbose_name='ID точки',
        help_text='Используется в QR-кодах и ссылках. Задаётся вручную.',
    )
    name = models.CharField(max_length=255, verbose_name='Название')
    description = models.TextField(
        blank=True, null=True,
        verbose_name='Описание',
        help_text='Для внутреннего пользования. Не отображается в приложении.',
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name='Активна',
        help_text='Неактивная точка скрыта для гостей.',
    )

    # ── Интеграция с кассой ────────────────────────────────────────────────

    # IIKO: organization UUID из личного кабинета
    iiko_organization_id = models.CharField(
        max_length=255,
        blank=True,
        verbose_name='IIKO Organization ID',
        help_text='UUID организации из ЛК iiko. Нужен для OLAP-отчётов по этой точке.',
    )

    # Dooglys: числовой branch ID
    dooglys_branch_id = models.PositiveIntegerField(
        blank=True,
        null=True,
        unique=True,
        verbose_name='Dooglys Branch ID',
        help_text='Числовой ID заведения из кабинета Dooglys.',
    )

    # Dooglys: строковый sale-point ID (nullable + unique → корректно в PostgreSQL)
    dooglys_sale_point_id = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        unique=True,
        verbose_name='Dooglys Sale Point ID',
        help_text='ID кассовой точки в Dooglys.',
    )

    story_image = models.ImageField(
        upload_to='branch/stories/',
        blank=True,
        null=True,
        verbose_name='Фото для сториса',
        help_text='Изображение-шаблон, которое гость видит перед публикацией VK-сторис.',
    )

    def save(self, *args, **kwargs):
        # Пустые строки → NULL, чтобы не нарушать unique-ограничение
        if not self.dooglys_sale_point_id:
            self.dooglys_sale_point_id = None
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = 'Торговая точка'
        verbose_name_plural = 'Торговые точки'
        ordering = ['name']


class BranchConfig(TimeStampedModel):
    """
    Публичные настройки точки: адрес, телефон, ссылки на карты.
    Создаётся автоматически при создании Branch (через сигнал).
    """

    branch = models.OneToOneField(
        Branch,
        on_delete=models.CASCADE,
        related_name='config',
        verbose_name='Торговая точка',
    )
    address = models.CharField(
        max_length=500,
        blank=True,
        verbose_name='Адрес',
        help_text='Отображается в приложении под названием точки.',
    )
    phone = models.CharField(
        max_length=50,
        blank=True,
        verbose_name='Телефон',
        help_text='Основной контактный номер точки.',
    )
    yandex_map = models.URLField(
        blank=True,
        verbose_name='Яндекс Карты',
        help_text='Ссылка на точку в Яндекс Картах.',
    )
    gis_map = models.URLField(
        blank=True,
        verbose_name='2ГИС',
        help_text='Ссылка на точку в 2ГИС.',
    )

    # ── Кастомные сообщения для гостей (override уровня точки) ────────────
    code_prompt_message = models.TextField(
        blank=True,
        default='',
        verbose_name='Подсказка про код монет (для этой точки)',
        help_text=(
            'Перезаписывает аналогичное поле в настройках сети ТОЛЬКО для этой точки. '
            'Оставьте пустым, чтобы использовать общий текст из настроек тенанта.'
        ),
    )
    quest_show_message = models.TextField(
        blank=True,
        default='',
        verbose_name='Подсказка про показ задания (для этой точки)',
        help_text='Перезаписывает текст подсказки в активации задания только для этой точки.',
    )

    # ── Окно выдачи подарка ко дню рождения (LU-13) ─────────────────
    birthday_window_days = models.PositiveSmallIntegerField(
        default=5,
        verbose_name='Окно подарка ДР (±дней)',
        help_text=(
            'Сколько дней до и после ДР гость может получить подарок. '
            '0 — только день-в-день; 1 — ±1 день; 5 — стандартно ±5 дней; '
            '14 — две недели и т.д. Каждая точка настраивается независимо.'
        ),
    )

    def __str__(self):
        return f'Настройки: {self.branch.name}'

    class Meta:
        verbose_name = 'Настройки точки'
        verbose_name_plural = 'Настройки точек'


class ClientBranch(TimeStampedModel):
    """
    Профиль гостя в конкретной торговой точке.
    Позволяет хранить данные, специфичные для связки гость–точка
    (дата рождения, флаг сотрудника, заметки менеджера).
    """

    client = models.ForeignKey(
        'guest.Client',
        on_delete=models.CASCADE,
        verbose_name='Гость',
        related_name='branch_profiles',
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        verbose_name='Торговая точка',
        related_name='clients',
    )
    birth_date = models.DateField(
        blank=True,
        null=True,
        verbose_name='Дата рождения',
        help_text='Используется для поздравлений и персональных акций.',
    )
    birth_date_set_at = models.DateField(
        blank=True,
        null=True,
        editable=False,
        verbose_name='Дата установки ДР',
        help_text=(
            'Устанавливается автоматически при первой записи birth_date. '
            'Используется для защиты от злоупотреблений: ДР-приз доступен '
            'только если дата установлена не менее 30 дней назад.'
        ),
    )
    invited_by = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invited_guests',
        verbose_name='Пригласил',
        help_text='ClientBranch гостя, который пригласил через сторис. Устанавливается только при первой регистрации.',
    )
    is_employee = models.BooleanField(
        default=False,
        verbose_name='Сотрудник',
        help_text='Сотрудники исключаются из статистики и акций.',
    )
    notes = models.TextField(
        blank=True,
        verbose_name='Заметки',
        help_text='Внутренние заметки. Гость их не видит.',
    )

    def save(self, *args, **kwargs):
        if self.birth_date and not self.birth_date_set_at:
            self.birth_date_set_at = timezone.localdate()
        super().save(*args, **kwargs)

    @property
    def coins_balance(self) -> int:
        """Текущий баланс монет: сумма INCOME − сумма EXPENSE по всем транзакциям."""
        result = self.transactions.aggregate(
            income=Sum('amount', filter=Q(type='income')),
            expense=Sum('amount', filter=Q(type='expense')),
        )
        return (result['income'] or 0) - (result['expense'] or 0)

    def __str__(self):
        return f'{self.client} @ {self.branch.name}'

    class Meta:
        unique_together = ('client', 'branch')
        verbose_name = 'Профиль гостя'
        verbose_name_plural = 'Профили гостей'
        ordering = ['-created_at']


# ── ClientBranchVisit ─────────────────────────────────────────────────────────

class ClientBranchVisit(models.Model):
    """
    Запись визита гостя при сканировании QR-кода.

    Записывается атомарно через record_visit(). Cooldown предотвращает
    фантомные визиты: повторный вход в мини-приложение в течение
    COOLDOWN_HOURS засчитывается как тот же визит, новая запись не создаётся.

    SELECT FOR UPDATE на ClientBranch исключает гонку потоков при
    одновременных запросах (например, двойное нажатие у гостя).

    Используется для:
      - метрик посещаемости (уникальные визиты / день, неделю, месяц)
      - RF-анализа (recency = дней с последнего визита)
      - тепловых карт (день недели × час)
      - retention-анализа (возвращаемость гостей)
    """

    COOLDOWN_HOURS = 6

    client = models.ForeignKey(
        ClientBranch,
        on_delete=models.CASCADE,
        related_name='visits',
        verbose_name='Гость',
    )
    visited_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name='Время визита',
    )

    # ── Business methods ──────────────────────────────────────────────────────

    @classmethod
    @transaction.atomic
    def record_visit(cls, client_branch: 'ClientBranch') -> 'ClientBranchVisit | None':
        """
        Атомарно записывает визит, если cooldown истёк.

        SELECT FOR UPDATE блокирует строку ClientBranch — только один поток
        пройдёт проверку и создаст запись при конкурентных вызовах.

        Returns:
            ClientBranchVisit — новая запись визита.
            None              — cooldown ещё не истёк, визит не засчитан.
        """
        locked = ClientBranch.objects.select_for_update().get(pk=client_branch.pk)
        threshold = timezone.now() - timedelta(hours=cls.COOLDOWN_HOURS)
        if cls.objects.filter(client=locked, visited_at__gte=threshold).exists():
            return None
        return cls.objects.create(client=locked)

    def __str__(self):
        return f'{self.client} @ {self.visited_at:%d.%m.%Y %H:%M}'

    class Meta:
        verbose_name = 'Визит гостя'
        verbose_name_plural = 'Визиты гостей'
        ordering = ['-visited_at']
        indexes = [
            # Основной запрос в record_visit: последний визит конкретного гостя
            models.Index(
                fields=['client', '-visited_at'],
                name='visit_client_time_idx',
            ),
            # Агрегации по времени: визиты за период, тепловые карты
            models.Index(
                fields=['visited_at'],
                name='visit_time_idx',
            ),
        ]


# ── DailyCode ─────────────────────────────────────────────────────────────────

# Кодовые сутки начинаются в 03:00 MSK — в это же время cron перегенерирует коды.
# До 03:00 действителен код, сгенерированный накануне; после 03:00 — новый.
DAILY_CODE_ROLLOVER_HOUR = 3


def current_code_date():
    """
    Дата, на которую сейчас действует код дня.

    Кодовые сутки сдвинуты на DAILY_CODE_ROLLOVER_HOUR часов вперёд относительно
    календарных, чтобы окно 00:00–03:00 не оставалось без кода
    (cron генерации запускается в 03:00).
    """
    return (timezone.localtime() - timedelta(hours=DAILY_CODE_ROLLOVER_HOUR)).date()


class DailyCodePurpose(models.TextChoices):
    GAME     = 'game',     'Игра'
    QUEST    = 'quest',    'Квест'
    BIRTHDAY = 'birthday', 'День рождения'


class DailyCodeGeneratedBy(models.TextChoices):
    AUTO   = 'auto',   'Автоматически (cron)'
    MANUAL = 'manual', 'Вручную (через мобильное API)'


class DailyCode(TimeStampedModel):
    """
    Код дня — 5-значный код, уникальный для каждой (точка, назначение, дата).

    Назначения:
      game     — требуется начиная с 3-й игры для подтверждения выигрыша
      quest    — требуется для засчитывания выполненного квеста
      birthday — требуется для получения подарка на день рождения

    Доставочный код вынесен отдельно (Delivery.short_code).

    Генерируется ежедневно через Celery (пока — вручную через admin-action).
    """

    branch = models.ForeignKey(
        Branch,
        on_delete=models.CASCADE,
        related_name='daily_codes',
        verbose_name='Торговая точка',
    )
    purpose = models.CharField(
        max_length=20,
        choices=DailyCodePurpose,
        verbose_name='Назначение',
    )
    code = models.CharField(
        max_length=5,
        verbose_name='Код',
        help_text='5-значный цифровой код.',
    )
    valid_date = models.DateField(
        db_index=True,
        verbose_name='Дата',
        help_text='День, на который действует код.',
    )
    generated_by = models.CharField(
        'Источник генерации',
        max_length=8,
        choices=DailyCodeGeneratedBy.choices,
        default=DailyCodeGeneratedBy.AUTO,
        help_text='Cron (auto) или ручной триггер из мобильного приложения (manual).',
    )

    def __str__(self):
        return f'{self.branch} / {self.get_purpose_display()} / {self.valid_date}: {self.code}'

    class Meta:
        unique_together = ('branch', 'purpose', 'valid_date')
        verbose_name = 'Код дня'
        verbose_name_plural = 'Коды дня'
        ordering = ['-valid_date', 'branch__name', 'purpose']


# ── Cooldown ──────────────────────────────────────────────────────────────────

class CooldownFeature(models.TextChoices):
    GAME      = 'game',      'Игра'
    INVENTORY = 'inventory', 'Инвентарь'
    SHOP      = 'shop',      'Магазин'
    QUEST     = 'quest',     'Квесты'


class Cooldown(TimeStampedModel):
    """
    Перезарядка гостя для конкретной фичи.

    Одна запись на пару (client, feature). При каждом срабатывании
    запись обновляется (не создаётся новая) — через метод activate().

    Жизненный цикл:
      is_active=True  → гость заблокирован, expires_at > now()
      is_active=False → перезарядка истекла, можно использовать фичу

    Фичи:
      game      — после игры, 18 ч
      inventory — после активации приза из инвентаря, 18 ч
      shop      — после покупки из магазина за баллы, 18 ч
      quest     — после активации квеста (независимо от результата), 18 ч
    """

    client = models.ForeignKey(
        ClientBranch,
        on_delete=models.CASCADE,
        related_name='cooldowns',
        verbose_name='Гость',
    )
    feature = models.CharField(
        max_length=20,
        choices=CooldownFeature,
        verbose_name='Функция',
    )
    last_activated_at = models.DateTimeField(
        verbose_name='Последняя активация',
    )
    duration = models.PositiveIntegerField(
        default=18,
        verbose_name='Длительность (ч)',
        help_text='Часов после активации, в течение которых фича заблокирована.',
    )
    expires_at = models.DateTimeField(
        verbose_name='Разблокируется',
        help_text='Устанавливается автоматически: last_activated_at + duration ч.',
    )

    # ── Computed state ────────────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        """True means the cooldown is still running — feature is blocked."""
        return timezone.now() < self.expires_at

    @property
    def remaining(self) -> timedelta | None:
        """Time left in the cooldown, or None if already expired."""
        delta = self.expires_at - timezone.now()
        return delta if delta.total_seconds() > 0 else None

    # ── Business methods ──────────────────────────────────────────────────────

    def activate(self) -> None:
        """Restart the cooldown clock (called when the feature is used)."""
        self.last_activated_at = timezone.now()
        self.expires_at = self.last_activated_at + timedelta(hours=self.duration)
        self.save(update_fields=['last_activated_at', 'expires_at'])

    # ── Meta ──────────────────────────────────────────────────────────────────

    def __str__(self):
        state = '🔒' if self.is_active else '✅'
        return f'{state} {self.client} / {self.get_feature_display()}'

    class Meta:
        unique_together = ('client', 'feature')
        verbose_name = 'Перезарядка'
        verbose_name_plural = 'Перезарядки'
        ordering = ['expires_at']
        indexes = [
            models.Index(
                fields=['client', 'feature'],
                name='cooldown_client_feature_idx',
            ),
            models.Index(
                fields=['expires_at'],
                name='cooldown_expires_idx',
            ),
            models.Index(
                fields=['feature', 'expires_at'],
                name='cooldown_feature_exp_idx',
            ),
        ]


# ── CoinTransaction ───────────────────────────────────────────────────────────

class TransactionType(models.TextChoices):
    INCOME  = 'income',  'Начисление'
    EXPENSE = 'expense', 'Списание'


class TransactionSource(models.TextChoices):
    GAME     = 'game',     'Игра'
    QUEST    = 'quest',    'Квест'
    SHOP     = 'shop',     'Магазин'
    BIRTHDAY = 'birthday', 'День рождения'
    DELIVERY = 'delivery', 'Доставка'
    MANUAL   = 'manual',   'Вручную'


class CoinTransactionManager(models.Manager):

    @transaction.atomic
    def create_transfer(
        self,
        client_branch: 'ClientBranch',
        amount: int,
        type: str,
        source: str,
        description: str = '',
    ) -> 'CoinTransaction':
        """
        Атомарно создаёт транзакцию.

        Перед записью блокирует строку ClientBranch (SELECT FOR UPDATE),
        чтобы исключить race condition при одновременном списании.
        Для EXPENSE проверяет, что баланс достаточен.

        Raises:
            ValidationError — если не хватает монет для списания.
        """
        locked = ClientBranch.objects.select_for_update().get(pk=client_branch.pk)

        if type == TransactionType.EXPENSE:
            if locked.coins_balance < amount:
                raise ValidationError({'amount': 'Недостаточно монет для списания.'})

        return self.create(
            client=locked,
            type=type,
            source=source,
            amount=amount,
            description=description,
        )


class CoinTransaction(models.Model):
    """
    Неизменяемая запись движения монет гостя.

    Баланс не хранится явно — вычисляется как SUM(INCOME) − SUM(EXPENSE)
    через ClientBranch.coins_balance или аннотацию queryset.

    Создавать через:
        CoinTransaction.objects.create_transfer(client_branch, amount, type, source)

    Удаление и редактирование запрещены: для корректировки создайте
    новую транзакцию противоположного типа.
    """

    client = models.ForeignKey(
        ClientBranch,
        on_delete=models.CASCADE,
        related_name='transactions',
        verbose_name='Гость',
    )
    type = models.CharField(
        max_length=10,
        choices=TransactionType,
        verbose_name='Тип',
    )
    source = models.CharField(
        max_length=20,
        choices=TransactionSource,
        verbose_name='Источник',
    )
    amount = models.PositiveIntegerField(verbose_name='Сумма (монет)')
    description = models.CharField(
        max_length=255,
        blank=True,
        verbose_name='Комментарий',
        help_text='Для ручных операций — причина. Для автоматических — ID связанного объекта.',
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        verbose_name='Дата',
    )

    objects = CoinTransactionManager()

    def delete(self, *args, **kwargs):
        raise NotImplementedError(
            'Транзакции не удаляются. Для корректировки создайте обратную транзакцию.'
        )

    def __str__(self):
        sign = '+' if self.type == TransactionType.INCOME else '−'
        return f'{sign}{self.amount} ★ | {self.client} | {self.get_source_display()}'

    class Meta:
        verbose_name = 'Транзакция монет'
        verbose_name_plural = 'Транзакции монет'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', 'created_at'], name='tx_client_time_idx'),
            models.Index(fields=['client', 'type'],       name='tx_client_type_idx'),
            models.Index(fields=['source', 'created_at'], name='tx_source_time_idx'),
            models.Index(fields=['type', 'created_at'],   name='tx_type_time_idx'),
        ]


# ── ClientVKStatus ────────────────────────────────────────────────────────────

class ClientVKStatus(models.Model):
    """
    VK-статус гостя: подписка на сообщество и рассылку.

    Создаётся при первом входе гостя в мини-приложение (через sync()).
    Обновляется при каждом входе.

    via_app-поля кодируют источник подписки:
      None  — ещё не подписан
      False — был подписан до нашего приложения (pre-existing)
      True  — подписался через наше приложение

    Назначение:
      - Attribution: сколько подписок принесло приложение vs уже имели
      - Сегментация: «лояльные» (via_app) vs «пришедшие с VK»
      - Условия акций: «бонус за подписку» только для via_app=True
    """

    client = models.OneToOneField(
        ClientBranch,
        on_delete=models.CASCADE,
        related_name='vk_status',
        verbose_name='Гость',
    )

    # ── Сообщество ────────────────────────────────────────────────────────────

    is_community_member = models.BooleanField(
        default=False,
        verbose_name='Подписан на сообщество',
    )
    community_joined_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Дата подписки (сообщество)',
        help_text='Когда мы впервые зафиксировали подписку. NULL — ещё не подписан.',
    )
    community_via_app = models.BooleanField(
        null=True,
        blank=True,
        verbose_name='Через приложение (сообщество)',
        help_text='null — не подписан; false — до приложения; true — через приложение.',
    )

    # ── Рассылка ──────────────────────────────────────────────────────────────

    is_newsletter_subscriber = models.BooleanField(
        default=False,
        verbose_name='Подписан на рассылку',
    )
    newsletter_joined_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Дата подписки (рассылка)',
        help_text='Когда мы впервые зафиксировали подписку. NULL — ещё не подписан.',
    )
    newsletter_via_app = models.BooleanField(
        null=True,
        blank=True,
        verbose_name='Через приложение (рассылка)',
        help_text='null — не подписан; false — до приложения; true — через приложение.',
    )

    # ── Сторис ────────────────────────────────────────────────────────────────

    is_story_uploaded = models.BooleanField(
        default=False,
        verbose_name='Опубликовал сторис',
        help_text='True — гость хотя бы раз опубликовал наш шаблон сторис через приложение.',
    )
    story_uploaded_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name='Дата публикации сторис',
        help_text='Когда впервые опубликовал сторис через приложение. NULL — ни разу.',
    )

    # ── Служебное ─────────────────────────────────────────────────────────────

    checked_at = models.DateTimeField(
        auto_now=True,
        verbose_name='Последняя проверка',
        help_text='Когда последний раз синхронизировали статус с VK API.',
    )

    # ── Business methods ──────────────────────────────────────────────────────

    @classmethod
    @transaction.atomic
    def sync(
        cls,
        client_branch: 'ClientBranch',
        *,
        is_member: bool,
        is_subscriber: bool,
    ) -> 'ClientVKStatus':
        """
        Создаёт или обновляет статус на основе данных VK API.

        При первичном создании:
          - уже подписан  → via_app=False (pre-existing, существовал до приложения)
          - не подписан   → via_app=None  (ещё не подписан)

        При обновлении:
          - новая подписка → joined_at фиксируется; via_app=None до явного
            вызова mark_subscribed() — источник подписки пока неизвестен
          - отписался     → сбрасывает joined_at и via_app в None
        """
        now = timezone.now()

        obj, created = cls.objects.get_or_create(
            client=client_branch,
            defaults={
                'is_community_member':     is_member,
                'community_joined_at':     now if is_member else None,
                'community_via_app':       False if is_member else None,
                'is_newsletter_subscriber': is_subscriber,
                'newsletter_joined_at':    now if is_subscriber else None,
                'newsletter_via_app':      False if is_subscriber else None,
            },
        )

        if created:
            return obj

        update_fields = ['checked_at']

        if is_member and not obj.is_community_member:
            obj.is_community_member = True
            obj.community_joined_at = now
            # via_app=None: подписался вне приложения или параллельно —
            # источник уточнится через mark_subscribed()
            update_fields += ['is_community_member', 'community_joined_at']
        elif not is_member and obj.is_community_member:
            obj.is_community_member = False
            obj.community_joined_at = None
            obj.community_via_app = None
            update_fields += ['is_community_member', 'community_joined_at', 'community_via_app']

        if is_subscriber and not obj.is_newsletter_subscriber:
            obj.is_newsletter_subscriber = True
            obj.newsletter_joined_at = now
            update_fields += ['is_newsletter_subscriber', 'newsletter_joined_at']
        elif not is_subscriber and obj.is_newsletter_subscriber:
            obj.is_newsletter_subscriber = False
            obj.newsletter_joined_at = None
            obj.newsletter_via_app = None
            update_fields += ['is_newsletter_subscriber', 'newsletter_joined_at', 'newsletter_via_app']

        obj.save(update_fields=update_fields)
        return obj

    @transaction.atomic
    def mark_subscribed(
        self,
        *,
        community: bool = False,
        newsletter: bool = False,
    ) -> None:
        """
        Фиксирует подписку, совершённую прямо в мини-приложении (via_app=True).

        Вызывается когда гость нажал «Подписаться» в онбординге приложения.
        Идемпотентен: повторный вызов для уже подписанного канала ничего не делает.
        """
        now = timezone.now()
        update_fields = []

        if community:
            if not self.is_community_member:
                self.is_community_member = True
                self.community_joined_at = now
                update_fields += ['is_community_member', 'community_joined_at']
            # Ставим via_app=True только если значение None (источник ещё неизвестен).
            # False = подписан до приложения (pre-existing) — не перебиваем.
            # Это также покрывает race condition: group_join Callback не трогает via_app,
            # оставляет None, и PATCH успешно проставляет True.
            if self.community_via_app is None:
                self.community_via_app = True
                update_fields += ['community_via_app']

        if newsletter:
            if not self.is_newsletter_subscriber:
                self.is_newsletter_subscriber = True
                self.newsletter_joined_at = now
                update_fields += ['is_newsletter_subscriber', 'newsletter_joined_at']
            # Аналогично для рассылки
            if self.newsletter_via_app is None:
                self.newsletter_via_app = True
                update_fields += ['newsletter_via_app']

        if update_fields:
            self.save(update_fields=update_fields)

    @transaction.atomic
    def mark_story_uploaded(self) -> bool:
        """
        Фиксирует публикацию нашего шаблона сторис через мини-приложение.

        Идемпотентен: повторный вызов ничего не делает и возвращает False.
        При первой публикации устанавливает is_story_uploaded=True и фиксирует
        story_uploaded_at — аналогично mark_subscribed() для подписок.

        Returns:
            True  — первая публикация, статус обновлён.
            False — уже публиковал ранее, ничего не изменилось.
        """
        if self.is_story_uploaded:
            return False
        self.is_story_uploaded = True
        self.story_uploaded_at = timezone.now()
        self.save(update_fields=['is_story_uploaded', 'story_uploaded_at'])
        return True

    def __str__(self):
        parts = []
        if self.is_community_member:
            src = ' (прил.)' if self.community_via_app else ' (до прил.)'
            parts.append(f'сообщ.{src}')
        if self.is_newsletter_subscriber:
            src = ' (прил.)' if self.newsletter_via_app else ' (до прил.)'
            parts.append(f'рассылка{src}')
        if self.is_story_uploaded:
            parts.append('сторис')
        return f'{self.client}: {", ".join(parts) or "—"}'

    class Meta:
        verbose_name = 'VK-статус гостя'
        verbose_name_plural = 'VK-статусы гостей'
        indexes = [
            # Сегментация: подписан / не подписан
            models.Index(fields=['is_community_member'],      name='vk_community_idx'),
            models.Index(fields=['is_newsletter_subscriber'], name='vk_newsletter_idx'),
            # Attribution: via_app vs pre-existing vs null
            models.Index(fields=['community_via_app'],   name='vk_comm_via_app_idx'),
            models.Index(fields=['newsletter_via_app'],  name='vk_news_via_app_idx'),
            # Сторис: аналитика по дате публикации
            models.Index(fields=['is_story_uploaded'],   name='vk_story_idx'),
            models.Index(fields=['story_uploaded_at'],   name='vk_story_at_idx'),
        ]


class Promotions(TimeStampedModel):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, verbose_name='Ресторан')

    title = models.CharField(max_length=100, verbose_name='Название', help_text='Заголовок акции, который видит гость в приложении.')
    discount = models.CharField(max_length=500, verbose_name='Акция', help_text='Описание условий акции, например: «−20% на всё меню».')
    dates = models.CharField(max_length=255, verbose_name='Даты', help_text='Период действия, например: «01.06 – 30.06».')
    images = models.ImageField(upload_to='promotions', verbose_name='Фото', help_text='Баннер акции, отображается в приложении.')

    def __str__(self):
        return self.title

    class Meta:
        verbose_name = 'Скидка'
        verbose_name_plural = 'Скидки и промоакции'


# ── Testimonials (reviews + VK messages) ──────────────────────────────────────

class TestimonialConversation(TimeStampedModel):
    """
    Один тред на уникального отправителя (идентификация по vk_sender_id).

    Объединяет все сообщения от одного пользователя в одну "переписку":
    — отзывы из мини-приложения (с оценкой, телефоном, столиком)
    — входящие сообщения из ВК-группы
    — ответы администратора
    """

    class Sentiment(models.TextChoices):
        POSITIVE           = 'POSITIVE',           'Позитивный'
        NEGATIVE           = 'NEGATIVE',           'Негативный'
        PARTIALLY_NEGATIVE = 'PARTIALLY_NEGATIVE', 'Частично негативный'
        NEUTRAL            = 'NEUTRAL',            'Нейтральный'
        SPAM               = 'SPAM',               'Спам / Не по теме'
        WAITING            = 'WAITING',            'Ожидает анализа'

    branch = models.ForeignKey(
        Branch,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='testimonials',
        verbose_name='Торговая точка',
    )
    client = models.ForeignKey(
        'ClientBranch',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='testimonials',
        verbose_name='Гость (если зарегистрирован)',
    )
    vk_guest = models.ForeignKey(
        'guest.Client',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='vk_conversations',
        verbose_name='VK гость (из группы, без точки)',
    )
    # Primary key for thread matching — VK user ID as a string
    vk_sender_id = models.CharField(
        'VK ID отправителя',
        max_length=50,
        blank=True,
        db_index=True,
    )

    sentiment  = models.CharField(
        'Тональность (ИИ)',
        max_length=20,
        choices=Sentiment.choices,
        default=Sentiment.WAITING,
    )
    ai_comment = models.TextField('Комментарий ИИ', blank=True)
    ai_draft   = models.TextField(
        'AI-черновик ответа', blank=True,
        help_text='Сгенерированный AI вариант ответа на отзыв (для одобрения админом).',
    )
    ai_draft_rejected = models.BooleanField(
        'AI-черновик отклонён', default=False,
        help_text='Админ отклонил черновик — больше не предлагать.',
    )

    has_unread      = models.BooleanField('Есть непрочитанные', default=True)
    is_replied      = models.BooleanField('Ответ отправлен', default=False)
    last_message_at = models.DateTimeField(
        'Последнее сообщение', null=True, blank=True, db_index=True,
    )
    last_reminded_at = models.DateTimeField(
        'Последнее напоминание', null=True, blank=True,
        help_text='Когда последний раз отправляли push-напоминание о неотвеченном черновике. Не чаще 1 раза в сутки.',
    )

    def __str__(self):
        ident = str(self.client) if self.client_id else (f'VK {self.vk_sender_id}' if self.vk_sender_id else '?')
        branch_name = self.branch.name if self.branch_id else 'ВК группа'
        return f'{ident} — {branch_name}'

    class Meta:
        verbose_name = 'Отзыв / Обращение'
        verbose_name_plural = 'Отзывы и Обращения'
        ordering = ['-last_message_at', '-created_at']
        indexes = [
            models.Index(fields=['branch', 'has_unread'],   name='testimonial_unread_idx'),
            models.Index(fields=['branch', 'vk_sender_id'], name='testimonial_sender_idx'),
        ]


class TestimonialMessage(models.Model):
    """Одно сообщение внутри тред-переписки."""

    class Source(models.TextChoices):
        APP         = 'APP',         'Из приложения'
        VK_MESSAGE  = 'VK_MESSAGE',  'ВК сообщение'
        ADMIN_REPLY = 'ADMIN_REPLY', 'Ответ администратора'

    conversation = models.ForeignKey(
        TestimonialConversation,
        on_delete=models.CASCADE,
        related_name='messages',
        verbose_name='Переписка',
    )
    source = models.CharField('Источник', max_length=20, choices=Source.choices)
    text   = models.TextField('Текст сообщения')

    # ── APP-only ───────────────────────────────────────────────────────────────
    rating       = models.PositiveSmallIntegerField('Оценка (1–5)', null=True, blank=True)
    phone        = models.CharField('Телефон', max_length=20, blank=True)
    table_number = models.PositiveIntegerField('Столик', null=True, blank=True)

    # ── VK-only (deduplication) ────────────────────────────────────────────────
    vk_message_id = models.CharField(
        'VK ID сообщения', max_length=50, blank=True, db_index=True,
    )

    # ── Read tracking (for ADMIN_REPLY messages) ───────────────────────────────
    read_at = models.DateTimeField(
        'Прочитано', null=True, blank=True,
        help_text='Заполняется Celery-задачей при обнаружении прочтения через VK API.',
    )

    created_at = models.DateTimeField('Время', auto_now_add=True, db_index=True)

    def __str__(self):
        return f'[{self.get_source_display()}] {self.text[:60]}'

    class Meta:
        verbose_name = 'Сообщение'
        verbose_name_plural = 'Сообщения'
        ordering = ['created_at']


# ── ReviewAutoReplyConfig ─────────────────────────────────────────────────────


class ReviewAutoReplyConfig(models.Model):
    """
    Настройки AI-автоответов на отзывы (одна запись на тенант — синглтон).

    Используется мобильным API `/api/v1/analytics/auto-reply/settings/`.
    SPAM-тональность всегда выключена (это не AI-параметр), поэтому не хранится.
    """

    class Reminder(models.IntegerChoices):
        M30  = 30,  '30 минут'
        M60  = 60,  '1 час'
        M180 = 180, '3 часа'
        M720 = 720, '12 часов'

    class Tone(models.TextChoices):
        FORMAL   = 'formal',   'Официальный'
        FRIENDLY = 'friendly', 'Дружелюбный'
        NEUTRAL  = 'neutral',  'Нейтральный'

    enabled = models.BooleanField('Включено', default=True)

    sentiment_positive           = models.BooleanField('Позитивные', default=True)
    sentiment_negative           = models.BooleanField('Негативные', default=True)
    sentiment_partially_negative = models.BooleanField('Частично негативные', default=True)
    sentiment_neutral            = models.BooleanField('Нейтральные', default=True)
    sentiment_pending            = models.BooleanField('Ожидают анализа', default=True)

    branch_enabled = models.JSONField(
        'Включено по точкам',
        default=dict, blank=True,
        help_text='Карта branch_id (str) → bool. Отсутствующие точки наследуют общий enabled.',
    )

    reminder_minutes = models.PositiveIntegerField(
        'Напоминание',
        choices=Reminder.choices,
        default=Reminder.M180,
    )
    ai_tone = models.CharField(
        'Тон AI',
        max_length=10,
        choices=Tone.choices,
        default=Tone.FRIENDLY,
    )

    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    @classmethod
    def get_singleton(cls) -> 'ReviewAutoReplyConfig':
        """Возвращает (создавая при необходимости) единственную запись текущего тенанта."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def to_mobile_dict(self) -> dict:
        """Сериализация в формат, который ждёт мобильное приложение."""
        return {
            'enabled': self.enabled,
            'sentiment_enabled': {
                'POSITIVE':           self.sentiment_positive,
                'NEGATIVE':           self.sentiment_negative,
                'PARTIALLY_NEGATIVE': self.sentiment_partially_negative,
                'NEUTRAL':            self.sentiment_neutral,
                'PENDING':            self.sentiment_pending,
            },
            'branch_enabled':   self.branch_enabled or {},
            'reminder_minutes': self.reminder_minutes,
            'ai_tone':          self.ai_tone,
        }

    def __str__(self):
        return 'Авто-ответы AI' + (' (вкл.)' if self.enabled else ' (выкл.)')

    class Meta:
        verbose_name = 'Авто-ответы AI: настройки'
        verbose_name_plural = 'Авто-ответы AI: настройки'


# ── AuditLog ──────────────────────────────────────────────────────────────────


class AuditLog(TimeStampedModel):
    """
    Журнал действий сотрудников/админов через мобильное API.

    Источник для GET /api/v1/audit-log/. Заполняется хук-функцией
    `log_audit()` (см. apps.tenant.branch.audit) — её вызывают view'ы,
    меняющие состояние тенанта (пороги RF, ручные коды, ответы на отзывы и т.д.).
    """

    class Action(models.TextChoices):
        COIN_ADJUST       = 'COIN_ADJUST',       'Корректировка баланса'
        REVIEW_REPLY      = 'REVIEW_REPLY',      'Ответ на отзыв'
        REVIEW_RESOLVE    = 'REVIEW_RESOLVE',    'Закрытие отзыва'
        BROADCAST_SEND    = 'BROADCAST_SEND',    'Рассылка'
        PRODUCT_CREATE    = 'PRODUCT_CREATE',    'Создан подарок'
        PRODUCT_UPDATE    = 'PRODUCT_UPDATE',    'Изменён подарок'
        PRODUCT_DELETE    = 'PRODUCT_DELETE',    'Удалён подарок'
        QUEST_CREATE      = 'QUEST_CREATE',      'Создан квест'
        QUEST_UPDATE      = 'QUEST_UPDATE',      'Изменён квест'
        QUEST_DELETE      = 'QUEST_DELETE',      'Удалён квест'
        PROMO_CREATE      = 'PROMO_CREATE',      'Создана акция'
        PROMO_UPDATE      = 'PROMO_UPDATE',      'Изменена акция'
        PROMO_DELETE      = 'PROMO_DELETE',      'Удалена акция'
        STAFF_INVITE      = 'STAFF_INVITE',      'Приглашён сотрудник'
        STAFF_TOGGLE      = 'STAFF_TOGGLE',      'Изменён статус сотрудника'
        STAFF_PERMS       = 'STAFF_PERMS',       'Изменены права'
        THRESHOLDS_SAVE   = 'THRESHOLDS_SAVE',   'Сохранены пороги RF'
        AUTO_REPLY_SAVE   = 'AUTO_REPLY_SAVE',   'Изменены настройки AI-ответов'
        DAILY_CODE_MANUAL = 'DAILY_CODE_MANUAL', 'Ручной код дня'
        AUTH_LOGIN        = 'AUTH_LOGIN',        'Вход'
        AUTH_LOGOUT       = 'AUTH_LOGOUT',       'Выход'

    staff_id = models.PositiveBigIntegerField(
        'ID сотрудника', null=True, blank=True, db_index=True,
        help_text='Логический ID User в shared-схеме. Не FK — иначе ломается удаление User из public.',
    )
    staff_name = models.CharField(
        'Имя сотрудника (снимок)',
        max_length=255, blank=True,
        help_text='Сохраняется на момент действия — переживает удаление User.',
    )

    @property
    def staff(self):
        """Совместимость: старый код может ожидать поле .staff."""
        if not self.staff_id:
            return None
        from django.contrib.auth import get_user_model
        return get_user_model().objects.filter(pk=self.staff_id).first()
    action_type = models.CharField(
        'Действие', max_length=32, choices=Action.choices, db_index=True,
    )
    target_type = models.CharField(
        'Тип объекта', max_length=32, blank=True,
        help_text='guest | review | product | quest | promotion | staff | broadcast | thresholds | daily_code',
    )
    target_id = models.CharField(
        'ID объекта', max_length=64, blank=True,
    )
    target_label = models.CharField(
        'Метка объекта', max_length=255, blank=True,
        help_text='Например, имя гостя или название подарка — сохраняется как снимок.',
    )
    details = models.TextField('Детали', blank=True)
    delta = models.JSONField('Изменения (before/after)', default=dict, blank=True)

    def __str__(self):
        return f'{self.created_at:%Y-%m-%d %H:%M} {self.staff_name or "?"} → {self.get_action_type_display()}'

    class Meta:
        verbose_name = 'Запись аудит-лога'
        verbose_name_plural = 'Аудит-лог'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['staff_id', '-created_at'], name='audit_staff_created_idx'),
            models.Index(fields=['action_type', '-created_at'], name='audit_action_created_idx'),
        ]


# ── SupportChatMessage ────────────────────────────────────────────────────────


class SupportChatMessage(TimeStampedModel):
    """
    Сообщение в чате с менеджером LoyalUP (поддержка/менеджер аккаунта).

    sender=user — отправлено сотрудником клиента через мобильное приложение.
    sender=manager — ответ менеджера LoyalUP (приходит из CRM/админки).
    """

    class Sender(models.TextChoices):
        USER    = 'user',    'Пользователь'
        MANAGER = 'manager', 'Менеджер'

    sender = models.CharField('Отправитель', max_length=8, choices=Sender.choices, db_index=True)
    author_id = models.PositiveBigIntegerField(
        'ID автора (если внутри платформы)', null=True, blank=True, db_index=True,
        help_text='Логический ID User в shared-схеме. Не FK — иначе ломается удаление User.',
    )
    text = models.TextField('Текст', blank=True)
    read_at = models.DateTimeField(
        'Прочитано', null=True, blank=True,
        help_text='Заполняется получателем при первом GET после доставки.',
    )

    def __str__(self):
        return f'[{self.sender}] {self.text[:60]}'

    class Meta:
        verbose_name = 'Сообщение в чате с менеджером'
        verbose_name_plural = 'Чат с менеджером'
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['created_at'], name='supchat_created_idx'),
            models.Index(fields=['sender', '-created_at'], name='supchat_sender_idx'),
        ]


# ── StaffProfile ──────────────────────────────────────────────────────────────


class StaffProfile(TimeStampedModel):
    """
    Профиль сотрудника в текущем тенанте.

    Хранит per-tenant поля, которых нет в shared `User` модели:
    телефон, JSON-карту прав (14 флагов из мобильного API §11),
    список точек, к которым у сотрудника есть доступ, отметку
    «последний вход» (через мобильное API).

    Создаётся лениво при первом GET /api/v1/staff/ или явно при invite.
    """

    user_id = models.PositiveBigIntegerField(
        'ID пользователя', unique=True,
        help_text='Логический ID User в shared-схеме. Не FK — иначе ломается удаление User.',
    )
    phone = models.CharField('Телефон', max_length=32, blank=True)

    @property
    def user(self):
        """Совместимость: старый код может ожидать поле .user."""
        from django.contrib.auth import get_user_model
        return get_user_model().objects.filter(pk=self.user_id).first()
    permissions = models.JSONField(
        'Права', default=dict, blank=True,
        help_text='Карта 14 флагов: see_analytics, edit_thresholds, manage_staff и т.д.',
    )
    branch_access = models.ManyToManyField(
        'branch.Branch',
        blank=True,
        related_name='staff_profiles',
        verbose_name='Доступ к точкам',
        help_text='Пустой список = доступ ко всем точкам тенанта.',
    )
    last_active_at = models.DateTimeField(
        'Последний вход', null=True, blank=True,
        help_text='Обновляется при каждом запросе через мобильное API.',
    )
    invitation_token = models.CharField(
        'Токен приглашения', max_length=64, blank=True,
        help_text='Одноразовая ссылка для установки пароля. Очищается после первого входа.',
    )

    def __str__(self):
        return f'Профиль: {self.user}'

    class Meta:
        verbose_name = 'Профиль сотрудника'
        verbose_name_plural = 'Профили сотрудников'
