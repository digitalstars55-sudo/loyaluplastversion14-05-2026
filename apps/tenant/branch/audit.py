"""
Хук для записи действий в AuditLog (тенант-специфичный).

Не падает, если что-то пошло не так — не блокируем основную бизнес-логику
из-за вспомогательного журнала.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def log_audit(
    user,
    action_type: str,
    *,
    target_type: str = '',
    target_id: int | str | None = None,
    target_label: str = '',
    details: str = '',
    delta: dict | None = None,
) -> None:
    """Записать действие сотрудника в журнал."""
    from apps.tenant.branch.models import AuditLog

    if user is None or not getattr(user, 'is_authenticated', False):
        return

    try:
        AuditLog.objects.create(
            staff_id=user.pk,
            staff_name=(user.get_full_name() or user.username or '')[:255],
            action_type=action_type,
            target_type=target_type or '',
            target_id=str(target_id) if target_id not in (None, '') else '',
            target_label=(target_label or '')[:255],
            details=details or '',
            delta=delta or {},
        )
    except Exception:
        logger.exception('AuditLog.create failed (action=%s)', action_type)
