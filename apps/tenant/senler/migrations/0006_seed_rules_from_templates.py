"""
Переносит существующие AutoBroadcastTemplate → AutoBroadcastRule.

⚠️ Правила создаются ВЫКЛЮЧЕННЫМИ (is_active=False) НАМЕРЕННО.
Legacy-задачи продолжают слать как слали; выключенные правила не шлют ничего.
Включаем правила только после сверки на проде (dry-run покажет, что движок
выбирает ровно тех же получателей, что и старая задача) — и одновременно
снимаем legacy-задачи с расписания.

Идемпотентно: если правило для события уже заведено — не дублируем.
"""
from django.db import migrations


def seed_rules(apps, schema_editor):
    Template = apps.get_model('senler', 'AutoBroadcastTemplate')
    Rule = apps.get_model('senler', 'AutoBroadcastRule')

    for t in Template.objects.all():
        if Rule.objects.filter(event=t.type).exists():
            continue
        Rule.objects.create(
            name=f'{t.get_type_display()} (перенесено из шаблона)',
            event=t.type,
            message_text=t.message_text,
            image=t.image.name if t.image else '',
            # Не наследуем is_active — правило стартует ВЫКЛЮЧЕННЫМ.
            is_active=False,
            send_hour_start=9,
            send_hour_end=21,
            priority=0,
        )


def unseed_rules(apps, schema_editor):
    """Откат: сносим только перенесённые (по префиксу имени), созданные руками — нет."""
    Rule = apps.get_model('senler', 'AutoBroadcastRule')
    Rule.objects.filter(name__endswith='(перенесено из шаблона)').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('senler', '0005_autobroadcastlog_entity_key_autobroadcastrule_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_rules, unseed_rules),
    ]
