# Архив правил авторассылок (контракт платформы №31): DELETE в API кабинета
# не удаляет правило — на нём история отправок и общий дедуп-лог.
#
# db_default=False обязателен: миграция тенантная (migrate_schemas), и между
# ALTER TABLE и рестартом web/celery старый код пишет INSERT без этой колонки —
# без значения на стороне БД такие вставки падают (урок 17.09).
# После выкладки: migrate_schemas + рестарт web и celery.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('senler', '0013_alter_autobroadcastlog_trigger_type_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='autobroadcastrule',
            name='is_archived',
            field=models.BooleanField(
                db_default=False,
                db_index=True,
                default=False,
                help_text='Архивное правило скрыто из списков и не отправляет сообщения. '
                          'История отправок сохраняется.',
                verbose_name='В архиве',
            ),
        ),
    ]
