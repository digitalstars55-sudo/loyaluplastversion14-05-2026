from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0022_branch_review_links'),
    ]

    operations = [
        migrations.AddField(
            model_name='branch',
            name='review_links_default',
            field=models.BooleanField(default=False, help_text='Если у позитивного отзыва не определено кафе (общий VK-отзыв сети) — кнопка вставит ссылки ЭТОЙ точки. Отметьте одну точку сети.', verbose_name='Основная точка для ссылок'),
        ),
    ]
