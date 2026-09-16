from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CheckUpIdentity',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('checkup_user_id', models.CharField(db_index=True, max_length=64, verbose_name='ID пользователя CheckUp')),
                ('tenant_schema', models.CharField(max_length=63, verbose_name='Сеть (schema_name)')),
                ('display_name', models.CharField(blank=True, max_length=150, verbose_name='Имя из CheckUp')),
                ('email', models.CharField(blank=True, max_length=254, verbose_name='Email из CheckUp (только показ)')),
                ('last_role', models.CharField(blank=True, max_length=20, verbose_name='Роль в последнем обмене')),
                ('last_branch_ids', models.JSONField(blank=True, default=list, verbose_name='Публичные branch_id в последнем обмене')),
                ('exchanges_count', models.PositiveIntegerField(default=0, verbose_name='Обменов')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('last_exchanged_at', models.DateTimeField(blank=True, null=True, verbose_name='Последний обмен')),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='checkup_identity', to=settings.AUTH_USER_MODEL, verbose_name='Пользователь LoyalUP')),
            ],
            options={
                'verbose_name': 'Личность CheckUp',
                'verbose_name_plural': 'Личности CheckUp',
                'unique_together': {('checkup_user_id', 'tenant_schema')},
            },
        ),
    ]
