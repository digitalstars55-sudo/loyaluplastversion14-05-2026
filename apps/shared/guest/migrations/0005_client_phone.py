# №78 карты переезда: телефон гостя с согласия через ВКонтакте.
# Shared-модель (public-схема) → применять `migrate_schemas --shared`.
# Все поля с безопасными значениями по умолчанию: ADD COLUMN ... DEFAULT ''
# в PostgreSQL — метаданные, таблицу не переписывает.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('guest', '0004_alter_client_vk_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='phone',
            field=models.CharField(
                blank=True,
                default='',
                help_text='E.164 (+7…), с согласия гостя через ВКонтакте.',
                max_length=20,
                verbose_name='Телефон',
            ),
        ),
        migrations.AddField(
            model_name='client',
            name='phone_source',
            field=models.CharField(
                blank=True,
                default='',
                help_text="'vk' — подпись ВК сошлась; 'vk_unverified' — сохранён в режиме наблюдения без проверки подписи.",
                max_length=16,
                verbose_name='Источник телефона',
            ),
        ),
        migrations.AddField(
            model_name='client',
            name='phone_consent_at',
            field=models.DateTimeField(
                blank=True,
                help_text='Когда гость поделился номером (окно согласия ВК).',
                null=True,
                verbose_name='Согласие на телефон',
            ),
        ),
    ]
