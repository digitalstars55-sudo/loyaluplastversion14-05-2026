"""
Initial migration for users app — adds PushToken model only.

The User model existed prior to migrations being introduced in this app
(it lives in shared schema and was synced via Django's built-in auth
migrations). We do NOT recreate it here. PushToken is the first
migration-tracked model in this app.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='PushToken',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('token', models.CharField(help_text='Expo push token, APNs token или FCM registration ID.', max_length=255, unique=True, verbose_name='Push-токен')),
                ('platform', models.CharField(choices=[('ios', 'iOS'), ('android', 'Android'), ('web', 'Web')], max_length=10, verbose_name='Платформа')),
                ('last_seen_at', models.DateTimeField(auto_now=True, verbose_name='Последняя активность')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создан')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='push_tokens', to=settings.AUTH_USER_MODEL, verbose_name='Пользователь')),
            ],
            options={
                'verbose_name': 'Push-токен',
                'verbose_name_plural': 'Push-токены',
                'ordering': ['-last_seen_at'],
                'indexes': [
                    models.Index(fields=['user', 'platform'], name='users_pusht_user_id_03b0f1_idx'),
                    models.Index(fields=['-last_seen_at'], name='users_pusht_last_se_3da32d_idx'),
                ],
            },
        ),
    ]
