# Фаза 5 (RFM-награды): новый источник транзакции баллов 'rfm' —
# начисления/откаты RFM-кампаний. Только choices, схема БД не меняется.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0029_qrcode_review_mode'),
    ]

    operations = [
        migrations.AlterField(
            model_name='cointransaction',
            name='source',
            field=models.CharField(
                choices=[
                    ('game', 'Игра'),
                    ('quest', 'Квест'),
                    ('shop', 'Магазин'),
                    ('birthday', 'День рождения'),
                    ('delivery', 'Доставка'),
                    ('manual', 'Вручную'),
                    ('rfm', 'RFM-кампания'),
                ],
                max_length=20,
                verbose_name='Источник',
            ),
        ),
    ]
