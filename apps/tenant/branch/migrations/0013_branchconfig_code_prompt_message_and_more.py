from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0012_drop_user_fks'),
    ]

    operations = [
        migrations.AddField(
            model_name='branchconfig',
            name='code_prompt_message',
            field=models.TextField(
                blank=True,
                default='',
                help_text=(
                    'Перезаписывает аналогичное поле в настройках сети ТОЛЬКО '
                    'для этой точки. Оставьте пустым, чтобы использовать общий '
                    'текст из настроек тенанта.'
                ),
                verbose_name='Подсказка про код монет (для этой точки)',
            ),
        ),
        migrations.AddField(
            model_name='branchconfig',
            name='quest_show_message',
            field=models.TextField(
                blank=True,
                default='',
                help_text=(
                    'Перезаписывает текст подсказки в активации задания только '
                    'для этой точки.'
                ),
                verbose_name='Подсказка про показ задания (для этой точки)',
            ),
        ),
    ]
