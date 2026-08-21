# RF-оркестратор (ТЗ авторассылок v1.1 §5) — флаг сети, по умолчанию выключен.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0009_clientconfig_auto_broadcast_weekly_cap'),
    ]

    operations = [
        migrations.AddField(
            model_name='clientconfig',
            name='rf_orchestrator_enabled',
            field=models.BooleanField(default=False, help_text='Глобальные правила частоты RF-сообщений: минимум 72 часа между ними, не более 2 за 14 дней и 3 за 30 дней, пауза 7 дней после визита, заморозка ±7 дней вокруг дня рождения. Выключено — поведение прежнее. Включайте вместе с запуском RF-правил.', verbose_name='RF-оркестратор (антиспам v1.1)'),
        ),
    ]
