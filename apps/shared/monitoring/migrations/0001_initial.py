from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='MonitorAlertState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(db_index=True, max_length=120, unique=True, verbose_name='Ключ сигнала')),
                ('severity', models.CharField(choices=[('warn', 'Предупреждение'), ('critical', 'Критично')], max_length=10, verbose_name='Важность')),
                ('title', models.CharField(max_length=255, verbose_name='Заголовок')),
                ('body', models.TextField(blank=True, verbose_name='Текст')),
                ('data', models.JSONField(blank=True, default=dict, verbose_name='Доп. данные')),
                ('first_seen_at', models.DateTimeField(auto_now_add=True, verbose_name='Впервые замечен')),
                ('last_seen_at', models.DateTimeField(verbose_name='Замечен в последний раз')),
                ('last_sent_at', models.DateTimeField(blank=True, null=True, verbose_name='Последний пуш')),
                ('resolved_at', models.DateTimeField(blank=True, null=True, verbose_name='Закрыт')),
                ('sent_count', models.PositiveIntegerField(default=0, verbose_name='Сколько раз слали')),
            ],
            options={
                'verbose_name': 'Сигнал мониторинга',
                'verbose_name_plural': 'Сигналы мониторинга',
                'ordering': ['-last_seen_at'],
            },
        ),
    ]
