from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0002_clientconfig_code_prompt_message_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='clientconfig',
            name='brand_color',
            field=models.CharField(
                max_length=7,
                default='#d3a9e5',
                help_text=(
                    'Главный цвет VK мини-приложения в формате #RRGGBB. По нему '
                    'автоматически генерируются производные оттенки (тёмный, '
                    'светлый, и т.д.) — весь миниапп перекрашивается одним '
                    'полем. По умолчанию фиолетовый #d3a9e5.'
                ),
                verbose_name='Главный цвет бренда (HEX)',
            ),
        ),
    ]
