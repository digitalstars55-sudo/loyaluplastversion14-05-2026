# №78: отметка «баллы за номер начислены» — награда один раз на гостя.
# Shared-модель → `migrate_schemas --shared`; ADD COLUMN NULL — метаданные.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('guest', '0006_client_phone_placement'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='phone_reward_at',
            field=models.DateTimeField(
                blank=True,
                help_text='Награда за первый номер выдаётся один раз на гостя; при отзыве согласия не отбирается.',
                null=True,
                verbose_name='Баллы за номер начислены',
            ),
        ),
    ]
