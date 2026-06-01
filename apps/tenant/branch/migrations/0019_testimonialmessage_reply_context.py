from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0018_testimonialconversation_last_polled_vk_msg_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='testimonialmessage',
            name='reply_to_text',
            field=models.TextField(blank=True, default='', verbose_name='Контекст (на что ответ)'),
        ),
        migrations.AddField(
            model_name='testimonialmessage',
            name='reply_to_date',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Время контекста'),
        ),
    ]
