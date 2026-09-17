# №78: новый источник монет 'phone' — спасибо за номер телефона, данный через ВК.
# Только choices: SQL не меняется, применяется по всем тенантным схемам без блокировок.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0036_testimonialconversation_checkup_verdict'),
    ]

    operations = [
        migrations.AlterField(
            model_name='cointransaction',
            name='source',
            field=models.CharField(choices=[('game', 'Игра'), ('quest', 'Квест'), ('shop', 'Магазин'), ('birthday', 'День рождения'), ('delivery', 'Доставка'), ('manual', 'Вручную'), ('rfm', 'RFM-кампания'), ('phone', 'Номер телефона')], max_length=20, verbose_name='Источник'),
        ),
    ]
