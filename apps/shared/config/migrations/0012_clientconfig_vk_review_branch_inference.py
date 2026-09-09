# Флаг «точка ВК-отзыва по последнему скану» (09.09.2026). Дефолт выключено —
# прод неотличим от эталона, включается по тенанту.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0011_clientconfig_entry_flags_stage1'),
    ]

    operations = [
        migrations.AddField(
            model_name='clientconfig',
            name='vk_review_branch_inference',
            field=models.BooleanField(
                default=False,
                verbose_name='Точка ВК-отзыва по последнему скану',
                help_text='Для сообщений гостя в ВК-группу подставлять точку (и стол) последнего скана QR в окне ниже. Негатив с такой точкой уходит в жалобы CheckUp с пометкой «по скану». По умолчанию выключено.',
            ),
        ),
        migrations.AddField(
            model_name='clientconfig',
            name='vk_review_branch_inference_hours',
            field=models.PositiveSmallIntegerField(
                default=24,
                verbose_name='Окно поиска скана, часов',
                help_text='Насколько старый скан ещё считаем относящимся к сообщению. По умолчанию 24.',
            ),
        ),
    ]
