from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0015_testimonialconversation_last_reminded_at'),
    ]

    operations = [
        migrations.AlterField(
            model_name='branchconfig',
            name='birthday_window_days',
            field=models.PositiveSmallIntegerField(
                null=True, blank=True,
                verbose_name='Окно подарка ДР (±дней)',
                help_text=(
                    'Сколько дней до и после ДР гость может получить подарок. '
                    '0 — только день-в-день; 1 — ±1 день; 5 — стандартно ±5 дней. '
                    'Пусто = использовать значение сети (настройки клиента). '
                    'Резолв: точка → сеть → 5 по умолчанию.'
                ),
            ),
        ),
    ]
