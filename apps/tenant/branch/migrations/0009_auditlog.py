"""
Migration: создаёт AuditLog для GET /api/v1/audit-log/.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0008_testimonial_ai_draft'),
        ('users', '0002_pushtoken'),
    ]

    operations = [
        migrations.CreateModel(
            name='AuditLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('staff_name', models.CharField(blank=True, help_text='Сохраняется на момент действия — переживает удаление User.', max_length=255, verbose_name='Имя сотрудника (снимок)')),
                ('action_type', models.CharField(choices=[
                    ('COIN_ADJUST', 'Корректировка баланса'),
                    ('REVIEW_REPLY', 'Ответ на отзыв'),
                    ('REVIEW_RESOLVE', 'Закрытие отзыва'),
                    ('BROADCAST_SEND', 'Рассылка'),
                    ('PRODUCT_CREATE', 'Создан подарок'),
                    ('PRODUCT_UPDATE', 'Изменён подарок'),
                    ('PRODUCT_DELETE', 'Удалён подарок'),
                    ('QUEST_CREATE', 'Создан квест'),
                    ('QUEST_UPDATE', 'Изменён квест'),
                    ('QUEST_DELETE', 'Удалён квест'),
                    ('PROMO_CREATE', 'Создана акция'),
                    ('PROMO_UPDATE', 'Изменена акция'),
                    ('PROMO_DELETE', 'Удалена акция'),
                    ('STAFF_INVITE', 'Приглашён сотрудник'),
                    ('STAFF_TOGGLE', 'Изменён статус сотрудника'),
                    ('STAFF_PERMS', 'Изменены права'),
                    ('THRESHOLDS_SAVE', 'Сохранены пороги RF'),
                    ('AUTO_REPLY_SAVE', 'Изменены настройки AI-ответов'),
                    ('DAILY_CODE_MANUAL', 'Ручной код дня'),
                    ('AUTH_LOGIN', 'Вход'),
                    ('AUTH_LOGOUT', 'Выход'),
                ], db_index=True, max_length=32, verbose_name='Действие')),
                ('target_type', models.CharField(blank=True, help_text='guest | review | product | quest | promotion | staff | broadcast | thresholds | daily_code', max_length=32, verbose_name='Тип объекта')),
                ('target_id', models.CharField(blank=True, max_length=64, verbose_name='ID объекта')),
                ('target_label', models.CharField(blank=True, help_text='Например, имя гостя или название подарка — сохраняется как снимок.', max_length=255, verbose_name='Метка объекта')),
                ('details', models.TextField(blank=True, verbose_name='Детали')),
                ('delta', models.JSONField(blank=True, default=dict, verbose_name='Изменения (before/after)')),
                ('staff', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='Сотрудник')),
            ],
            options={
                'verbose_name': 'Запись аудит-лога',
                'verbose_name_plural': 'Аудит-лог',
                'ordering': ['-created_at'],
                'indexes': [
                    models.Index(fields=['staff', '-created_at'], name='audit_staff_created_idx'),
                    models.Index(fields=['action_type', '-created_at'], name='audit_action_created_idx'),
                ],
            },
        ),
    ]
