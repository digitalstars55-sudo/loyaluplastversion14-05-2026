from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0005_user_push_prefs'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='branch_access',
            field=models.JSONField(
                blank=True, default=dict,
                help_text=(
                    'Ограничения per-tenant. JSON: {"schema_name": "all"|[branch_id,...]}. '
                    'Если ключа нет — доступ ко всем точкам этого тенанта (по умолчанию).'
                ),
                verbose_name='Доступ к точкам',
            ),
        ),
    ]
