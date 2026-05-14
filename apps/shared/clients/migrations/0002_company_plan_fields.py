"""Migration: добавляет в Company поля тарифа для мобильного API /billing/status/."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='company',
            name='plan_code',
            field=models.CharField(
                choices=[('starter', 'Стартовый'), ('standard', 'Стандарт'), ('pro', 'Pro')],
                default='standard',
                max_length=16,
                verbose_name='Тариф',
            ),
        ),
        migrations.AddField(
            model_name='company',
            name='plan_price_rub',
            field=models.PositiveIntegerField(
                default=4900,
                help_text='Стоимость подписки в месяц.',
                verbose_name='Цена тарифа (₽)',
            ),
        ),
        migrations.AddField(
            model_name='company',
            name='auto_pay_enabled',
            field=models.BooleanField(
                default=False,
                help_text='Включён авто-платёж по карте.',
                verbose_name='Авто-оплата',
            ),
        ),
    ]
