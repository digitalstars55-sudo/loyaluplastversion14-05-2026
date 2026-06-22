import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AuditEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Время')),
                ('actor_username', models.CharField(blank=True, db_index=True, max_length=150, verbose_name='Ник')),
                ('actor_role', models.CharField(blank=True, max_length=20, verbose_name='Роль')),
                ('tenant_schema', models.CharField(blank=True, db_index=True, max_length=63, verbose_name='Схема клиента')),
                ('tenant_name', models.CharField(blank=True, max_length=255, verbose_name='Клиент')),
                ('action', models.CharField(choices=[('login', 'Вход'), ('login_failed', 'Неудачный вход'), ('logout', 'Выход'), ('view', 'Просмотр'), ('create', 'Создание'), ('update', 'Изменение'), ('delete', 'Удаление')], db_index=True, max_length=16, verbose_name='Действие')),
                ('target', models.CharField(blank=True, max_length=255, verbose_name='Раздел / объект')),
                ('method', models.CharField(blank=True, max_length=8, verbose_name='Метод')),
                ('path', models.CharField(blank=True, max_length=512, verbose_name='Путь')),
                ('status_code', models.PositiveIntegerField(blank=True, null=True, verbose_name='Код')),
                ('ip', models.GenericIPAddressField(blank=True, null=True, verbose_name='IP')),
                ('user_agent', models.CharField(blank=True, max_length=300, verbose_name='User-Agent')),
                ('meta', models.JSONField(blank=True, default=dict, verbose_name='Доп. данные')),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='audit_events', to=settings.AUTH_USER_MODEL, verbose_name='Пользователь')),
            ],
            options={
                'verbose_name': 'Запись журнала',
                'verbose_name_plural': 'Журнал действий',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(fields=['-created_at'], name='audit_created_idx'),
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(fields=['actor', '-created_at'], name='audit_actor_created_idx'),
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(fields=['tenant_schema', '-created_at'], name='audit_tenant_created_idx'),
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(fields=['action', '-created_at'], name='audit_action_created_idx'),
        ),
    ]
