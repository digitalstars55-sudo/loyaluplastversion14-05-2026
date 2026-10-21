from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketer', '0007_studio_publish'),
    ]

    operations = [
        migrations.AddField(
            model_name='studioproject', name='series',
            field=models.CharField(blank=True, help_text='Серия роликов бренда: «реклама блюд», «команда»…',
                                   max_length=120, verbose_name='Серия'),
        ),
        migrations.AddField(
            model_name='studioproject', name='author_checkup_id',
            field=models.CharField(blank=True, help_text='id пользователя CheckUp — ему пуш «ролик готов».',
                                   max_length=64, verbose_name='Автор в CheckUp'),
        ),
        migrations.AddField(
            model_name='studioproject', name='proposed_plan',
            field=models.JSONField(blank=True, default=list,
                                   help_text='[{"channel", "fmt", "at"}] — встанет в план после «Опубликовать».',
                                   verbose_name='Предложенный план публикации'),
        ),
    ]
