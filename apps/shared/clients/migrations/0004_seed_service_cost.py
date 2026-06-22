"""
Засев истории стоимости обслуживания из текущего Company.plan_price_rub.

Для каждого клиента (кроме public) создаём один открытый период (end_date=NULL)
с monthly_rub = plan_price_rub, действующий с давней даты, чтобы покрыть любые
запрашиваемые периоды сводной статистики. Идемпотентно: если у клиента уже есть
запись истории — пропускаем.
"""
from datetime import date

from django.db import migrations

# Достаточно ранняя дата, чтобы засеянный тариф покрыл все периоды отчётов.
_SEED_START = date(2024, 1, 1)


def seed_service_cost(apps, schema_editor):
    Company = apps.get_model('clients', 'Company')
    ServiceCostPeriod = apps.get_model('clients', 'ServiceCostPeriod')

    for company in Company.objects.exclude(schema_name='public'):
        if ServiceCostPeriod.objects.filter(company=company).exists():
            continue
        ServiceCostPeriod.objects.create(
            company=company,
            monthly_rub=company.plan_price_rub or 0,
            start_date=_SEED_START,
            end_date=None,
        )


def unseed(apps, schema_editor):
    ServiceCostPeriod = apps.get_model('clients', 'ServiceCostPeriod')
    ServiceCostPeriod.objects.filter(start_date=_SEED_START).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0003_servicecostperiod'),
    ]

    operations = [
        migrations.RunPython(seed_service_cost, unseed),
    ]
