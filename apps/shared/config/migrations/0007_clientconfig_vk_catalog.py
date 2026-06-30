from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0006_clientconfig_story_activation_minutes_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='clientconfig',
            name='vk_catalog_enabled',
            field=models.BooleanField(default=False, help_text='Участвовать в сетевой игре для новичков, заходящих в мини-приложение из каталога VK (без QR). Город этой сети попадёт в список выбора, а новичок сможет выиграть приветственный подарок и забрать его в любой точке сети по коду дня. Нужен подарок с флагом «Приветственный подарок новичка» в каталоге. По умолчанию выключено.', verbose_name='Показывать в каталоге VK (сетевой вход)'),
        ),
        migrations.AddField(
            model_name='clientconfig',
            name='vk_catalog_city',
            field=models.CharField(blank=True, default='', help_text='Название города этой сети, как оно показывается новичку в списке выбора (напр. «Брянск»).', max_length=100, verbose_name='Город (для каталога VK)'),
        ),
    ]
