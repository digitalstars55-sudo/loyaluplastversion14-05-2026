# Этап 1 «Антихрупкость входа»: два пер-тенантных флага, оба default=False.
# Аддитивно: при выключенных флагах поведение прода не меняется.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0010_clientconfig_rf_orchestrator_enabled'),
    ]

    operations = [
        migrations.AddField(
            model_name='clientconfig',
            name='web_entry_enabled',
            field=models.BooleanField(default=False, help_text='Мини-приложение работает и вне ВК (браузер, Телеграм) — гость входит через VK ID и получает тот же профиль и баланс. Выключено — вне ВК показывается заглушка «Откройте во ВКонтакте», как раньше.', verbose_name='Веб-вход вне ВК (через VK ID)'),
        ),
        migrations.AddField(
            model_name='clientconfig',
            name='degrade_enabled',
            field=models.BooleanField(default=False, help_text='Если внутри ВК механизмы мини-приложения не отвечают — предложить гостю «Продолжить в браузере» вместо тупиковой ошибки. Требует включённого веб-входа. Выключено — прежнее поведение.', verbose_name='Умная деградация при сбое ВК'),
        ),
    ]
