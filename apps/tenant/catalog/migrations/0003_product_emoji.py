"""Migration: добавляет Product.emoji для отображения в мобильном приложении."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0002_productbranch_alter_product_options_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='emoji',
            field=models.CharField(
                blank=True,
                help_text='Один символ-эмодзи для отображения в мобильном приложении.',
                max_length=8,
                verbose_name='Эмодзи',
            ),
        ),
    ]
