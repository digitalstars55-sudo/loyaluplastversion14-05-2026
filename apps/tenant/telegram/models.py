from apps.shared.secret_fields import EncryptedSecretField
import uuid

from django.db import models

from apps.shared.base import TimeStampedModel


class TelegramBot(TimeStampedModel):
    """
    Telegram-бот, привязанный к торговой точке.
    Используется для уведомлений администраторов точки.
    """

    name = models.CharField(max_length=255, verbose_name='Название', help_text='Внутреннее название для удобства — гости не видят.')
    bot_username = models.CharField(
        max_length=255,
        verbose_name='Username бота',
        help_text='Username без @, например: my_restaurant_bot',
    )
    api = EncryptedSecretField(
        max_length=512,
        verbose_name='API Token',
        help_text='Токен от @BotFather. Храните в секрете.',
    )
    branch = models.ForeignKey(
        'branch.Branch',
        on_delete=models.CASCADE,
        related_name='telegram_bots',
        verbose_name='Торговая точка',
    )

    def __str__(self):
        return f'@{self.bot_username} ({self.name})'

    class Meta:
        verbose_name = 'Telegram-бот'
        verbose_name_plural = 'Telegram-боты'
        ordering = ['name']


class BotAdmin(TimeStampedModel):
    """
    Администратор бота — получатель уведомлений.
    Chat ID заполняется автоматически при верификации через бота.
    """

    bot = models.ForeignKey(
        TelegramBot,
        on_delete=models.CASCADE,
        related_name='admins',
        verbose_name='Бот',
    )
    name = models.CharField(max_length=255, verbose_name='Имя')
    chat_id = models.BigIntegerField(
        null=True,
        blank=True,
        verbose_name='Chat ID',
        help_text='Заполняется автоматически при подключении через бота.',
    )
    verification_token = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        verbose_name='Токен верификации',
        db_index=True,
    )
    is_active = models.BooleanField(default=True, verbose_name='Активен', help_text='Снимите флаг, чтобы отключить уведомления без удаления записи.')

    def __str__(self):
        return f'{self.name} → @{self.bot.bot_username}'

    class Meta:
        verbose_name = 'Администратор бота'
        verbose_name_plural = 'Администраторы бота'
        ordering = ['name']
