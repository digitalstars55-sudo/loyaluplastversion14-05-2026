import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('guest', '0001_initial'),
        ('branch', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='LoyaltyOrder',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('external_order_id', models.CharField(db_index=True, max_length=64, unique=True, verbose_name='ID заказа (BFF)')),
                ('order_amount', models.PositiveIntegerField(help_text='Рублёвая сумма заказа. Идёт в зачёт трат для статусов (если не возвращён).', verbose_name='Сумма заказа, ₽')),
                ('points_earned', models.PositiveIntegerField(default=0, verbose_name='Начислено баллов')),
                ('points_redeemed', models.PositiveIntegerField(default=0, verbose_name='Списано баллов')),
                ('status', models.CharField(choices=[('active', 'Активен'), ('refunded', 'Возвращён')], db_index=True, default='active', max_length=10, verbose_name='Статус')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Создан')),
                ('refunded_at', models.DateTimeField(blank=True, null=True, verbose_name='Возвращён')),
                ('branch', models.ForeignKey(help_text='Где оформлен заказ. Баланс сетевой, точка — для аналитики/записи.', on_delete=django.db.models.deletion.PROTECT, related_name='loyalty_orders', to='branch.branch', verbose_name='Точка')),
                ('client', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='loyalty_orders', to='guest.client', verbose_name='Гость')),
            ],
            options={
                'verbose_name': 'Лоялти-заказ',
                'verbose_name_plural': 'Лоялти-заказы',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='LoyaltyIdempotencyKey',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=128, unique=True, verbose_name='Ключ идемпотентности')),
                ('response', models.JSONField(verbose_name='Сохранённый ответ')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Создан')),
            ],
            options={
                'verbose_name': 'Ключ идемпотентности',
                'verbose_name_plural': 'Ключи идемпотентности',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='loyaltyorder',
            index=models.Index(fields=['client', 'status', 'created_at'], name='loyorder_client_stat_idx'),
        ),
    ]
