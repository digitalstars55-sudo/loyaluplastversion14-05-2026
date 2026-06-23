from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0026_contactpointevent'),
    ]

    operations = [
        migrations.AlterField(
            model_name='qrcode',
            name='mode',
            field=models.CharField(
                choices=[('cafe', 'В кафе (на месте)'), ('delivery', 'Доставка'), ('website', 'С сайта (сетевой подарок)')],
                default='cafe',
                help_text='«Доставка» добавляет delivery=true. «С сайта» добавляет web=<метка> (игра с сайта → сетевой подарок, забор в любой точке по её коду дня).',
                max_length=16,
                verbose_name='Тип ссылки',
            ),
        ),
    ]
