"""
Профиль владельца для мобильного приложения LoyalUP.

Добавляет редактируемые через /api/v1/me/ поля на User:
phone, city, birthday, birthday_set_at. Все non-destructive (AddField,
nullable / с дефолтом) — безопасно применять на прод-схеме без потери данных.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0002_pushtoken'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='phone',
            field=models.CharField(blank=True, default='', max_length=20, verbose_name='Телефон'),
        ),
        migrations.AddField(
            model_name='user',
            name='city',
            field=models.CharField(blank=True, default='', max_length=80, verbose_name='Город'),
        ),
        migrations.AddField(
            model_name='user',
            name='birthday',
            field=models.DateField(blank=True, null=True, verbose_name='Дата рождения'),
        ),
        migrations.AddField(
            model_name='user',
            name='birthday_set_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='ДР зафиксирован'),
        ),
    ]
