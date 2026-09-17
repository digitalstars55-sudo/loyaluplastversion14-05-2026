# №78: баллы за первый номер телефона, данный через ВК (настройка сети).
# Shared-модель → `migrate_schemas --shared`; ADD COLUMN ... DEFAULT 0 — метаданные.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0014_clientconfig_guest_phone_enabled'),
    ]

    operations = [
        migrations.AddField(
            model_name='clientconfig',
            name='guest_phone_reward_coins',
            field=models.PositiveSmallIntegerField(
                default=0,
                help_text=(
                    'Сколько баллов начислить гостю за первый номер, данный через ВК '
                    '(на счёт той точки, где он его дал). 0 — без награды. Подставляется '
                    'в текст авторассылки «Просьба поделиться номером» как {награда}.'
                ),
                verbose_name='Баллы за номер телефона (№78)',
            ),
        ),
    ]
