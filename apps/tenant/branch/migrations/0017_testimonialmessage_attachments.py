from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0016_alter_branchconfig_birthday_window_days'),
    ]

    operations = [
        migrations.AddField(
            model_name='testimonialmessage',
            name='attachments',
            field=models.JSONField(blank=True, default=list, verbose_name='Вложения'),
        ),
    ]
