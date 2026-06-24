from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0006_user_branch_access'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='feature_access',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    'Пусто = все разделы, доступные роли. Если отметить разделы — пользователь '
                    'видит ТОЛЬКО их (напр. только «Коды дня»). Работает вместе с доступом к точкам.'
                ),
                verbose_name='Доступ к разделам',
            ),
        ),
    ]
