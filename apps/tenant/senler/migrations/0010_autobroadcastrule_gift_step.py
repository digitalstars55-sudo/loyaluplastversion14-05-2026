# Фаза 5 (RF/RFM-награды): подарочный шаг правила (ТЗ §8, §15.4) —
# тир каталога, срок сгорания и запасной текст M0. Аддитивно: у существующих
# правил все поля пустые, поведение не меняется.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('senler', '0009_rf_gift_expiring_event'),
    ]

    operations = [
        migrations.AddField(
            model_name='autobroadcastrule',
            name='gift_tier',
            field=models.CharField(blank=True, default='', help_text='Пусто — правило без подарка. Иначе G1, G2, G3 или список через запятую (G1,G2) — система сама выберет позицию каталога.', max_length=16, verbose_name='Категория подарка (тир)'),
        ),
        migrations.AddField(
            model_name='autobroadcastrule',
            name='gift_lifetime_days',
            field=models.PositiveSmallIntegerField(default=0, help_text='0 — срок позиции каталога («Срок действия по умолчанию»).', verbose_name='Срок сгорания подарка, дней'),
        ),
        migrations.AddField(
            model_name='autobroadcastrule',
            name='gift_fallback_text',
            field=models.TextField(blank=True, default='', help_text='Уходит, если подарок выдать нельзя. Переменные те же, кроме {подарок}/{дней_осталось}. Пусто — гость пропускается.', verbose_name='Запасной текст без подарка (M0)'),
        ),
    ]
