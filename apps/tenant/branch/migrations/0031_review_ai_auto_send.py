# Автоотправка ответов ИИ на позитивные отзывы.
#
# Всё АДДИТИВНО: новые поля с дефолтами, ни одно существующее не меняется по
# смыслу. Мастер-флаг ReviewAutoReplyConfig.auto_send_enabled = False, поэтому
# сразу после миграции поведение прода остаётся прежним (черновик + ручной ответ).
# Data-migration нет.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('branch', '0030_alter_cointransaction_source'),
    ]

    operations = [
        # ── ReviewAutoReplyConfig: настройки автоотправки ──────────────────────
        migrations.AddField(
            model_name='reviewautoreplyconfig',
            name='auto_send_enabled',
            field=models.BooleanField(default=False, help_text='ИИ САМ отправляет гостю ответ на позитивный отзыв (без участия человека). Только POSITIVE, только если в отзыве нет вопроса/просьбы. Выключите — и всё вернётся к режиму «черновик, ответ вручную».', verbose_name='Автоотправка позитивных ответов'),
        ),
        migrations.AddField(
            model_name='reviewautoreplyconfig',
            name='auto_send_delay_minutes',
            field=models.PositiveSmallIntegerField(choices=[(5, '5 минут'), (15, '15 минут'), (30, '30 минут'), (60, '60 минут')], default=15, help_text='Сколько ждать перед отправкой — за это время сотрудник успеет отменить автоответ из пуша или из карточки отзыва.', verbose_name='Окно отмены, мин'),
        ),
        migrations.AddField(
            model_name='reviewautoreplyconfig',
            name='auto_send_attach_links',
            field=models.BooleanField(default=True, help_text='Прикреплять к автоответу кнопки со ссылками на отзывы точки (или основной точки сети, если кафе не определено).', verbose_name='Кнопки «Яндекс Карты»/«2ГИС»'),
        ),
        migrations.AddField(
            model_name='reviewautoreplyconfig',
            name='auto_send_links_text',
            field=models.CharField(blank=True, default='Будем очень рады вашему отзыву на Яндекс Картах или в 2ГИС — кнопки ниже 👇', help_text='Добавляется в конец автоответа ТОЛЬКО если кнопки включены и есть хотя бы одна ссылка.', max_length=200, verbose_name='Фраза перед кнопками'),
        ),
        migrations.AddField(
            model_name='reviewautoreplyconfig',
            name='auto_send_daily_limit',
            field=models.PositiveIntegerField(default=50, help_text='Предохранитель: больше этого числа автоответов за сутки (МСК) ИИ не отправит — остальные останутся черновиками.', verbose_name='Лимит автоответов в сутки'),
        ),
        migrations.AddField(
            model_name='reviewautoreplyconfig',
            name='auto_send_branch_enabled',
            field=models.JSONField(blank=True, default=dict, help_text='Карта branch_id (str) → bool. Отсутствующие точки наследуют мастер-флаг. ВК-отзывы без точки отправляются при включённом мастер-флаге.', verbose_name='Автоотправка по точкам'),
        ),

        # ── TestimonialConversation: состояние автоотправки ────────────────────
        migrations.AddField(
            model_name='testimonialconversation',
            name='ai_needs_human',
            field=models.BooleanField(default=False, help_text='ИИ определил, что в отзыве вопрос/просьба — автоответ отправлять нельзя.', verbose_name='Нужен человек'),
        ),
        migrations.AddField(
            model_name='testimonialconversation',
            name='auto_send_status',
            field=models.CharField(blank=True, choices=[('scheduled', 'Запланирован'), ('sent', 'Отправлен'), ('cancelled', 'Отменён'), ('skipped', 'Пропущен'), ('failed', 'Ошибка отправки')], db_index=True, default='', help_text='Пусто — автоответ не рассматривался. scheduled — запланирован, sent — отправлен, cancelled — отменён, skipped — не подошёл по условиям, failed — ошибка отправки.', max_length=12, verbose_name='Статус автоответа'),
        ),
        migrations.AddField(
            model_name='testimonialconversation',
            name='auto_send_at',
            field=models.DateTimeField(blank=True, help_text='Плановое время отправки (для scheduled) или фактическое (для sent).', null=True, verbose_name='Время автоответа'),
        ),
        migrations.AddField(
            model_name='testimonialconversation',
            name='auto_send_reason',
            field=models.CharField(blank=True, default='', help_text='Почему автоответ пропущен/отменён/упал: manual_reply, needs_human, numeric_only, daily_limit, vk_error: …', max_length=120, verbose_name='Причина'),
        ),
        migrations.AddField(
            model_name='testimonialconversation',
            name='auto_send_draft_hash',
            field=models.CharField(blank=True, default='', help_text='sha256 черновика на момент планирования. Если черновик изменили — автоотправка отменяется.', max_length=64, verbose_name='Хеш черновика'),
        ),

        # ── TestimonialMessage: пометка «отправил ИИ» ──────────────────────────
        migrations.AddField(
            model_name='testimonialmessage',
            name='is_ai_generated',
            field=models.BooleanField(default=False, help_text='Ответ отправлен автоматически (автоотправка ИИ), а не сотрудником.', verbose_name='Отправлено ИИ'),
        ),

        # ── AuditLog: два новых типа действия (только choices) ─────────────────
        migrations.AlterField(
            model_name='auditlog',
            name='action_type',
            field=models.CharField(
                choices=[
                    ('COIN_ADJUST', 'Корректировка баланса'),
                    ('REVIEW_REPLY', 'Ответ на отзыв'),
                    ('REVIEW_RESOLVE', 'Закрытие отзыва'),
                    ('BROADCAST_SEND', 'Рассылка'),
                    ('PRODUCT_CREATE', 'Создан подарок'),
                    ('PRODUCT_UPDATE', 'Изменён подарок'),
                    ('PRODUCT_DELETE', 'Удалён подарок'),
                    ('QUEST_CREATE', 'Создан квест'),
                    ('QUEST_UPDATE', 'Изменён квест'),
                    ('QUEST_DELETE', 'Удалён квест'),
                    ('PROMO_CREATE', 'Создана акция'),
                    ('PROMO_UPDATE', 'Изменена акция'),
                    ('PROMO_DELETE', 'Удалена акция'),
                    ('STAFF_INVITE', 'Приглашён сотрудник'),
                    ('STAFF_TOGGLE', 'Изменён статус сотрудника'),
                    ('STAFF_PERMS', 'Изменены права'),
                    ('THRESHOLDS_SAVE', 'Сохранены пороги RF'),
                    ('AUTO_REPLY_SAVE', 'Изменены настройки AI-ответов'),
                    ('AUTO_REPLY_SENT', 'ИИ отправил ответ гостю'),
                    ('AUTO_REPLY_CANCEL', 'Отменён автоответ ИИ'),
                    ('DAILY_CODE_MANUAL', 'Ручной код дня'),
                    ('AUTH_LOGIN', 'Вход'),
                    ('AUTH_LOGOUT', 'Выход'),
                ],
                db_index=True, max_length=32, verbose_name='Действие',
            ),
        ),
    ]
