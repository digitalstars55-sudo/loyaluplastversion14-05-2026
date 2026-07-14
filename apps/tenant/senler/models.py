from django.core.exceptions import ValidationError
from django.db import models

from apps.shared.base import TimeStampedModel


# ── Choices ───────────────────────────────────────────────────────────────────

class AutoBroadcastType(models.TextChoices):
    BIRTHDAY_7_DAYS  = 'birthday_7d',   'За 7 дней до дня рождения'
    BIRTHDAY_1_DAY   = 'birthday_1d',   'За 1 день до дня рождения'
    BIRTHDAY         = 'birthday',      'День рождения'
    AFTER_GAME_3H    = 'after_game_3h', 'Через 3 часа после игры'
    GIFT_NOT_CLAIMED = 'gift_not_claimed', 'Подарок из сториз/сайта не забран'
    # ── Фаза 2: события, где «через сколько дней» задаёт сам маркетолог ────────
    # (задержка берётся из AutoBroadcastRule.delay_days — одно событие, сколько
    #  угодно правил: «не приходил 30 дней», «не приходил 60 дней» и т.д.)
    NO_VISIT_DAYS    = 'no_visit_days',   'Не приходил N дней (реактивация)'
    SUBSCRIBED_DAYS  = 'subscribed_days', 'Подписался N дней назад (welcome)'


class AudienceType(models.TextChoices):
    ALL      = 'all',      'Все оцифрованные'
    SPECIFIC = 'specific', 'Конкретные пользователи'


class GenderFilter(models.TextChoices):
    ALL    = 'all', 'Все'
    MALE   = 'm',   'Мужчины'
    FEMALE = 'f',   'Женщины'


class SendStatus(models.TextChoices):
    PENDING   = 'pending',   'Ожидает'
    RUNNING   = 'running',   'Отправляется'
    DONE      = 'done',      'Завершена'
    FAILED    = 'failed',    'Ошибка'
    CANCELLED = 'cancelled', 'Отменена'


class TriggerType(models.TextChoices):
    MANUAL = 'manual', 'Вручную'
    AUTO   = 'auto',   'Автоматически'


class RecipientStatus(models.TextChoices):
    PENDING = 'pending', 'Ожидает'
    SENT    = 'sent',    'Отправлено'
    FAILED  = 'failed',  'Ошибка'
    SKIPPED = 'skipped', 'Пропущен'   # нет vk_id или заблокировал сообщения


# ── SenlerConfig ──────────────────────────────────────────────────────────────

class SenlerConfig(TimeStampedModel):
    """
    VK-конфигурация для рассылок от имени сообщества.

    Один объект на Branch.  Токен сообщества нужен для messages.send.
    vk_group_id — числовой ID группы (положительное число).
    """

    branch = models.OneToOneField(
        'branch.Branch',
        on_delete=models.CASCADE,
        related_name='senler_config',
        verbose_name='Торговая точка',
    )
    vk_group_id = models.PositiveBigIntegerField(
        verbose_name='ID группы VK',
        help_text='Числовой ID VK-сообщества (без минуса).',
    )
    vk_community_token = models.CharField(
        max_length=512,
        verbose_name='Токен сообщества VK',
        help_text='Community access token — НЕ пользовательский токен.',
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name='Активна',
        help_text='Снимите флаг, чтобы отключить рассылки без удаления настроек.',
    )

    # ── VK Callback API ───────────────────────────────────────────────────────
    vk_callback_confirmation = models.CharField(
        'Строка подтверждения Callback',
        max_length=64,
        blank=True,
        help_text='VK → Управление → Работа с API → Callback API → Строка для ответа на подтверждение.',
    )
    vk_callback_secret = models.CharField(
        'Секрет Callback',
        max_length=64,
        blank=True,
        help_text='Произвольная строка — вводится в VK и проверяется при входящих событиях.',
    )

    # ── Long Poll catchup ─────────────────────────────────────────────────────
    longpoll_ts = models.CharField(
        max_length=32,
        blank=True,
        default='',
        verbose_name='Long Poll ts',
        help_text='Последний обработанный ts VK Group Long Poll. Используется для catchup после простоя.',
    )

    notes = models.TextField(
        blank=True,
        verbose_name='Заметки',
        help_text='Внутренние комментарии для администратора.',
    )

    def __str__(self):
        return f'VK-конфиг: {self.branch}'

    class Meta:
        verbose_name = 'Настройки рассылки VK'
        verbose_name_plural = 'Настройки рассылки VK'


# ── Broadcast ─────────────────────────────────────────────────────────────────

class Broadcast(TimeStampedModel):
    """
    Многоразовый шаблон рассылки.

    Создаётся один раз — можно запускать сколько угодно.
    Каждый запуск создаёт BroadcastSend с историей отправок.

    Логика фильтрации аудитории:
      audience_type=ALL:
        → все оцифрованные гости (is_employee=False, client.is_active=True, vk_id IS NOT NULL)
        → + gender_filter (AND, если не ALL)
        → + rf_segments: любой из выбранных сегментов (AND к остальным, OR внутри сегментов)
      audience_type=SPECIFIC:
        → только specific_clients; gender_filter и rf_segments ИГНОРИРУЮТСЯ
    """

    branch = models.ForeignKey(
        'branch.Branch',
        on_delete=models.CASCADE,
        related_name='broadcasts',
        verbose_name='Торговая точка',
    )
    name = models.CharField(
        max_length=255,
        verbose_name='Название',
        help_text='Только для администратора — гости не видят это поле.',
    )
    message_text = models.TextField(
        verbose_name='Текст сообщения',
        help_text='Лимит VK: 4096 символов.',
    )
    image = models.ImageField(
        upload_to='broadcasts/',
        blank=True,
        null=True,
        verbose_name='Изображение',
        help_text='Прикрепляется к сообщению как фото.',
    )

    # ── Audience ──────────────────────────────────────────────────────────────

    audience_type = models.CharField(
        max_length=8,
        choices=AudienceType.choices,
        default=AudienceType.ALL,
        verbose_name='Тип аудитории',
    )

    # Refinement filters — apply only when audience_type == ALL
    gender_filter = models.CharField(
        max_length=3,
        choices=GenderFilter.choices,
        default=GenderFilter.ALL,
        verbose_name='Пол',
    )
    rf_segments = models.ManyToManyField(
        'analytics.RFSegment',
        blank=True,
        related_name='broadcasts',
        verbose_name='RF-сегменты',
        help_text='Оставьте пустым — получат все. Выберите несколько — OR между ними.',
    )

    # Exact list — used only when audience_type == SPECIFIC
    specific_clients = models.ManyToManyField(
        'branch.ClientBranch',
        blank=True,
        related_name='targeted_broadcasts',
        verbose_name='Конкретные гости',
        help_text='Фильтры пола и RF-сегментов игнорируются.',
    )

    def __str__(self):
        return f'[{self.branch}] {self.name}'

    class Meta:
        verbose_name = 'Рассылка'
        verbose_name_plural = 'Рассылки'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['branch', '-created_at'], name='broadcast_branch_date_idx'),
        ]


# ── BroadcastSend ─────────────────────────────────────────────────────────────

class BroadcastSend(TimeStampedModel):
    """
    Один запуск рассылки.

    Создаётся при каждом нажатии «Отправить» (вручную или автоматически).
    Аудитория разрешается в момент запуска — при повторе состав может измениться
    (например, гости переместились в другой RF-сегмент).

    trigger_type=MANUAL — запущено администратором.
    trigger_type=AUTO   — запущено Celery-задачей / хуком.
    """

    broadcast = models.ForeignKey(
        Broadcast,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='sends',
        verbose_name='Рассылка',
    )
    auto_broadcast_template = models.ForeignKey(
        'senler.AutoBroadcastTemplate',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sends',
        verbose_name='Шаблон авторассылки',
    )
    status = models.CharField(
        max_length=9,
        choices=SendStatus.choices,
        default=SendStatus.PENDING,
        verbose_name='Статус',
        db_index=True,
    )
    trigger_type = models.CharField(
        max_length=6,
        choices=TriggerType.choices,
        default=TriggerType.MANUAL,
        verbose_name='Тип запуска',
    )
    triggered_by = models.CharField(
        max_length=255,
        blank=True,
        verbose_name='Запустил',
        help_text='Логин администратора или имя системной задачи.',
    )

    # ── Timing ────────────────────────────────────────────────────────────────

    started_at  = models.DateTimeField(null=True, blank=True, verbose_name='Начало')
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name='Окончание')

    # ── Stats (updated as messages are sent) ──────────────────────────────────

    recipients_count = models.PositiveIntegerField(default=0, verbose_name='Получателей')
    sent_count       = models.PositiveIntegerField(default=0, verbose_name='Отправлено')
    failed_count     = models.PositiveIntegerField(default=0, verbose_name='Ошибок')
    skipped_count    = models.PositiveIntegerField(default=0, verbose_name='Пропущено')

    error_message = models.TextField(blank=True, verbose_name='Ошибка')

    def clean(self):
        if not self.broadcast_id and not self.auto_broadcast_template_id:
            raise ValidationError('Необходимо указать рассылку или шаблон авторассылки.')

    def __str__(self):
        name = (
            self.broadcast.name if self.broadcast_id
            else str(self.auto_broadcast_template) if self.auto_broadcast_template_id
            else '—'
        )
        return f'{name} — {self.get_status_display()} ({self.created_at:%d.%m.%Y %H:%M})'

    class Meta:
        verbose_name = 'Запуск рассылки'
        verbose_name_plural = 'История рассылок'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['broadcast', '-created_at'], name='bsend_broadcast_date_idx'),
            models.Index(fields=['status'],                   name='bsend_status_idx'),
        ]


# ── BroadcastRecipient ────────────────────────────────────────────────────────

class BroadcastRecipient(models.Model):
    """
    Запись о попытке доставки одного сообщения одному гостю.

    vk_id денормализован: при удалении ClientBranch история сохраняется.
    status=SKIPPED — у гостя нет vk_id или он заблокировал сообщения.
    """

    send = models.ForeignKey(
        BroadcastSend,
        on_delete=models.CASCADE,
        related_name='recipients',
        verbose_name='Запуск',
    )
    client_branch = models.ForeignKey(
        'branch.ClientBranch',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='received_broadcasts',
        verbose_name='Гость',
    )
    vk_id   = models.PositiveIntegerField(verbose_name='VK ID')
    status  = models.CharField(
        max_length=7,
        choices=RecipientStatus.choices,
        default=RecipientStatus.PENDING,
        verbose_name='Статус',
    )
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name='Отправлено')
    error   = models.CharField(max_length=512, blank=True, verbose_name='Ошибка')

    # ── Read tracking ────────────────────────────────────────────────────────
    vk_message_id = models.PositiveBigIntegerField(
        null=True, blank=True,
        verbose_name='VK message ID',
        help_text='ID сообщения из ответа messages.send — для отслеживания прочтений.',
    )
    read_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='Прочитано',
        help_text='Когда сообщение было прочитано (из VK API).',
    )

    def __str__(self):
        return f'vk{self.vk_id} — {self.get_status_display()}'

    class Meta:
        verbose_name = 'Получатель'
        verbose_name_plural = 'Получатели'
        indexes = [
            models.Index(fields=['send', 'status'], name='brecip_send_status_idx'),
            models.Index(fields=['vk_id'],           name='brecip_vkid_idx'),
            models.Index(fields=['client_branch'],   name='brecip_cb_idx'),
        ]


# ── AutoBroadcastTemplate ─────────────────────────────────────────────────────

class AutoBroadcastTemplate(TimeStampedModel):
    """
    Текст для автоматической рассылки по триггеру.

    Один глобальный шаблон на тип триггера — используется для всех Branch.
    Если шаблон отсутствует или is_active=False — рассылка для этого
    триггера не отправляется.

    Типы триггеров:
      birthday_7d   — за 7 дней до ДР
      birthday_1d   — за 1 день до ДР
      birthday      — в день рождения
      after_game_3h — через 3 часа после игры
    """

    type = models.CharField(
        max_length=32,
        choices=AutoBroadcastType.choices,
        unique=True,
        verbose_name='Триггер',
    )
    message_text = models.TextField(
        verbose_name='Текст сообщения',
        help_text=(
            'Лимит VK: 4096 символов. Для триггера «Подарок из сториз/сайта не забран» '
            'можно вставлять переменные: {дней_осталось} — сколько дней осталось забрать '
            'подарок; {подарок} — название подарка; {адреса} — список адресов всех точек '
            '(подарок сетевой — забрать можно в любой). Пример: «Ваш подарок {подарок} всё '
            'ещё ждёт вас! Осталось {дней_осталось} дн. Приходите в любое наше кафе: {адреса}».'
        ),
    )
    image = models.ImageField(
        upload_to='auto_broadcasts/',
        blank=True,
        null=True,
        verbose_name='Изображение',
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name='Активен',
        help_text='Снимите флаг, чтобы временно отключить этот триггер.',
    )

    def __str__(self):
        return self.get_type_display()

    class Meta:
        verbose_name = 'Шаблон авторассылки'
        verbose_name_plural = 'Шаблоны авторассылок'
        ordering = ['type']


# ── AutoBroadcastLog ──────────────────────────────────────────────────────────

class AutoBroadcastLog(models.Model):
    """
    Tracks sent auto-broadcasts to prevent duplicate sends.

    Birthday triggers: check .filter(trigger_type=type, vk_id=vk_id, sent_at__year=year)
    After-game trigger: check .filter(trigger_type=type, vk_id=vk_id, sent_at__date=today)

    ⚠️ КЛЮЧ ДЕДУПА — (trigger_type, vk_id, период). Он НЕ меняется и НЕ включает
    правило/шаблон: на проде накоплено ~13k записей, и именно они не дают повторно
    написать людям, которых уже поздравили. Новый движок правил (engine.py) пишет и
    читает ЭТОТ ЖЕ лог с теми же trigger_type → старые записи продолжают работать,
    и даже одновременный запуск старой задачи и нового движка не даст дубля.
    Следствие (осознанное): если на одно событие заведено несколько правил, гость
    за период получит ОДНО сообщение (побеждает правило с большим приоритетом) —
    правила это варианты текста, а не повод писать человеку дважды.
    """

    trigger_type = models.CharField(
        max_length=32,
        choices=AutoBroadcastType.choices,
        verbose_name='Триггер',
        db_index=True,
    )
    vk_id = models.PositiveIntegerField(verbose_name='VK ID', db_index=True)
    sent_at = models.DateTimeField(auto_now_add=True, verbose_name='Отправлено')

    # Дедуп по КОНКРЕТНОМУ объекту, а не по периоду: для «подарок не забран» это
    # 'gift:<id>' — гость может получить несколько подарков, и по каждому положено
    # своё напоминание. Пусто — дедуп по периоду (ДР/после игры), как раньше.
    entity_key = models.CharField(
        max_length=64, blank=True, default='', db_index=True,
        verbose_name='Ключ объекта',
        help_text='Например gift:123 — дедуп по конкретному подарку, а не по периоду.',
    )
    # Каким правилом отправлено — только для статистики/трассировки, В ДЕДУПЕ НЕ УЧАСТВУЕТ.
    rule = models.ForeignKey(
        'senler.AutoBroadcastRule',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='logs',
        verbose_name='Правило',
    )

    def __str__(self):
        return f'{self.get_trigger_type_display()} → vk{self.vk_id} @ {self.sent_at:%d.%m.%Y %H:%M}'

    class Meta:
        verbose_name = 'Лог авторассылки'
        verbose_name_plural = 'Лог авторассылок'
        indexes = [
            models.Index(fields=['trigger_type', 'vk_id', 'sent_at'], name='autobc_log_idx'),
            models.Index(fields=['trigger_type', 'entity_key'], name='autobc_log_entity_idx'),
        ]


# ── AutoBroadcastRule (конструктор авторассылок) ──────────────────────────────

class AutoBroadcastRule(TimeStampedModel):
    """
    Правило авторассылки — «конструктор»: событие + когда + кому + что написать.

    Отличие от AutoBroadcastTemplate (legacy): шаблонов может быть НЕСКОЛЬКО на одно
    событие, у каждого свои задержка/окно/период/аудитория. Legacy-шаблоны остаются
    и переносятся сюда данными; старые задачи не трогаются и служат путём отката.

    Пустая настройка = «как было»: пустые branches/rf_segments = все, delay_days=None
    = задержка по умолчанию для события, active_from/to пусто = бессрочно.
    """

    name = models.CharField(
        max_length=120,
        verbose_name='Название',
        help_text='Только для админки — гости его не видят. Напр. «Напоминание о подарке».',
    )
    event = models.CharField(
        max_length=32,
        choices=AutoBroadcastType.choices,
        db_index=True,
        verbose_name='Событие (триггер)',
        help_text='Что должно произойти с гостем, чтобы ему ушло это сообщение.',
    )

    # ── Когда слать ───────────────────────────────────────────────────────────
    delay_days = models.PositiveSmallIntegerField(
        null=True, blank=True,
        verbose_name='Задержка после события, дней',
        help_text='Пусто — задержка по умолчанию для этого события (напр. 10 дней для '
                  'незабранного подарка берётся из настроек сети).',
    )
    send_hour_start = models.PositiveSmallIntegerField(
        default=9,
        verbose_name='Не слать раньше (час)',
        help_text='Тихие часы. По умолчанию 9 — до 9 утра сообщения не уходят.',
    )
    send_hour_end = models.PositiveSmallIntegerField(
        default=21,
        verbose_name='Не слать позже (час)',
        help_text='По умолчанию 21 — после 21:00 сообщения не уходят.',
    )
    active_from = models.DateField(
        null=True, blank=True,
        verbose_name='Действует с',
        help_text='Пусто — без ограничения.',
    )
    active_to = models.DateField(
        null=True, blank=True,
        verbose_name='Действует по',
        help_text='Пусто — без ограничения.',
    )

    # ── Кому слать (фильтры те же, что у обычных рассылок) ────────────────────
    branches = models.ManyToManyField(
        'branch.Branch',
        blank=True,
        related_name='auto_broadcast_rules',
        verbose_name='Точки',
        help_text='Пусто — все точки сети.',
    )
    gender_filter = models.CharField(
        max_length=3,
        choices=GenderFilter.choices,
        default=GenderFilter.ALL,
        verbose_name='Пол',
    )
    rf_segments = models.ManyToManyField(
        'analytics.RFSegment',
        blank=True,
        related_name='auto_broadcast_rules',
        verbose_name='RF-сегменты',
        help_text='Пусто — без ограничения по сегменту.',
    )

    # ── Что написать ──────────────────────────────────────────────────────────
    message_text = models.TextField(
        verbose_name='Текст сообщения',
        help_text=(
            'Лимит VK: 4096 символов. Переменные: {имя} — имя гостя; {подарок} — название '
            'подарка; {дней_осталось} — сколько дней осталось забрать; {адреса} — адреса '
            'всех точек. Доступность переменной зависит от события.'
        ),
    )
    image = models.ImageField(
        upload_to='auto_broadcasts/',
        blank=True, null=True,
        verbose_name='Изображение',
    )

    is_active = models.BooleanField(
        default=False,
        verbose_name='Активно',
        help_text='Пока выключено — сообщения не уходят. Перед включением посмотрите '
                  'предпросмотр «кому уйдёт».',
    )
    priority = models.PositiveSmallIntegerField(
        default=0,
        verbose_name='Приоритет',
        help_text='Если на одно событие несколько правил — гость получит сообщение по '
                  'правилу с БОЛЬШИМ приоритетом (одно сообщение, не несколько).',
    )

    def __str__(self):
        return f'{self.name} ({self.get_event_display()})'

    class Meta:
        verbose_name = 'Правило авторассылки'
        verbose_name_plural = 'Авторассылки (конструктор)'
        ordering = ['event', '-priority', 'name']
        indexes = [
            models.Index(fields=['event', 'is_active'], name='autobc_rule_event_idx'),
        ]
