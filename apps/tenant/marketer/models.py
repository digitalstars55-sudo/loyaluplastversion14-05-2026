from apps.shared.secret_fields import EncryptedSecretField
from django.db import models

from apps.shared.base import TimeStampedModel


class MarketerSettings(TimeStampedModel):
    """
    Настройки AI-маркетолога тенанта (одна запись на схему).

    ⚠️ Токен для стены — ОТДЕЛЬНЫЙ, не из SenlerConfig: постинг со
    senler-токена рискует спам-флагом, который уронит отзывы и рассылки.
    wall.post требует ПОЛЬЗОВАТЕЛЬСКИЙ токен админа сообщества
    (community-токен стену постить не умеет).
    """

    WEEKDAYS = (
        (0, 'Понедельник'), (1, 'Вторник'), (2, 'Среда'), (3, 'Четверг'),
        (4, 'Пятница'), (5, 'Суббота'), (6, 'Воскресенье'),
    )

    is_enabled = models.BooleanField(
        'Включён',
        default=False,
        help_text='Мастер-флаг: выключено — ни одна задача маркетолога не работает.',
    )
    vk_group_id = models.PositiveBigIntegerField(
        'ID группы VK',
        null=True,
        blank=True,
        help_text='Числовой ID сообщества (без минуса). Пусто — берётся из SenlerConfig первой точки.',
    )
    vk_wall_token = EncryptedSecretField(
        'Токен для стены',
        max_length=512,
        blank=True,
        help_text=(
            'Токен с правом «Стена»: токен сообщества (Управление → Работа с API → '
            'Ключи доступа, галочка «Стена») или пользовательский токен админа. '
            'НЕ senler-токен (у того «Сообщения» — каналы изолированы). '
            'Пусто — посты остаются черновиками, публикация недоступна.'
        ),
    )
    autopost_enabled = models.BooleanField(
        'Автопубликация',
        default=False,
        help_text=(
            'Выключено (по умолчанию) — маркетолог только готовит черновики, '
            'публикация вручную из админки. Включено — сгенерированный дайджест '
            'публикуется на стену сразу.'
        ),
    )

    # ── Еженедельный дайджест «Что нового?» ───────────────────────────────────
    digest_enabled = models.BooleanField(
        'Дайджест «Что нового?»',
        default=True,
        help_text='Еженедельный пост-дайджест из данных лояльности за неделю.',
    )
    digest_weekday = models.PositiveSmallIntegerField(
        'День дайджеста',
        default=0,
        choices=WEEKDAYS,
    )
    digest_hour = models.PositiveSmallIntegerField(
        'Час дайджеста (МСК)',
        default=12,
        help_text='0–23. Черновик готовится в этот час (тик раз в час).',
    )
    last_digest_at = models.DateTimeField(
        'Последний дайджест',
        null=True,
        blank=True,
        editable=False,
    )

    # ── Знания от владельца ──────────────────────────────────────────────────
    brand_voice = models.TextField(
        'Тон бренда',
        blank=True,
        help_text=(
            'Как писать: голос бренда, обращение к гостям, что нельзя упоминать. '
            'Передаётся ИИ при каждой генерации.'
        ),
    )
    extra_facts = models.TextField(
        'Факты от владельца',
        blank=True,
        help_text=(
            'Актуальные акции, новинки меню, события — всё, чего нет в данных '
            'системы. ИИ использует ТОЛЬКО факты отсюда и из данных лояльности.'
        ),
    )

    # ── Картинки в фирменном стиле (16.10.2026) ───────────────────────────────
    image_style = models.TextField(
        'Фирменный стиль картинок',
        blank=True,
        help_text='Цвета, свет, посуда, настроение, чего не рисовать. Уходит в каждую картинку ИИ.',
    )
    image_source = models.CharField(
        'Откуда брать картинку',
        max_length=12,
        default='ai',
        choices=[('ai', 'Рисует ИИ'), ('bank_first', 'Сначала фотобанк, иначе ИИ')],
    )

    class Meta:
        verbose_name = 'Настройки AI-маркетолога'
        verbose_name_plural = 'Настройки AI-маркетолога'

    def __str__(self):
        state = 'вкл' if self.is_enabled else 'выкл'
        return f'AI-маркетолог ({state})'


class MarketerPostType(models.TextChoices):
    DIGEST  = 'digest',  'Дайджест «Что нового?»'
    INSIGHT = 'insight', 'Инсайт лояльности'
    PROMO   = 'promo',   'Промо'
    CUSTOM  = 'custom',  'Произвольный'


class MarketerPostStatus(models.TextChoices):
    DRAFT     = 'draft',     'Черновик'
    SCHEDULED = 'scheduled', 'Запланирован'
    PUBLISHED = 'published', 'Опубликован'
    REJECTED  = 'rejected',  'Отклонён'
    FAILED    = 'failed',    'Ошибка публикации'


class MarketerPost(TimeStampedModel):
    """
    Пост, подготовленный AI-маркетологом.

    Жизненный цикл: draft → published (кнопкой в админке или автопостом)
    либо draft → rejected. failed — публикация упала (текст сохранён,
    можно повторить).
    """

    post_type = models.CharField(
        'Тип',
        max_length=16,
        choices=MarketerPostType.choices,
        default=MarketerPostType.DIGEST,
        db_index=True,
    )
    status = models.CharField(
        'Статус',
        max_length=16,
        choices=MarketerPostStatus.choices,
        default=MarketerPostStatus.DRAFT,
        db_index=True,
    )
    text = models.TextField('Текст поста')
    context_snapshot = models.JSONField(
        'Снимок данных',
        default=dict,
        blank=True,
        help_text='Knowledge Core на момент генерации — из каких фактов написан пост.',
    )
    model_used = models.CharField('Модель ИИ', max_length=64, blank=True)
    created_by = models.CharField(
        'Автор',
        max_length=150,
        blank=True,
        help_text='ai — сгенерирован задачей; иначе username администратора.',
    )

    published_at = models.DateTimeField('Опубликован', null=True, blank=True)
    vk_post_id = models.CharField(
        'ID поста VK',
        max_length=32,
        blank=True,
        help_text='post_id из ответа wall.post.',
    )
    error = models.TextField('Ошибка', blank=True)

    # --- Волна 3: контент-план и отложенные посты (14.10.2026) ---
    # Запланировать = одобрение человеком: в назначенное время пост уходит на
    # стену задачей publish_due_marketer_posts_task даже при autopost_enabled=False.
    scheduled_at = models.DateTimeField('Опубликовать в', null=True, blank=True, db_index=True)
    topic = models.CharField(
        'Тема из контент-плана', max_length=200, blank=True,
        help_text='Пусто — пост не из плана.',
    )
    image = models.ImageField(
        'Картинка', upload_to='marketer_posts/', null=True, blank=True,
        help_text='Прикрепляется к посту на стене (photos.saveWallPhoto).',
    )
    # 16.10.2026: визуальный приём, который ИИ выбрал по базе знаний вместе с
    # текстом, — из него рисуется картинка; и откуда картинка взялась.
    visual = models.TextField('Что на картинке', blank=True)
    image_source = models.CharField(
        'Картинка из', max_length=8, blank=True,
        choices=[('ai', 'ИИ'), ('bank', 'фотобанк'), ('upload', 'загружена')],
    )

    class Meta:
        verbose_name = 'Пост маркетолога'
        verbose_name_plural = 'Посты маркетолога'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_post_type_display()} · {self.get_status_display()} · {self.created_at:%d.%m.%Y}'


class MarketerPhoto(TimeStampedModel):
    """
    Фотобанк сети (16.10.2026).

    photo — настоящее фото блюда или зала: маркетолог ставит его к посту, если
    в тексте есть блюдо из названия/меток. style — образец стиля: ИИ рисует,
    глядя на него (свет, посуда, столы), но не копирует.
    """

    class Kind(models.TextChoices):
        PHOTO = 'photo', 'Фото для постов'
        STYLE = 'style', 'Образец стиля'

    image = models.ImageField('Фото', upload_to='marketer_bank/')
    title = models.CharField('Что на фото', max_length=200, blank=True)
    tags = models.CharField('Метки', max_length=500, blank=True,
                            help_text='Через запятую: хачапури, завтрак, Ленина.')
    kind = models.CharField('Вид', max_length=8, choices=Kind.choices, default=Kind.PHOTO)
    is_active = models.BooleanField('Используется', default=True)
    created_by = models.CharField('Кто добавил', max_length=150, blank=True)
    used_count = models.PositiveIntegerField('Сколько раз ставили', default=0)

    class Meta:
        verbose_name = 'Фото фотобанка'
        verbose_name_plural = 'Фотобанк маркетолога'
        ordering = ['-created_at']

    def __str__(self):
        return self.title or f'Фото №{self.pk}'


class MarketerJob(TimeStampedModel):
    """
    Ход одной генерации (16.10.2026): кабинет показывает, на каком она шаге.

    Живёт в базе, а не в кэше: кэш LoyalUP по умолчанию в памяти процесса, и
    задача в celery его не видит. Заодно это честная блокировка от двойного
    нажатия — раньше замок жил в памяти одного воркера web.
    """

    class Kind(models.TextChoices):
        POST = 'post', 'Пост'
        PLAN = 'plan', 'Контент-план'
        IMAGE = 'image', 'Картинка'

    class Status(models.TextChoices):
        RUNNING = 'running', 'Идёт'
        DONE = 'done', 'Готово'
        FAILED = 'failed', 'Ошибка'

    kind = models.CharField(max_length=8, choices=Kind.choices)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.RUNNING,
                              db_index=True)
    stage = models.CharField(max_length=16, blank=True)
    stage_label = models.CharField(max_length=200, blank=True)
    step = models.PositiveSmallIntegerField(default=0)
    total = models.PositiveSmallIntegerField(default=0)
    post = models.ForeignKey(MarketerPost, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name='jobs')
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    created_by = models.CharField(max_length=150, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Генерация маркетолога'
        verbose_name_plural = 'Генерации маркетолога'
        ordering = ['-created_at']


# ═════════════════════════════════════════════════════════════════════════════
# Студия видео (19.10.2026, макет https://claude.ai/artifact/1yDCLmgeuJRDE7nAgZ5DUU)
#
# Задание и правки — словами, ручного таймлайна нет. ИИ пишет сценарий из
# сцен, сборщик (отдельный процесс с HyperFrames + FFmpeg, `tools/studio-worker`)
# собирает MP4 сразу в четырёх форматах. Каждая правка — новая версия, прежние
# остаются и возвращаются одной кнопкой. Стиль бренда версионный: ролик помнит,
# по какой версии стиля собран, а «запомнить для бренда» поднимает версию.
# ═════════════════════════════════════════════════════════════════════════════

class StudioStyle(TimeStampedModel):
    """Стиль бренда для роликов — одна запись на сеть (схему)."""

    version = models.PositiveIntegerField('Версия стиля', default=1)
    palette = models.JSONField('Цвета', default=list, blank=True,
                               help_text='["#1B2838", "#C9A227", "#F6F3EA"] — фон, акцент, светлый.')
    heading_font = models.CharField('Шрифт заголовков', max_length=60, blank=True, default='Manrope')
    body_font = models.CharField('Шрифт текста', max_length=60, blank=True, default='Manrope')
    logo = models.ImageField('Логотип (светлый фон)', upload_to='studio_brand/', null=True, blank=True)
    logo_dark = models.ImageField('Логотип (тёмный фон)', upload_to='studio_brand/', null=True, blank=True)
    music = models.FileField('Музыка по умолчанию', upload_to='studio_brand/', null=True, blank=True)
    cut_pace = models.CharField('Ритм склеек', max_length=60, blank=True, default='0,6–1,2 с')
    captions = models.CharField('Титры', max_length=200, blank=True,
                                default='снизу, на плашке, не закрывают лицо и блюдо')
    final_card = models.CharField('Финал', max_length=200, blank=True,
                                  default='логотип + адрес + одно предложение')
    forbidden = models.TextField('Нельзя', blank=True,
                                 default='Цены, которых нет в меню\nЛица гостей без согласия\nМузыка без лицензии')
    rules = models.JSONField('Запомнено для бренда', default=list, blank=True,
                             help_text='[{"text", "version", "by", "at"}] — правки, ставшие правилом.')
    address = models.CharField('Адрес для финала', max_length=200, blank=True)

    class Meta:
        verbose_name = 'Стиль роликов'
        verbose_name_plural = 'Стиль роликов'

    def __str__(self):
        return f'Стиль роликов, версия {self.version}'


class StudioStyleVersion(TimeStampedModel):
    """Снимок стиля на каждую версию — откат и «по какой версии собран ролик»."""

    version = models.PositiveIntegerField('Версия', unique=True)
    snapshot = models.JSONField('Снимок', default=dict)
    note = models.CharField('Что поменяли', max_length=300, blank=True)
    created_by = models.CharField('Кто', max_length=150, blank=True)

    class Meta:
        verbose_name = 'Версия стиля роликов'
        verbose_name_plural = 'Версии стиля роликов'
        ordering = ['-version']


class StudioProject(TimeStampedModel):
    """Ролик: задание, вопросы ИИ, версии и публикация."""

    class Kind(models.TextChoices):
        REEL = 'reel', 'Ролик 15–30 с'
        STORIES = 'stories', 'Сторис'
        BANNER = 'banner', 'Баннер'

    class Status(models.TextChoices):
        ASKING = 'asking', 'Ждёт ответа'
        QUEUED = 'queued', 'В очереди'
        RENDERING = 'rendering', 'Собирается'
        READY = 'ready', 'Готово'
        FAILED = 'failed', 'Ошибка'

    class Approval(models.TextChoices):
        NONE = 'none', 'Не отправлен'
        WAITING = 'waiting', 'Ждёт согласования'
        APPROVED = 'approved', 'Согласован'
        REJECTED = 'rejected', 'Вернули на правку'

    title = models.CharField('Название', max_length=200)
    kind = models.CharField('Что сделать', max_length=10, choices=Kind.choices, default=Kind.REEL)
    brief = models.TextField('Задание словами')
    duration = models.PositiveSmallIntegerField('Длина, с', default=20)
    status = models.CharField('Статус', max_length=10, choices=Status.choices, default=Status.QUEUED,
                              db_index=True)
    question = models.TextField('Вопрос ИИ', blank=True,
                                help_text='Что ИИ уточняет перед сборкой (цена, предложение).')
    answer = models.TextField('Ответ', blank=True)
    style_version = models.PositiveIntegerField('Версия стиля', default=1)
    current_version = models.ForeignKey('StudioVersion', null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name='+')
    approval = models.CharField('Согласование', max_length=10, choices=Approval.choices,
                                default=Approval.NONE)
    approved_by = models.CharField('Согласовал', max_length=150, blank=True)
    approved_at = models.DateTimeField('Согласован', null=True, blank=True)
    publish_plan = models.JSONField('Публикация', default=list, blank=True,
                                    help_text='[{"channel", "format", "at", "status", "post_id"}].')
    created_by = models.CharField('Автор', max_length=150, blank=True)

    class Meta:
        verbose_name = 'Ролик студии'
        verbose_name_plural = 'Ролики студии'
        ordering = ['-updated_at']

    def __str__(self):
        return self.title


class StudioAsset(TimeStampedModel):
    """Материал: видео, фото, логотип, музыка. Без проекта — библиотека сети."""

    class Kind(models.TextChoices):
        VIDEO = 'video', 'Видео'
        PHOTO = 'photo', 'Фото'
        MUSIC = 'music', 'Музыка'

    project = models.ForeignKey(StudioProject, null=True, blank=True, on_delete=models.CASCADE,
                                related_name='assets')
    kind = models.CharField('Вид', max_length=8, choices=Kind.choices)
    file = models.FileField('Файл', upload_to='studio_assets/')
    title = models.CharField('Что на нём', max_length=200, blank=True)
    duration = models.FloatField('Длительность, с', null=True, blank=True)
    size = models.PositiveBigIntegerField('Размер, байт', default=0)
    created_by = models.CharField('Кто добавил', max_length=150, blank=True)

    class Meta:
        verbose_name = 'Материал студии'
        verbose_name_plural = 'Материалы студии'
        ordering = ['created_at']


class StudioVersion(TimeStampedModel):
    """Версия ролика: сценарий, правка, ход сборки и готовые файлы по форматам."""

    class Status(models.TextChoices):
        QUEUED = 'queued', 'В очереди'
        RENDERING = 'rendering', 'Собирается'
        READY = 'ready', 'Готово'
        FAILED = 'failed', 'Ошибка'

    project = models.ForeignKey(StudioProject, on_delete=models.CASCADE, related_name='versions')
    number = models.PositiveIntegerField('Номер')
    instruction = models.TextField('Правка словами', blank=True)
    scene = models.PositiveSmallIntegerField('Правка только сцены', null=True, blank=True)
    scenario = models.JSONField('Сценарий', default=dict, blank=True)
    changelog = models.JSONField('Что изменилось', default=list, blank=True)
    style_version = models.PositiveIntegerField('Версия стиля', default=1)
    style_snapshot = models.JSONField('Стиль на момент сборки', default=dict, blank=True)
    status = models.CharField('Статус', max_length=10, choices=Status.choices, default=Status.QUEUED,
                              db_index=True)
    stage = models.CharField('Шаг', max_length=200, blank=True)
    progress = models.PositiveSmallIntegerField('Готово, %', default=0)
    error = models.TextField('Ошибка', blank=True)
    worker = models.CharField('Сборщик', max_length=100, blank=True)
    claimed_at = models.DateTimeField('Взят в сборку', null=True, blank=True)
    finished_at = models.DateTimeField('Собран', null=True, blank=True)
    remember_suggestion = models.CharField('Предложить запомнить', max_length=300, blank=True)

    class Meta:
        verbose_name = 'Версия ролика'
        verbose_name_plural = 'Версии роликов'
        ordering = ['project', 'number']
        unique_together = [('project', 'number')]


class StudioOutput(TimeStampedModel):
    """Готовый MP4 одного формата одной версии."""

    version = models.ForeignKey(StudioVersion, on_delete=models.CASCADE, related_name='files')
    fmt = models.CharField('Формат', max_length=8)
    file = models.FileField('MP4', upload_to='studio_out/')
    poster = models.ImageField('Кадр', upload_to='studio_out/', null=True, blank=True)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    duration = models.FloatField(default=0)

    class Meta:
        verbose_name = 'Файл ролика'
        verbose_name_plural = 'Файлы роликов'
        unique_together = [('version', 'fmt')]
