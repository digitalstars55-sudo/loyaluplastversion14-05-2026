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

# Стоимость обслуживания — ФИКСИРОВАННАЯ месячная ставка, не зависит от длины
# выбранного периода (по запросу владельца). Суммы по клиентам всегда ровные
# (25 000, а не 25 833), не «пляшут» и не зависят от разных периодов оплаты.
# По периоду меняется только себестоимость подарков. Историю тарифов храним для
# записи/редактирования; для показателя берём ставку, действующую на конец периода.


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
    def monthly_rate_at(cls, company, as_of: _date) -> Decimal:
        """
        Месячная ставка тарифа, действующего на дату as_of. Если нет покрывающего
        периода — берём последний по дате начала. Если истории нет вообще — фолбэк
        на Company.plan_price_rub.
        """
        periods = list(company.service_cost_periods.all())
        for p in periods:
            if p.start_date <= as_of and (p.end_date is None or as_of <= p.end_date):
                return Decimal(p.monthly_rub)
        if periods:
            return Decimal(max(periods, key=lambda p: p.start_date).monthly_rub)
        return Decimal(company.plan_price_rub or 0)

    @classmethod
    def cost_for(cls, company, start: _date, end: _date) -> Decimal:
        """
        Стоимость обслуживания = ФИКСИРОВАННАЯ месячная ставка. НЕ зависит от
        выбранного периода вообще.

        По требованию владельца: обслуживание всегда показывает месячный тариф
        (например 25 000 ₽), одинаково на «7 дней», «30 дней», «май», «всё время».
        По периоду меняется ТОЛЬКО себестоимость подарков. Так цифры по клиентам
        не «пляшут» от длины периода и не зависят от того, что у всех разные
        периоды оплаты. Ставку берём действующую на конец периода (история
        тарифов сохраняется для записи и редактирования).
        """
        return cls.monthly_rate_at(company, end).quantize(Decimal('0.01'))

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
