from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0004_clientconfig_birthday_window_days'),
    ]

    operations = [
        migrations.AddField(
            model_name='clientconfig',
            name='brand_color_secondary',
            field=models.CharField(
                max_length=7,
                default='#d6de23',
                verbose_name='Второй (акцентный) цвет бренда (HEX)',
                help_text=(
                    'Акцентный цвет в формате #RRGGBB (по умолчанию лаймовый #d6de23). '
                    'Перекрашивает акцентные элементы миниаппа (кнопки действий, бейджи). '
                    'У бренда всегда два цвета — задайте оба для целостного вида.'
                ),
            ),
        ),
        migrations.AlterField(
            model_name='clientconfig',
            name='logotype_image',
            field=models.ImageField(
                upload_to='config/logos/',
                blank=True, null=True,
                verbose_name='Логотип',
                help_text=(
                    'Опционально (платный брендинг). Требования: PNG с прозрачным фоном, '
                    'квадрат (рекомендуется 512×512 px), сторона 256–2048 px, до 1 МБ.'
                ),
            ),
        ),
    ]
