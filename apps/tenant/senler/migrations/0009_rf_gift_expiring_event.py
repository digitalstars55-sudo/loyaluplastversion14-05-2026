# Фаза 5 (RF/RFM-награды): новое событие конструктора rf_gift_expiring —
# напоминание про неактивированный RF/RFM-подарок за delay_days дней до
# сгорания. Только choices, схема БД не меняется.

from django.db import migrations, models

_CHOICES = [
    ('birthday_7d', 'За 7 дней до дня рождения'),
    ('birthday_1d', 'За 1 день до дня рождения'),
    ('birthday', 'День рождения'),
    ('after_game_3h', 'Через 3 часа после игры'),
    ('gift_not_claimed', 'Подарок из сториз/сайта не забран'),
    ('no_visit_days', 'Не приходил N дней (реактивация)'),
    ('subscribed_days', 'Подписался N дней назад (welcome)'),
    ('follow_up', 'Догоняющее (не отреагировал на другое правило)'),
    ('rf_gift_expiring', 'RF-подарок скоро сгорит (напоминание)'),
]


class Migration(migrations.Migration):

    dependencies = [
        ('senler', '0008_autobroadcastrule_follow_up_condition_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='autobroadcastlog',
            name='trigger_type',
            field=models.CharField(choices=_CHOICES, db_index=True, max_length=32, verbose_name='Триггер'),
        ),
        migrations.AlterField(
            model_name='autobroadcastrule',
            name='event',
            field=models.CharField(choices=_CHOICES, db_index=True, help_text='Что должно произойти с гостем, чтобы ему ушло это сообщение.', max_length=32, verbose_name='Событие (триггер)'),
        ),
        migrations.AlterField(
            model_name='autobroadcasttemplate',
            name='type',
            field=models.CharField(choices=_CHOICES, max_length=32, unique=True, verbose_name='Триггер'),
        ),
    ]
