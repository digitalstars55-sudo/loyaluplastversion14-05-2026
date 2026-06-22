from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0005_product_is_story_prize'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='cost_price_rub',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text=(
                    'Реальная стоимость подарка в рублях. Попадает в «Экономику клиента» '
                    'по факту активации подарка гостем — берётся снимок себестоимости на '
                    'момент активации, поэтому изменение этого поля не меняет прошлые отчёты.'
                ),
                max_digits=10,
                verbose_name='Себестоимость, ₽',
            ),
        ),
    ]
