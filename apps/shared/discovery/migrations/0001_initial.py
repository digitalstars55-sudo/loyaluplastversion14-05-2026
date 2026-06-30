import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('clients', '0004_seed_service_cost'),
    ]

    operations = [
        migrations.CreateModel(
            name='DiscoveryEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('vk_id', models.PositiveIntegerField(db_index=True, verbose_name='VK ID гостя')),
                ('stage', models.CharField(choices=[('open', 'Открыл сетевой вход'), ('play', 'Крутанул колесо'), ('claim_open', 'Открыл список городов')], db_index=True, max_length=16, verbose_name='Стадия воронки')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Когда')),
            ],
            options={
                'verbose_name': 'Событие сетевого входа (VK-каталог)',
                'verbose_name_plural': 'События сетевого входа (VK-каталог)',
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='DiscoveryClaim',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('vk_id', models.PositiveIntegerField(unique=True, verbose_name='VK ID гостя')),
                ('city', models.CharField(blank=True, default='', max_length=100, verbose_name='Город')),
                ('home_branch_id', models.PositiveIntegerField(help_text='branch_id точки, на которой создан StoryGiftEntry новичка.', verbose_name='Точка-«дом» подарка')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Выбрал город')),
                ('redeemed_at', models.DateTimeField(blank=True, db_index=True, null=True, verbose_name='Активировал на кассе')),
                ('redeemed_branch_id', models.PositiveIntegerField(blank=True, help_text='branch_id точки, где забрали по коду дня.', null=True, verbose_name='Точка активации')),
                ('company', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='discovery_claims', to='clients.company', verbose_name='Выбранная сеть (тенант)')),
            ],
            options={
                'verbose_name': 'Приз новичка (VK-каталог)',
                'verbose_name_plural': 'Призы новичков (VK-каталог)',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='discoveryevent',
            index=models.Index(fields=['stage', 'created_at'], name='disc_stage_created_idx'),
        ),
    ]
