# Режим «Отзыв со стола» для точек контакта (QR): новый выбор в mode
# + номер стола. Аддитивно, существующие QR не затрагиваются.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0028_alter_qrcode_mode'),
    ]

    operations = [
        migrations.AddField(
            model_name='qrcode',
            name='table_number',
            field=models.PositiveIntegerField(blank=True, null=True, help_text='Только для типа «Отзыв со стола»: ссылка откроет форму отзыва с привязкой к этому столу. Для остальных типов не заполняется.', verbose_name='Номер стола'),
        ),
        migrations.AlterField(
            model_name='qrcode',
            name='mode',
            field=models.CharField(choices=[('cafe', 'В кафе (на месте)'), ('delivery', 'Доставка'), ('delivery_network', 'Доставка — вся сеть (один QR)'), ('website', 'С сайта (сетевой подарок)'), ('review', 'Отзыв со стола')], default='cafe', help_text='«Доставка» добавляет delivery=true. «С сайта» добавляет web=<метка> (игра с сайта → сетевой подарок, забор в любой точке по её коду дня).', max_length=16, verbose_name='Тип ссылки'),
        ),
    ]
