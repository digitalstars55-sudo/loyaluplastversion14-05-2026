"""
Единообразное представление объектов рассылок для внешнего кабинета.

Зачем модуль: кабинет CheckUp — внешний потребитель, ему нужен стабильный
JSON без сюрпризов. Поэтому форма ответа собирается в одном месте, а не в
каждой вьюхе. Сознательно взяты обычные функции вместо DRF-сериализаторов:
модели тенантные, а тесты работают на моках (в тестовой БД таблиц нет) —
функции проверяются без ORM.

Инварианты: все даты/датавремя — ISO-строки или null; текст рассылки в
списках режется до 200 символов (кабинет показывает превью, а не письмо).
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

PREVIEW_TEXT_LIMIT = 200

# Окно VK для messages.edit / messages.delete — 24 часа с момента отправки
# (см. senler.services._VK_EDIT_WINDOW_HOURS). После него аварийные кнопки
# бессмысленны, кабинет их прячет.
VK_EDIT_WINDOW_HOURS = 24


def iso(value):
    """datetime/date → ISO-строка, None → None."""
    return value.isoformat() if value else None


def draft_to_dict(draft) -> dict:
    """Карточка черновика. image всегда null — картинки в v1 не поддержаны."""
    segment = draft.segment if draft.segment_id else None
    return {
        'id':            draft.pk,
        'name':          draft.name or '',
        'message_text':  draft.message_text or '',
        'image':         None,
        'mode':          draft.mode,
        'segment':       segment_to_dict(segment),
        'r_score':       draft.r_score,
        'f_score':       draft.f_score,
        'period':        {'start': iso(draft.start), 'end': iso(draft.end)},
        'branch_ids':    list(draft.branch_ids or []),
        'gender_filter': draft.gender_filter,
        'variants':      list(draft.variants or []),
        'status':        draft.status,
        'created_by':    draft.created_by or '',
        'sent_at':       iso(draft.sent_at),
        'last_send_id':  draft.last_send_id,
        'created_at':    iso(draft.created_at),
        'updated_at':    iso(draft.updated_at),
    }


def segment_to_dict(segment) -> dict | None:
    if segment is None:
        return None
    return {
        'id':    segment.pk,
        'code':  segment.code,
        'name':  segment.name,
        'emoji': getattr(segment, 'emoji', '') or '',
    }


def send_to_dict(send) -> dict:
    """
    Строка истории запусков.

    read_count приходит из annotate() в списке; для одиночного объекта
    (после cancel/edit/delete) его нет — отдаём 0, кабинет обновляет список.
    """
    broadcast = send.broadcast if send.broadcast_id else None
    branch = broadcast.branch if broadcast is not None else None
    text = (broadcast.message_text if broadcast is not None else '') or ''
    fresh = bool(
        send.finished_at
        and send.finished_at >= timezone.now() - timedelta(hours=VK_EDIT_WINDOW_HOURS)
    )
    manageable = bool(send.status == 'done' and send.sent_count > 0 and fresh)
    return {
        'id':               send.pk,
        'status':           send.status,
        'branch_id':        branch.pk if branch is not None else None,
        'branch_name':      branch.name if branch is not None else '',
        'name':             (broadcast.name if broadcast is not None else '') or '',
        'message_text':     text[:PREVIEW_TEXT_LIMIT],
        'trigger_type':     send.trigger_type,
        'triggered_by':     send.triggered_by or '',
        'started_at':       iso(send.started_at),
        'finished_at':      iso(send.finished_at),
        'recipients_count': send.recipients_count,
        'sent_count':       send.sent_count,
        'failed_count':     send.failed_count,
        'skipped_count':    send.skipped_count,
        'read_count':       getattr(send, 'read_count', 0) or 0,
        'created_at':       iso(send.created_at),
        'can_cancel':       send.status in ('pending', 'running'),
        'can_edit_in_vk':   manageable,
        'can_delete_in_vk': manageable,
    }
