from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0013_branchconfig_code_prompt_message_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='branchconfig',
            name='birthday_window_days',
            field=models.PositiveSmallIntegerField(
                default=5,
                help_text=(
                    'Сколько дней до и после ДР гость может получить подарок. '
                    '0 — только день-в-день; 1 — ±1 день; 5 — стандартно ±5 '
                    'дней; 14 — две недели и т.д. Каждая точка настраивается '
                    'независимо.'
                ),
                verbose_name='Окно подарка ДР (±дней)',
            ),
        ),
    ]
