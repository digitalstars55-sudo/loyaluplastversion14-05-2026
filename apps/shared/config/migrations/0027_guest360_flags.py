from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0026_clientconfig_pos_plugin_enabled'),
    ]

    operations = [
        migrations.AddField(
            model_name='clientconfig',
            name='guest_waiter_hint_enabled',
            field=models.BooleanField(db_default=False, default=False, help_text='Гость отсканировал QR на точке — официантам на смене в CheckUp приходит пуш: имя, сколько раз был, любимое, аллергии, ждущий подарок.', verbose_name='Подсказка официанту при скане'),
        ),
        migrations.AddField(
            model_name='clientconfig',
            name='guest_occasions_auto_enabled',
            field=models.BooleanField(db_default=False, default=False, help_text='Утром в день повода гостю уходит поздравление (тем же каналом, что и рассылки). ДР поздравляет прежняя автоверсия — её это не меняет.', verbose_name='Автопоздравления: годовщины и даты из заметок'),
        ),
        migrations.AddField(
            model_name='clientconfig',
            name='guest_occasion_text',
            field=models.TextField(blank=True, default='', help_text='{name} — имя гостя, {occasion} — повод. Пусто — текст по умолчанию.', verbose_name='Текст автопоздравления'),
        ),
    ]
