# Стол кассы у точки контакта (24.09.2026, «скан со стола → официант стола»):
# id стола в кассе и зал. Аддитивно: два новых поля с пустым умолчанием, у
# существующих QR ничего не меняется, ссылки прежние. table_number — только
# новый текст подсказки (стол теперь бывает и у «В кафе»), SQL не меняется.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0037_alter_cointransaction_source'),
    ]

    operations = [
        migrations.AddField(
            model_name='qrcode',
            name='table_external_id',
            field=models.CharField(blank=True, db_index=True, default='', help_text='Для QR на столе: id стола в кассе (iiko — UUID стола из схемы залов). Заполняет CheckUp при создании QR по схеме залов. В ссылку не попадает.', max_length=64, verbose_name='ID стола в кассе'),
        ),
        migrations.AddField(
            model_name='qrcode',
            name='table_hall',
            field=models.CharField(blank=True, default='', help_text='Зал стола в кассе («Зал», «Бар», «Летник»). Номера столов в разных залах повторяются — без зала «стол 1» неоднозначен.', max_length=120, verbose_name='Зал'),
        ),
        migrations.AlterField(
            model_name='qrcode',
            name='table_number',
            field=models.PositiveIntegerField(blank=True, help_text='«Отзыв со стола»: обязателен — ссылка откроет форму отзыва с привязкой к этому столу. «В кафе»: по желанию — QR лежит на столе, скан относится к этому столу (игра, подписка), ссылка при этом прежняя. Для доставки и сайта не заполняется.', null=True, verbose_name='Номер стола'),
        ),
    ]
