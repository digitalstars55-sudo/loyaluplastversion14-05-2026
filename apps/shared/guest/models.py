from django.db import models

from apps.shared.base import TimeStampedModel


class Gender(models.TextChoices):
    MALE   = 'm', 'Мужской'
    FEMALE = 'f', 'Женский'


class Client(TimeStampedModel):
    """
    Гость ресторана — VK-пользователь, идентифицированный через мини-приложение.
    Хранится в public-схеме и доступен из любого тенанта.
    """

    vk_id = models.PositiveBigIntegerField(
        unique=True,
        verbose_name='VK ID',
        help_text='Уникальный числовой ID пользователя ВКонтакте',
    )
    first_name = models.CharField(max_length=255, blank=True, verbose_name='Имя', help_text='Берётся из VK API.')
    last_name = models.CharField(max_length=255, blank=True, verbose_name='Фамилия', help_text='Берётся из VK API.')
    photo_url = models.URLField(max_length=500, blank=True, verbose_name='Фото', help_text='Ссылка на аватар из VK.')
    gender = models.CharField(
        max_length=1,
        choices=Gender.choices,
        blank=True,
        null=True,
        default=None,
        verbose_name='Пол',
        help_text='Берётся из VK API при регистрации (1 = женский, 2 = мужской).',
    )

    # №78 карты переезда: телефон с согласия гостя. Заполняется ручкой
    # POST /api/v1/client/phone/ по ответу bridge `VKWebAppGetPhoneNumber`
    # (apps/tenant/branch/api/client_phone.py). Пусто = гость номер не давал;
    # старый код этих полей не читает, так что с выключенным флагом
    # GUEST_PHONE_ENABLED всё ведёт себя как раньше.
    # ⚠️ db_default обязателен: guest_client пишут и web, и celery; после
    # миграции до перезапуска старый код вставляет строки без новых колонок —
    # без дефолта на уровне БД это IntegrityError (17.09: поллинг ВК 25 минут
    # не мог завести новых гостей из-за phone_placement).
    phone = models.CharField(
        'Телефон',
        max_length=20,
        blank=True,
        default='',
        db_default='',
        help_text='E.164 (+7…), с согласия гостя через ВКонтакте.',
    )
    phone_source = models.CharField(
        'Источник телефона',
        max_length=16,
        blank=True,
        default='',
        db_default='',
        help_text="'vk' — подпись ВК сошлась; 'vk_unverified' — сохранён в режиме наблюдения без проверки подписи.",
    )
    phone_consent_at = models.DateTimeField(
        'Согласие на телефон',
        null=True,
        blank=True,
        help_text='Когда гость поделился номером (окно согласия ВК).',
    )
    phone_placement = models.CharField(
        'Где дал номер',
        max_length=32,
        blank=True,
        default='',
        db_default='',
        help_text="Место в мини-аппе: 'profile', 'review', … — чтобы сравнивать, где гости соглашаются.",
    )
    phone_reward_at = models.DateTimeField(
        'Баллы за номер начислены',
        null=True,
        blank=True,
        help_text='Награда за первый номер выдаётся один раз на гостя; при отзыве согласия не отбирается.',
    )

    is_active = models.BooleanField(
        default=True,
        verbose_name='Активен',
        help_text='Снимите флаг, чтобы заблокировать гостя на всей платформе',
    )

    def __str__(self):
        name = f'{self.first_name} {self.last_name}'.strip()
        return name if name else f'vk{self.vk_id}'

    class Meta:
        verbose_name = 'Гость'
        verbose_name_plural = 'Гости'
        ordering = ['-created_at']


class TelegramLink(TimeStampedModel):
    """
    Телеграм гостя ↔ его страница ВК (15.10.2026). Заводится, когда гость
    входит через VK ID внутри Телеграм-мини-аппа: в одном запросе есть и
    доказанный VK ID (обмен кода), и подписанная ботом initData. Дальше из
    Телеграма гость входит без VK ID — `POST /api/v1/vk/tg-session/`.
    """

    tg_user_id = models.PositiveBigIntegerField(unique=True, verbose_name='Telegram ID')
    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name='telegram_links', verbose_name='Гость',
    )

    class Meta:
        verbose_name = 'Привязка Телеграма'
        verbose_name_plural = 'Привязки Телеграма'

    def __str__(self):
        return f'tg {self.tg_user_id} → vk {self.client.vk_id}'


class GuestForgetLog(models.Model):
    """
    Журнал «удалить мои данные» (15.10.2026). Сам VK ID не хранится — только
    его хэш (чтобы на повторный вопрос «удалили ли меня» можно было ответить),
    и сколько строк обезличено в каждой сети.
    """

    vk_id_hash = models.CharField(max_length=64, db_index=True, verbose_name='Хэш VK ID')
    client_pk = models.PositiveBigIntegerField(verbose_name='Номер карточки гостя')
    requested_at = models.DateTimeField(auto_now_add=True, verbose_name='Запрошено')
    via = models.CharField(max_length=16, blank=True, default='', verbose_name='Как доказан гость')
    counts = models.JSONField(default=dict, blank=True, verbose_name='Обезличено строк')

    class Meta:
        verbose_name = 'Удаление данных гостя'
        verbose_name_plural = 'Удаления данных гостей'
        ordering = ['-requested_at']

    def __str__(self):
        return f'гость #{self.client_pk} — {self.requested_at:%d.%m.%Y %H:%M}'
