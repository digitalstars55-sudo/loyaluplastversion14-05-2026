"""
Migration: убирает FK от tenant-моделей (StaffProfile, AuditLog,
SupportChatMessage) к shared.User. Заменяет на plain BigIntegerField.

ПРИЧИНА: Django collector при User.delete() пытается обойти все reverse-FK,
включая tenant-таблицы — и падает на "relation branch_staffprofile does not exist"
когда удаление инициировано из public-схемы (admin, shell, миграции).
Стандартный django-tenants паттерн — не FK, а plain integer.

Существующие данные в трёх таблицах сбрасываются (на проде там 4–5 записей
от smoke-тестов — приемлемая потеря).
"""
from django.db import migrations, models


def _truncate(apps, schema_editor):
    """Чистим старые записи перед сменой схемы FK."""
    StaffProfile = apps.get_model('branch', 'StaffProfile')
    AuditLog = apps.get_model('branch', 'AuditLog')
    SupportChatMessage = apps.get_model('branch', 'SupportChatMessage')
    StaffProfile.objects.all().delete()
    AuditLog.objects.all().delete()
    SupportChatMessage.objects.all().delete()


class Migration(migrations.Migration):

    # ALTER TABLE drop-FK не может стартовать в той же транзакции, что и
    # cascade-DELETE из RunPython — Postgres ругается "pending trigger events".
    # Поэтому каждая операция получает свою транзакцию.
    atomic = False

    dependencies = [
        ('branch', '0011_phase2_polish'),
    ]

    operations = [
        migrations.RunPython(_truncate, reverse_code=migrations.RunPython.noop),

        # ── StaffProfile.user (OneToOneField CASCADE) → user_id PositiveBigIntegerField unique ──
        migrations.RemoveField(model_name='staffprofile', name='user'),
        migrations.AddField(
            model_name='staffprofile',
            name='user_id',
            field=models.PositiveBigIntegerField(
                default=0,
                help_text='Логический ID User в shared-схеме. Не FK — иначе ломается удаление User.',
                unique=True,
                verbose_name='ID пользователя',
            ),
            preserve_default=False,
        ),

        # ── AuditLog.staff (FK SET_NULL) → staff_id PositiveBigIntegerField nullable ──
        migrations.RemoveField(model_name='auditlog', name='staff'),
        migrations.AddField(
            model_name='auditlog',
            name='staff_id',
            field=models.PositiveBigIntegerField(
                blank=True,
                db_index=True,
                help_text='Логический ID User в shared-схеме. Не FK — иначе ломается удаление User из public.',
                null=True,
                verbose_name='ID сотрудника',
            ),
        ),
        # Восстановим индекс по новому полю
        migrations.RemoveIndex(model_name='auditlog', name='audit_staff_created_idx'),
        migrations.AddIndex(
            model_name='auditlog',
            index=models.Index(fields=['staff_id', '-created_at'], name='audit_staff_created_idx'),
        ),

        # ── SupportChatMessage.author (FK SET_NULL) → author_id PositiveBigIntegerField nullable ──
        migrations.RemoveField(model_name='supportchatmessage', name='author'),
        migrations.AddField(
            model_name='supportchatmessage',
            name='author_id',
            field=models.PositiveBigIntegerField(
                blank=True,
                db_index=True,
                help_text='Логический ID User в shared-схеме. Не FK — иначе ломается удаление User.',
                null=True,
                verbose_name='ID автора (если внутри платформы)',
            ),
        ),
    ]
