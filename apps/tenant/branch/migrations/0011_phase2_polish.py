"""
Migration: закрывает пробелы Phase 2.

- DailyCode.generated_by — реальный AUTO/MANUAL источник.
- SupportChatMessage.read_at — отметка прочтения.
- StaffProfile — per-tenant поля сотрудника (телефон, права, доступ к точкам).
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0010_supportchatmessage'),
        ('users', '0002_pushtoken'),
    ]

    operations = [
        migrations.AddField(
            model_name='dailycode',
            name='generated_by',
            field=models.CharField(
                choices=[('auto', 'Автоматически (cron)'), ('manual', 'Вручную (через мобильное API)')],
                default='auto',
                help_text='Cron (auto) или ручной триггер из мобильного приложения (manual).',
                max_length=8,
                verbose_name='Источник генерации',
            ),
        ),
        migrations.AddField(
            model_name='supportchatmessage',
            name='read_at',
            field=models.DateTimeField(
                blank=True,
                help_text='Заполняется получателем при первом GET после доставки.',
                null=True,
                verbose_name='Прочитано',
            ),
        ),
        migrations.CreateModel(
            name='StaffProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('phone', models.CharField(blank=True, max_length=32, verbose_name='Телефон')),
                ('permissions', models.JSONField(blank=True, default=dict, help_text='Карта 14 флагов: see_analytics, edit_thresholds, manage_staff и т.д.', verbose_name='Права')),
                ('last_active_at', models.DateTimeField(blank=True, help_text='Обновляется при каждом запросе через мобильное API.', null=True, verbose_name='Последний вход')),
                ('invitation_token', models.CharField(blank=True, help_text='Одноразовая ссылка для установки пароля. Очищается после первого входа.', max_length=64, verbose_name='Токен приглашения')),
                ('branch_access', models.ManyToManyField(blank=True, help_text='Пустой список = доступ ко всем точкам тенанта.', related_name='staff_profiles', to='branch.branch', verbose_name='Доступ к точкам')),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='Пользователь')),
            ],
            options={
                'verbose_name': 'Профиль сотрудника',
                'verbose_name_plural': 'Профили сотрудников',
            },
        ),
    ]
