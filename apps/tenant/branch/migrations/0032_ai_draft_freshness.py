# Актуальность AI-черновика: перегенерация при новых сообщениях гостя.
#
# АДДИТИВНО: два новых поля с NULL/0, существующие данные и поведение не
# меняются, пока не придёт новое сообщение гостя в тред с черновиком.
# Data-migration нет: у старых черновиков ai_draft_message_id = NULL, их
# актуальность до первой перегенерации считается по updated_at.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0031_review_ai_auto_send'),
    ]

    operations = [
        migrations.AddField(
            model_name='testimonialconversation',
            name='ai_draft_message_id',
            field=models.BigIntegerField(
                blank=True, null=True,
                help_text='id последнего гостевого TestimonialMessage на момент генерации черновика. '
                          'Пусто у старых черновиков — тогда актуальность считается по updated_at.',
                verbose_name='Черновик сгенерирован по сообщению',
            ),
        ),
        migrations.AddField(
            model_name='testimonialconversation',
            name='ai_draft_auto_generations',
            field=models.PositiveSmallIntegerField(
                default=0,
                help_text='Сколько раз ИИ сам генерировал черновик в этом треде (ручная «Перегенерировать» не считается). '
                          'Предохранитель от расхода токенов: после лимита — только вручную.',
                verbose_name='Автогенераций черновика',
            ),
        ),
    ]
