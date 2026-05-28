from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0014_branchconfig_birthday_window_days'),
    ]

    operations = [
        migrations.AddField(
            model_name='testimonialconversation',
            name='last_reminded_at',
            field=models.DateTimeField(
                blank=True, null=True,
                help_text='Когда последний раз отправляли push-напоминание о неотвеченном черновике. Не чаще 1 раза в сутки.',
                verbose_name='Последнее напоминание',
            ),
        ),
    ]
