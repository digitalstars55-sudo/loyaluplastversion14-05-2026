"""
Migration: создаёт ReviewAutoReplyConfig (singleton per-tenant)
для мобильного API /api/v1/analytics/auto-reply/settings/.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0006_alter_testimonialconversation_branch'),
    ]

    operations = [
        migrations.CreateModel(
            name='ReviewAutoReplyConfig',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('enabled', models.BooleanField(default=True, verbose_name='Включено')),
                ('sentiment_positive', models.BooleanField(default=True, verbose_name='Позитивные')),
                ('sentiment_negative', models.BooleanField(default=True, verbose_name='Негативные')),
                ('sentiment_partially_negative', models.BooleanField(default=True, verbose_name='Частично негативные')),
                ('sentiment_neutral', models.BooleanField(default=True, verbose_name='Нейтральные')),
                ('sentiment_pending', models.BooleanField(default=True, verbose_name='Ожидают анализа')),
                ('branch_enabled', models.JSONField(blank=True, default=dict, help_text='Карта branch_id (str) → bool. Отсутствующие точки наследуют общий enabled.', verbose_name='Включено по точкам')),
                ('reminder_minutes', models.PositiveIntegerField(choices=[(30, '30 минут'), (60, '1 час'), (180, '3 часа'), (720, '12 часов')], default=180, verbose_name='Напоминание')),
                ('ai_tone', models.CharField(choices=[('formal', 'Официальный'), ('friendly', 'Дружелюбный'), ('neutral', 'Нейтральный')], default='friendly', max_length=10, verbose_name='Тон AI')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
            ],
            options={
                'verbose_name': 'Авто-ответы AI: настройки',
                'verbose_name_plural': 'Авто-ответы AI: настройки',
            },
        ),
    ]
