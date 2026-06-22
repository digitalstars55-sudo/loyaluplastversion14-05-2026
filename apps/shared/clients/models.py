from datetime import date as _date
from decimal import Decimal

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


# ── ServiceCostPeriod ─────────────────────────────────────────────────────────

# Базис проренки: дневная ставка = месячная стоимость / 30. 30-дневный период
# при таком базисе стоит ровно одну месячную плату — интуитивно для дефолтного
# вида «30 дней» в сводной статистике (ТЗ §4, рекомендованный пропорциональный
# расчёт по дням).
_PRORATE_DAYS_IN_MONTH = Decimal(30)


class ServiceCostPeriod(models.Model):
    """
    История стоимости обслуживания клиента (ТЗ §4).

    Хранит, сколько стоила наша услуга клиенту в каждый интервал времени, чтобы
    отчёты за прошлые периоды считались по той ставке, которая действовала тогда,
    и не пересчитывались задним числом при смене цены. Открытый период
    (end_date=NULL) — действует сейчас. Засевается из Company.plan_price_rub.
    """

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name='service_cost_periods',
        verbose_name='Клиент',
    )
    monthly_rub = models.DecimalField(
        max_digits=10, decimal_places=2,
        verbose_name='Стоимость обслуживания, ₽/мес',
        help_text='Месячная стоимость нашей услуги для клиента в этот период.',
    )
    start_date = models.DateField(
        verbose_name='Действует с',
        help_text='Дата начала действия этой стоимости (включительно).',
    )
    end_date = models.DateField(
        null=True, blank=True,
        verbose_name='Действует по',
        help_text='Дата окончания (включительно). Пусто — действует сейчас.',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    @classmethod
    def cost_for(cls, company, start: _date, end: _date) -> Decimal:
        """
        Прорейтенная стоимость обслуживания клиента за период [start, end]
        (включительно) по истории тарифов. Дневная ставка = месячная / 30.

        Складывает проренку по каждому тарифному интервалу, пересекающему период.
        Если истории нет — фолбэк на текущий Company.plan_price_rub (тоже проренка),
        чтобы показатель работал до того, как менеджер заведёт историю.
        """
        periods = list(company.service_cost_periods.all())
        if not periods:
            monthly = Decimal(company.plan_price_rub or 0)
            days = (end - start).days + 1
            if days <= 0:
                return Decimal('0.00')
            return (monthly / _PRORATE_DAYS_IN_MONTH * Decimal(days)).quantize(Decimal('0.01'))

        total = Decimal('0')
        for p in periods:
            p_start = p.start_date
            p_end = p.end_date or end  # открытый период тянем до конца запроса
            o_start = max(start, p_start)
            o_end = min(end, p_end)
            days = (o_end - o_start).days + 1
            if days <= 0:
                continue
            total += p.monthly_rub / _PRORATE_DAYS_IN_MONTH * Decimal(days)
        return total.quantize(Decimal('0.01'))

    def __str__(self):
        tail = self.end_date.isoformat() if self.end_date else '…'
        return f'{self.company.name}: {self.monthly_rub}₽/мес ({self.start_date.isoformat()} — {tail})'

    class Meta:
        verbose_name = 'Стоимость обслуживания (период)'
        verbose_name_plural = 'Стоимость обслуживания — история'
        ordering = ['company', '-start_date']
        indexes = [
            models.Index(fields=['company', 'start_date'], name='svccost_company_start_idx'),
        ]
