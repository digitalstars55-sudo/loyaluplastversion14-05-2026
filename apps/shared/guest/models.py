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
    phone = models.CharField(
        'Телефон',
        max_length=20,
        blank=True,
        default='',
        help_text='E.164 (+7…), с согласия гостя через ВКонтакте.',
    )
    phone_source = models.CharField(
        'Источник телефона',
        max_length=16,
        blank=True,
        default='',
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
        help_text="Место в мини-аппе: 'profile', 'review', … — чтобы сравнивать, где гости соглашаются.",
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
