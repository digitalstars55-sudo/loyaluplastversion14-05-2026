from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='clientconfig',
            name='code_prompt_message',
            field=models.TextField(
                blank=True,
                default='ЧТОБЫ ЗАБРАТЬ МОНЕТЫ, ПОПРОСИТЕ КОД ДНЯ У СОТРУДНИКА',
                help_text=(
                    'Полный текст подсказки, который видит гость в модалке ввода '
                    'кода. Можно писать любую фразу — она показывается целиком. '
                    'Точка может переопределить в своих настройках.'
                ),
                verbose_name='Подсказка про код монет',
            ),
        ),
        migrations.AddField(
            model_name='clientconfig',
            name='quest_show_message',
            field=models.TextField(
                blank=True,
                default='У ВАС ЕСТЬ 30 МИНУТ, ЧТОБЫ ВЫПОЛНИТЬ ЗАДАНИЕ И ПОКАЗАТЬ РЕЗУЛЬТАТ СОТРУДНИКУ.',
                help_text=(
                    'Полный текст подсказки в активации задания (квеста). Можно '
                    'править свободно — например, заменить «сотруднику» на '
                    '«администратору» или сократить «30 минут».'
                ),
                verbose_name='Подсказка про показ задания',
            ),
        ),
    ]
