import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0023_branch_review_links_default'),
        ('analytics', '0004_alter_rfsegment_options_rfsegment_branch_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='DailyOrderStat',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('date', models.DateField(db_index=True, verbose_name='Дата')),
                ('orders_total', models.PositiveIntegerField(default=0, verbose_name='Всего заказов')),
                ('orders_in_cafe', models.PositiveIntegerField(default=0, verbose_name='В кафе')),
                ('orders_pickup_admin', models.PositiveIntegerField(default=0, verbose_name='Самовывоз (админ)')),
                ('orders_delivery_admin', models.PositiveIntegerField(default=0, verbose_name='Доставка (админ)')),
                ('source', models.CharField(default='dooglys', max_length=20, verbose_name='Источник POS')),
                ('cafe_name_raw', models.CharField(blank=True, default='', max_length=255, verbose_name='Название кафе (от POS)')),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='daily_order_stats', to='branch.branch', verbose_name='Торговая точка')),
            ],
            options={
                'verbose_name': 'Суточные заказы (POS)',
                'verbose_name_plural': 'Суточные заказы (POS)',
                'ordering': ['-date'],
                'indexes': [models.Index(fields=['branch', 'date'], name='daily_orders_branch_date_idx')],
                'unique_together': {('branch', 'date')},
            },
        ),
    ]
