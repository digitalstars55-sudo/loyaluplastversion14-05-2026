from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0017_testimonialmessage_attachments'),
    ]

    operations = [
        migrations.AddField(
            model_name='testimonialconversation',
            name='last_polled_vk_msg_id',
            field=models.BigIntegerField(
                default=0,
                help_text='Наибольший vk_message_id, который мы уже пытались утянуть в этом треде. На следующем тике берём только сообщения с id > этого значения, чтобы не терять старые при наплыве рассылок.',
                verbose_name='Курсор VK-поллинга',
            ),
        ),
    ]
