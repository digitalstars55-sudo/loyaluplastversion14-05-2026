import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0004_storygiftentry_website_source'),
        ('catalog', '0006_product_cost_price_rub'),
        ('branch', '0026_contactpointevent'),
    ]

    operations = [
        migrations.CreateModel(
            name='GiftCostEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('inventory', 'Приз гостя'), ('story', 'Подарок из сториз')], max_length=16, verbose_name='Тип подарка')),
                ('cost_rub', models.DecimalField(decimal_places=2, default=0, max_digits=10, verbose_name='Себестоимость на момент активации, ₽')),
                ('activated_at', models.DateTimeField(db_index=True, verbose_name='Активирован')),
                ('branch', models.ForeignKey(help_text='Точка активации (для сетевого website-подарка — где забрали).', on_delete=django.db.models.deletion.CASCADE, related_name='gift_cost_events', to='branch.branch', verbose_name='Точка')),
                ('client_branch', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='gift_cost_events', to='branch.clientbranch', verbose_name='Гость')),
                ('product', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='gift_cost_events', to='catalog.product', verbose_name='Подарок')),
            ],
            options={
                'verbose_name': 'Затрата на подарок',
                'verbose_name_plural': 'Затраты на подарки',
                'ordering': ['-activated_at'],
            },
        ),
        migrations.AddIndex(
            model_name='giftcostevent',
            index=models.Index(fields=['branch', 'activated_at'], name='giftcost_branch_act_idx'),
        ),
        migrations.AddIndex(
            model_name='giftcostevent',
            index=models.Index(fields=['activated_at'], name='giftcost_act_idx'),
        ),
    ]
