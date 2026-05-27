from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0003_product_emoji'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='is_archived',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Скрытый подарок: не выдаётся в магазине, ДР-пуле и '
                    'супер-пуле. У гостей, которые уже получили его в '
                    'инвентарь, остаётся доступным к активации — данные не '
                    'теряются.'
                ),
                verbose_name='Архивирован',
            ),
        ),
        migrations.AddIndex(
            model_name='product',
            index=models.Index(fields=['is_archived'], name='catalog_prod_archived_idx'),
        ),
    ]
