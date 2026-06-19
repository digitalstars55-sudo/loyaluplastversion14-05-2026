from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0021_clientvkstatus_subscription_source'),
    ]

    operations = [
        migrations.AddField(
            model_name='branch',
            name='review_link_yandex',
            field=models.URLField(blank=True, help_text='Ссылка на отзывы этой точки на Яндекс Картах. Подставляется кнопкой в ответ ТОЛЬКО на позитивные отзывы.', max_length=500, verbose_name='Ссылка Яндекс Карты'),
        ),
        migrations.AddField(
            model_name='branch',
            name='review_link_2gis',
            field=models.URLField(blank=True, help_text='Ссылка на отзывы этой точки в 2ГИС. Подставляется кнопкой в ответ ТОЛЬКО на позитивные отзывы.', max_length=500, verbose_name='Ссылка 2ГИС'),
        ),
    ]
