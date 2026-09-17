# №78: где гость дал номер (профиль, форма отзыва, …) — для сравнения мест.
# Shared-модель → `migrate_schemas --shared`; ADD COLUMN ... DEFAULT '' — метаданные.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('guest', '0005_client_phone'),
    ]

    operations = [
        migrations.AddField(
            model_name='client',
            name='phone_placement',
            field=models.CharField(
                blank=True,
                default='',
                help_text="Место в мини-аппе: 'profile', 'review', … — чтобы сравнивать, где гости соглашаются.",
                max_length=32,
                verbose_name='Где дал номер',
            ),
        ),
    ]
