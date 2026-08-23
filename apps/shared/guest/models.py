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
