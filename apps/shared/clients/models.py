from django.db import models
from django_tenants.models import TenantMixin, DomainMixin


class Company(TenantMixin):
    client_id = models.PositiveIntegerField(
        unique=True,
        verbose_name='ID клиента',
        help_text='Используется в QR кодах и ссылках',
    )
    name = models.CharField(max_length=255, verbose_name='Название')
    description = models.TextField(
        verbose_name='Описание',
        blank=True, null=True,
        help_text='Для удобства, ни на что не влияет',
    )
    is_active = models.BooleanField(
        default=False,
        verbose_name='Активен',
        help_text='Активно/Неактивно',
    )
    paid_until = models.DateField(
        verbose_name='Оплачено до',
        help_text='В этот день приложение у клиента перестанет работать',
    )

    class Plan(models.TextChoices):
        STARTER  = 'starter',  'Стартовый'
        STANDARD = 'standard', 'Стандарт'
        PRO      = 'pro',      'Pro'

    plan_code = models.CharField(
        max_length=16, choices=Plan.choices, default=Plan.STANDARD,
        verbose_name='Тариф',
    )
    plan_price_rub = models.PositiveIntegerField(
        default=4900,
        verbose_name='Цена тарифа (₽)',
        help_text='Стоимость подписки в месяц.',
    )
    auto_pay_enabled = models.BooleanField(
        default=False,
        verbose_name='Авто-оплата',
        help_text='Включён авто-платёж по карте.',
    )

    auto_create_schema = True

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = 'Клиент'
        verbose_name_plural = 'Клиенты'


class Domain(DomainMixin):
    class Meta:
        verbose_name = 'Домен'
        verbose_name_plural = 'Домены'
