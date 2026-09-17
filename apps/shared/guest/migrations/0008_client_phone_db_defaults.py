# Дефолты на уровне БД для phone / phone_source / phone_placement.
# Урок 17.09: AddField с default='' ставит DEFAULT только на время миграции и
# сразу снимает его; старый код (celery до перезапуска) вставлял строки
# guest_client без новых колонок → IntegrityError, поллинг ВК 25 минут не мог
# заводить новых гостей. ALTER COLUMN SET DEFAULT — метаданные, без блокировки.
# Shared-модель → `migrate_schemas --shared`.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('guest', '0007_client_phone_reward_at'),
    ]

    operations = [
        migrations.AlterField(
            model_name='client',
            name='phone',
            field=models.CharField(
                blank=True,
                db_default='',
                default='',
                help_text='E.164 (+7…), с согласия гостя через ВКонтакте.',
                max_length=20,
                verbose_name='Телефон',
            ),
        ),
        migrations.AlterField(
            model_name='client',
            name='phone_source',
            field=models.CharField(
                blank=True,
                db_default='',
                default='',
                help_text="'vk' — подпись ВК сошлась; 'vk_unverified' — сохранён в режиме наблюдения без проверки подписи.",
                max_length=16,
                verbose_name='Источник телефона',
            ),
        ),
        migrations.AlterField(
            model_name='client',
            name='phone_placement',
            field=models.CharField(
                blank=True,
                db_default='',
                default='',
                help_text="Место в мини-аппе: 'profile', 'review', … — чтобы сравнивать, где гости соглашаются.",
                max_length=32,
                verbose_name='Где дал номер',
            ),
        ),
    ]
