"""
Migration: создаёт SupportChatMessage для GET/POST /api/v1/support/chat/messages/.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0009_auditlog'),
        ('users', '0002_pushtoken'),
    ]

    operations = [
        migrations.CreateModel(
            name='SupportChatMessage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('sender', models.CharField(choices=[('user', 'Пользователь'), ('manager', 'Менеджер')], db_index=True, max_length=8, verbose_name='Отправитель')),
                ('text', models.TextField(blank=True, verbose_name='Текст')),
                ('author', models.ForeignKey(blank=True, help_text='Заполняется автоматически для sender=user. Менеджер может писать снаружи (тогда NULL).', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='Автор (если внутри платформы)')),
            ],
            options={
                'verbose_name': 'Сообщение в чате с менеджером',
                'verbose_name_plural': 'Чат с менеджером',
                'ordering': ['created_at'],
                'indexes': [
                    models.Index(fields=['created_at'], name='supchat_created_idx'),
                    models.Index(fields=['sender', '-created_at'], name='supchat_sender_idx'),
                ],
            },
        ),
    ]
