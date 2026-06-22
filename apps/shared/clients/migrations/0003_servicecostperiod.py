import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('clients', '0002_company_plan_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='ServiceCostPeriod',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('monthly_rub', models.DecimalField(decimal_places=2, help_text='Месячная стоимость нашей услуги для клиента в этот период.', max_digits=10, verbose_name='Стоимость обслуживания, ₽/мес')),
                ('start_date', models.DateField(help_text='Дата начала действия этой стоимости (включительно).', verbose_name='Действует с')),
                ('end_date', models.DateField(blank=True, help_text='Дата окончания (включительно). Пусто — действует сейчас.', null=True, verbose_name='Действует по')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('company', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='service_cost_periods', to='clients.company', verbose_name='Клиент')),
            ],
            options={
                'verbose_name': 'Стоимость обслуживания (период)',
                'verbose_name_plural': 'Стоимость обслуживания — история',
                'ordering': ['company', '-start_date'],
            },
        ),
        migrations.AddIndex(
            model_name='servicecostperiod',
            index=models.Index(fields=['company', 'start_date'], name='svccost_company_start_idx'),
        ),
    ]
