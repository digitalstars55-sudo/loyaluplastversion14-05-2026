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
    vk_wall_token = models.CharField(
        'Токен для стены',
        max_length=512,
        blank=True,
        help_text=(
            'ПОЛЬЗОВАТЕЛЬСКИЙ токен админа сообщества с правами wall,photos,offline. '
            'НЕ senler-токен и НЕ community-токен (тот стену постить не умеет). '
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

    class Meta:
        verbose_name = 'Пост маркетолога'
        verbose_name_plural = 'Посты маркетолога'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_post_type_display()} · {self.get_status_display()} · {self.created_at:%d.%m.%Y}'
