"""Архив гостя сети (17.10.2026).

Владелец: «если удаляем мы — он уходит в архив, если заново заходит — профиль
активируется с памятью, ничего не пропадает». Архив — отметка на всех профилях
гостя в этой сети (`ClientBranch.archived_at`), а не удаление: монеты, визиты,
подарки и история остаются. Архивный гость не виден в списке гостей, не
получает рассылок и не входит в аналитику «живых» — те же места, где уже
отсекаются сотрудники (`is_employee=False`).
"""
import logging

from django.utils import timezone

log = logging.getLogger(__name__)


def в_архив(client, кто: str = '') -> int:
    """Все профили гостя в сети — в архив. → сколько профилей отмечено."""
    from apps.tenant.branch.models import ClientBranch
    n = (ClientBranch.objects.filter(client=client, archived_at__isnull=True)
         .update(archived_at=timezone.now(), archived_by=(кто or '')[:150]))
    if n:
        log.info('guest archive: гость %s в архив (%s профилей), %s', client.pk, n, кто)
    return n


def из_архива(client) -> int:
    """Снять архив со всех профилей гостя (вход гостя или кнопка в кабинете)."""
    from apps.tenant.branch.models import ClientBranch
    n = (ClientBranch.objects.filter(client=client, archived_at__isnull=False)
         .update(archived_at=None, archived_by=''))
    if n:
        log.info('guest archive: гость %s вернулся из архива (%s профилей)', client.pk, n)
    return n
