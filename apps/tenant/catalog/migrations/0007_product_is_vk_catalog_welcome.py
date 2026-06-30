from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0006_product_cost_price_rub'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='is_vk_catalog_welcome',
            field=models.BooleanField(default=False, help_text='Подарок, который получает новичок, зашедший в мини-приложение из каталога VK (без QR) и выбравший этот город. Сетевой: забирается в любой точке сети по её коду дня. Используется ПЕРВЫЙ активный подарок с этим флагом. Не смешивается с супер-призами и сториз.', verbose_name='Приветственный подарок новичка (VK-каталог)'),
        ),
    ]
