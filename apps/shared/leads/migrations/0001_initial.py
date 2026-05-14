"""
Миграция для модели Lead. Создаёт таблицу leads_lead в SHARED-схеме.
Безопасна — только добавляет таблицу, не трогает существующие.

Применяется через стандартный django-tenants:
    python manage.py migrate_schemas --shared
"""

from django.conf import settings
from django.db import migrations, models

import apps.shared.leads.models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ('clients', '0001_initial'),
        # Зависимость от users.User (AUTH_USER_MODEL) — для FK confirmed_by
        # Будет автоматически добавлена Django при первом makemigrations.
        # Пока ссылаемся на settings.AUTH_USER_MODEL — Django сам разрулит.
    ]

    operations = [
        migrations.CreateModel(
            name='Lead',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('session_token', models.CharField(
                    db_index=True, default=apps.shared.leads.models._gen_token,
                    editable=False, max_length=64, unique=True, verbose_name='Сессионный токен',
                )),
                ('cafe_name', models.CharField(blank=True, max_length=200, verbose_name='Название кафе')),
                ('cafe_count', models.PositiveIntegerField(blank=True, null=True, verbose_name='Количество точек')),
                ('traffic_estimate', models.CharField(blank=True, max_length=200, verbose_name='Примерный трафик')),
                ('package_suggested', models.CharField(blank=True, max_length=50, verbose_name='Рекомендованный пакет')),
                ('full_name', models.CharField(blank=True, max_length=200, verbose_name='ФИО клиента')),
                ('email', models.EmailField(blank=True, db_index=True, max_length=254, verbose_name='Email')),
                ('vk_token', models.TextField(blank=True, verbose_name='VK API token')),
                ('domain_slug', models.CharField(
                    blank=True, max_length=63,
                    help_text='Без .levone.ru на конце. Можно поменять перед confirm.',
                    verbose_name='Поддомен (предложенный)',
                )),
                ('status', models.CharField(
                    choices=[
                        ('draft', 'Черновик (AI заполняет)'),
                        ('submitted', 'Отправлен — ждёт подтверждения'),
                        ('confirmed', 'Подтверждён — тенант создан'),
                        ('rejected', 'Отклонён'),
                    ],
                    db_index=True, default='draft', max_length=20, verbose_name='Статус',
                )),
                ('conversation_history', models.JSONField(
                    blank=True, default=list,
                    help_text='Список {role, text, ts} — полный диалог для аудита.',
                    verbose_name='История чата с AI',
                )),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Создан')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлён')),
                ('submitted_at', models.DateTimeField(blank=True, null=True, verbose_name='Отправлен на подтверждение')),
                ('confirmed_at', models.DateTimeField(blank=True, null=True, verbose_name='Подтверждён')),
                ('rejected_at', models.DateTimeField(blank=True, null=True, verbose_name='Отклонён')),
                ('rejection_reason', models.TextField(blank=True, verbose_name='Причина отклонения')),
                ('notified_super_admin', models.BooleanField(default=False, verbose_name='Уведомление супер-админу отправлено')),
                ('confirmed_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=models.deletion.SET_NULL,
                    related_name='confirmed_leads',
                    to=settings.AUTH_USER_MODEL,
                    verbose_name='Кто подтвердил',
                )),
                ('company', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=models.deletion.SET_NULL,
                    related_name='leads',
                    to='clients.company',
                    verbose_name='Созданная компания',
                )),
            ],
            options={
                'verbose_name': 'Заявка (Lead)',
                'verbose_name_plural': 'Заявки (Leads)',
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['status', '-created_at'], name='leads_lead_status_created_idx'),
                ],
            },
        ),
    ]
