from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0012_clientconfig_vk_review_branch_inference'),
    ]

    operations = [
        migrations.AddField(
            model_name='clientconfig',
            name='admin_brand_enabled',
            field=models.BooleanField(
                default=False,
                help_text='Веб-админка этой сети (шапка, кнопки, сайдбар, фильтры) окрашивается в главный и акцентный цвета бренда, в шапке — логотип сети. Выключено — прежний фиолетовый вид ЛоялUP.',
                verbose_name='Красить админку в цвета бренда',
            ),
        ),
    ]
