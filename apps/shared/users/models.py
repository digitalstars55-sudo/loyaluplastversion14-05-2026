from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """
    Иерархия ролей:
    - SUPERADMIN    → управляет всей платформой (public schema), недоступен для удаления/изменения
    - NETWORK_ADMIN → полный доступ внутри своих тенантов, не пересекается с чужими
    - CLIENT        → только аналитика и ответы на отзывы
    """

    class Role(models.TextChoices):
        SUPERADMIN    = 'superadmin',    'Супер Администратор'
        NETWORK_ADMIN = 'network_admin', 'Администратор сети'
        CLIENT        = 'client',        'Клиент'

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.CLIENT,
        verbose_name='Роль',
        help_text=(
            'Супер Администратор — управляет всей платформой. '
            'Администратор сети — полный доступ к своим тенантам. '
            'Клиент — только аналитика и ответы на отзывы.'
        ),
    )
    # Для NETWORK_ADMIN и CLIENT — привязка к компаниям (тенантам)
    companies = models.ManyToManyField(
        'clients.Company',
        blank=True,
        related_name='admins',
        verbose_name='Компании',
        help_text='Тенанты, к которым у пользователя есть доступ. Для Супер Администратора (is_superuser) не требуется.',
    )

    # Профиль владельца для мобильного приложения (редактируется через /api/v1/me/)
    phone = models.CharField('Телефон', max_length=20, blank=True, default='')
    city = models.CharField('Город', max_length=80, blank=True, default='')
    birthday = models.DateField('Дата рождения', null=True, blank=True)
    # Фиксируется при первой установке ДР — после этого менять может только админ.
    birthday_set_at = models.DateTimeField('ДР зафиксирован', null=True, blank=True)

    @property
    def is_superadmin(self):
        return self.role == self.Role.SUPERADMIN

    @property
    def is_network_admin(self):
        return self.role == self.Role.NETWORK_ADMIN

    @property
    def is_client(self):
        return self.role == self.Role.CLIENT

    def save(self, *args, **kwargs):
        # is_superuser=True всегда означает SUPERADMIN
        if self.is_superuser:
            self.role = self.Role.SUPERADMIN
        # Все роли, кроме SUPERADMIN, требуют is_staff=True для аналитики
        # SUPERADMIN получает is_staff через is_superuser (Django имплицитно)
        # Но staff_member_required проверяет is_staff явно — ставим его всем
        self.is_staff = True
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.username} ({self.get_role_display()})'

    class Meta:
        verbose_name = 'Пользователь'
        verbose_name_plural = 'Пользователи'



class PushToken(models.Model):
    """
    Expo / APNs / FCM push-токен пользователя мобильного приложения LoyalUP.
    Один пользователь может иметь несколько токенов (несколько устройств).
    """
    class Platform(models.TextChoices):
        IOS     = 'ios',     'iOS'
        ANDROID = 'android', 'Android'
        WEB     = 'web',     'Web'

    user = models.ForeignKey(
        'users.User',
        on_delete=models.CASCADE,
        related_name='push_tokens',
        verbose_name='Пользователь',
    )
    token = models.CharField('Push-токен', max_length=255, unique=True)
    platform = models.CharField('Платформа', max_length=10, choices=Platform.choices)
    last_seen_at = models.DateTimeField('Последняя активность', auto_now=True)
    created_at = models.DateTimeField('Создан', auto_now_add=True)

    def __str__(self):
        return f'{self.user.username} · {self.platform} · {self.token[:16]}…'

    class Meta:
        verbose_name = 'Push-токен'
        verbose_name_plural = 'Push-токены'
        ordering = ['-last_seen_at']
        indexes = [
            models.Index(fields=['user', 'platform']),
            models.Index(fields=['-last_seen_at']),
        ]
