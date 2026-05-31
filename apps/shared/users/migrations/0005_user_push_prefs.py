from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0004_notification'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='push_prefs',
            field=models.JSONField(
                blank=True, default=dict,
                help_text='Какие пуши и с каких тенантов получать. Подробнее см. модель.',
                verbose_name='Настройки push',
            ),
        ),
    ]
