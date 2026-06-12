from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0020_branchconfig_story_activation_text_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='clientvkstatus',
            name='community_source',
            field=models.CharField(
                max_length=16,
                choices=[('cafe', 'Кафе'), ('delivery', 'Доставка'), ('story', 'Сториз')],
                null=True,
                blank=True,
                db_index=True,
                help_text='Откуда подписался: кафе / доставка / сториз. Заполняется при via_app-подписке.',
                verbose_name='Источник подписки (сообщество)',
            ),
        ),
        migrations.AddField(
            model_name='clientvkstatus',
            name='newsletter_source',
            field=models.CharField(
                max_length=16,
                choices=[('cafe', 'Кафе'), ('delivery', 'Доставка'), ('story', 'Сториз')],
                null=True,
                blank=True,
                db_index=True,
                help_text='Откуда подписался: кафе / доставка / сториз. Заполняется при via_app-подписке.',
                verbose_name='Источник подписки (рассылка)',
            ),
        ),
    ]
