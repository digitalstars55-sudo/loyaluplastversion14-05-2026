# Автоподтверждение на негативные отзывы («спасибо, разберёмся»).
#
# АДДИТИВНО: мастер-флаг ReviewAutoReplyConfig.auto_ack_enabled = False по
# умолчанию, поэтому после миграции поведение прода не меняется. Поле
# TestimonialConversation.auto_send_kind пустое у всех существующих записей
# (= полный ответ ИИ). Data-migration нет.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0032_ai_draft_freshness'),
    ]

    operations = [
        migrations.AddField(
            model_name='reviewautoreplyconfig',
            name='auto_ack_enabled',
            field=models.BooleanField(
                default=False,
                help_text='Если на негативный/частично негативный отзыв никто не ответил за «окно», ИИ отправит короткое подтверждение «спасибо, разберёмся». Тред остаётся неотвеченным — ответить по существу всё равно нужно человеку.',
                verbose_name='Автоподтверждение на негатив',
            ),
        ),
        migrations.AddField(
            model_name='reviewautoreplyconfig',
            name='auto_ack_delay_minutes',
            field=models.PositiveSmallIntegerField(
                choices=[(5, '5 минут'), (15, '15 минут'), (30, '30 минут'), (60, '60 минут'), (120, '2 часа')],
                default=30,
                help_text='Сколько ждать ответа сотрудника, прежде чем ИИ отправит подтверждение. Пуш «ИИ напишет в HH:MM» приходит сразу — отменить можно из карточки.',
                verbose_name='Окно без ответа, мин',
            ),
        ),
        migrations.AddField(
            model_name='reviewautoreplyconfig',
            name='auto_ack_text',
            field=models.CharField(
                blank=True,
                default='Спасибо большое за обратную связь 🙏 Мы сейчас во всём разберёмся и обязательно вернёмся к вам с ответом.',
                help_text='Одна и та же фраза для всех негативных отзывов. Без обещаний скидок и конкретики — по существу ответит человек.',
                max_length=300,
                verbose_name='Текст подтверждения',
            ),
        ),
        migrations.AddField(
            model_name='testimonialconversation',
            name='auto_send_kind',
            field=models.CharField(
                blank=True,
                choices=[('reply', 'ответ ИИ (позитив)'), ('ack', 'подтверждение «разберёмся» (негатив)')],
                default='',
                help_text='Что именно запланировано/отправлено в auto_send_*: полный ответ ИИ на позитив или короткое автоподтверждение на негатив. Пусто у старых записей = ответ.',
                max_length=8,
                verbose_name='Тип автоответа',
            ),
        ),
    ]
