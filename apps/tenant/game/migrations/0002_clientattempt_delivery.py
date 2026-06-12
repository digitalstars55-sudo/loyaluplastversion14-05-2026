from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('game', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='clientattempt',
            name='delivery',
            field=models.BooleanField(
                default=False,
                db_index=True,
                help_text='True — сессия начата гостем доставки (а не по QR в кафе). '
                          'Источник сканирования для аналитики: кафе vs доставка.',
                verbose_name='Игра с доставки',
            ),
        ),
    ]
