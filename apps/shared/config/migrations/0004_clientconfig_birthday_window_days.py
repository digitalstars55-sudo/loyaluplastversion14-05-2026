from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0003_clientconfig_brand_color'),
    ]

    operations = [
        migrations.AddField(
            model_name='clientconfig',
            name='birthday_window_days',
            field=models.PositiveSmallIntegerField(
                default=5,
                verbose_name='Окно подарка ДР — на всю сеть (дней ±)',
                help_text=(
                    'Значение по умолчанию для всей сети: за сколько дней до и после ДР '
                    'гостю доступен подарок. 0 = только в день рождения. Точка может '
                    'переопределить в своих настройках; кнопка «Применить ко всем точкам» '
                    'проставит это значение всем точкам сразу.'
                ),
            ),
        ),
    ]
