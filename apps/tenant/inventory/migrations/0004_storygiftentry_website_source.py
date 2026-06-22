import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0023_branch_review_links_default'),
        ('inventory', '0003_storygiftentry'),
    ]

    operations = [
        migrations.AddField(
            model_name='storygiftentry',
            name='source',
            field=models.CharField(
                default='story',
                db_index=True,
                max_length=16,
                help_text='story — вход из сториз; website — вход по QR с сайта клиента.',
                verbose_name='Источник входа',
            ),
        ),
        migrations.AddField(
            model_name='storygiftentry',
            name='activated_branch',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='story_gifts_activated_here',
                to='branch.branch',
                help_text='Для website-подарка: точка, где забрали по её коду дня. Для сториз — пусто (забирают на точке входа).',
                verbose_name='Точка активации (сетевой подарок)',
            ),
        ),
        migrations.AlterField(
            model_name='storygiftentry',
            name='campaign_key',
            field=models.CharField(
                blank=True,
                default='',
                max_length=64,
                help_text='Для website — метка сайта (напр. tula), чтобы различать сайты в аналитике.',
                verbose_name='Метка источника/сайта',
            ),
        ),
    ]
