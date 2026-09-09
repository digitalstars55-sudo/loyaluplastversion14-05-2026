"""
Предполагаемая точка ВК-отзыва (09.09.2026).

────────────────────────────────────────────────────────────────────────
Зачем
────────────────────────────────────────────────────────────────────────
ВК-группа у сети одна, поэтому тред сообщений гостя из группы создаётся
с `branch=None` (`handle_vk_incoming_message` → `_get_or_create_conversation(None, …)`).
Точки нет вообще — значит негатив из ВК не уходит в жалобы CheckUp
(`checkup_complaints.build_payload` возвращает None) и в вебе/мобилке
у карточки пусто вместо названия кафе.

Но последний скан гостя мы знаем: `QRScan` (какой QR, какой стол, когда)
и `ClientBranchVisit` (визит в точку). Типичный ВК-отзыв — это ответ на
авто-сообщение «всё ли понравилось?», которое уходит через 3 часа после
игры, то есть скан почти всегда рядом по времени.

────────────────────────────────────────────────────────────────────────
Правила (важно)
────────────────────────────────────────────────────────────────────────
• Это ПОДСКАЗКА, а не выбор гостя. Она живёт в отдельных полях
  `TestimonialConversation.inferred_*` и НИКОГДА не пишется в `branch`.
• Работает только при включённом пер-тенантном флаге
  `ClientConfig.vk_review_branch_inference` (по умолчанию ВЫКЛЮЧЕН —
  поведение прежнее).
• Только внутри окна `ClientConfig.vk_review_branch_inference_hours`
  (по умолчанию 24 ч). Ничего «около» окна не угадываем: старого скана
  недостаточно, чтобы отправить жалобу на конкретную точку.
• Ничего не роняем: приём ВК-сообщения важнее подсказки, любая ошибка —
  в лог и `False`.
• Не нашли свежий скан — старую подсказку НЕ стираем: у неё видна дата
  скана, по ней и понятно, насколько она устарела.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# Окно по умолчанию, если конфиг недоступен/пуст.
DEFAULT_WINDOW_HOURS = 24


@dataclass
class InferredPoint:
    """Найденная подсказка: точка, стол (если QR «Отзыв со стола»), когда и откуда."""

    branch: object
    table_number: int | None
    scan_at: datetime
    source: str  # 'qr_scan' | 'visit'


def inference_settings() -> tuple[bool, int]:
    """(включено ли, окно в часах) для текущего тенанта.

    Читаем `ClientConfig` так же, как `get_branch_info`. Конфига нет,
    схема public, БД молчит — считаем, что выключено.
    """
    try:
        from django.db import connection

        from apps.shared.config.models import ClientConfig

        tenant = getattr(connection, 'tenant', None)
        if tenant is None:
            return (False, DEFAULT_WINDOW_HOURS)
        cfg = ClientConfig.objects.get(company=tenant)
        hours = int(cfg.vk_review_branch_inference_hours or DEFAULT_WINDOW_HOURS)
        return (bool(cfg.vk_review_branch_inference), hours or DEFAULT_WINDOW_HOURS)
    except Exception:
        return (False, DEFAULT_WINDOW_HOURS)


def find_last_scan(vk_guest, at, window_hours: int) -> InferredPoint | None:
    """Последний скан QR гостя в окне (at - window; at]. Нет — последний визит.

    `vk_guest` — это `guest.Client` (public-схема), а `QRScan.client` /
    `ClientBranchVisit.client` — это `ClientBranch`, поэтому фильтр идёт
    через `client__client`.
    """
    from apps.tenant.branch.models import ClientBranchVisit, QRScan

    since = at - timedelta(hours=int(window_hours or DEFAULT_WINDOW_HOURS))

    scan = (QRScan.objects
            .filter(client__client=vk_guest, scanned_at__gt=since, scanned_at__lte=at)
            .select_related('qr__branch')
            .order_by('-scanned_at', '-id')
            .first())
    if scan is not None and scan.qr_id and scan.qr.branch_id:
        return InferredPoint(
            branch=scan.qr.branch,
            table_number=scan.qr.table_number,
            scan_at=scan.scanned_at,
            source='qr_scan',
        )

    # Скана «точки контакта» нет — берём обычный визит (там стола нет).
    visit = (ClientBranchVisit.objects
             .filter(client__client=vk_guest, visited_at__gt=since, visited_at__lte=at)
             .select_related('client__branch')
             .order_by('-visited_at', '-id')
             .first())
    if visit is not None and visit.client_id and visit.client.branch_id:
        return InferredPoint(
            branch=visit.client.branch,
            table_number=None,
            scan_at=visit.visited_at,
            source='visit',
        )

    return None


def apply_branch_inference(conv, at=None) -> bool:
    """Проставить треду подсказку о точке. True — подсказка обновлена.

    Ничего не делаем, если фича выключена, у треда уже есть настоящая точка
    или гость не опознан. Свежий скан не нашёлся — тред не трогаем вообще
    (старая подсказка остаётся, у неё видна дата скана).
    """
    try:
        enabled, window_hours = inference_settings()
        if not enabled:
            return False
        if getattr(conv, 'branch_id', None):
            return False
        if not getattr(conv, 'vk_guest_id', None):
            return False

        moment = at or timezone.now()
        point = find_last_scan(conv.vk_guest, moment, window_hours)
        if point is None:
            return False

        conv.inferred_branch = point.branch
        conv.inferred_table_number = point.table_number
        conv.inferred_scan_at = point.scan_at
        conv.inferred_source = point.source
        conv.inferred_at = timezone.now()
        conv.save(update_fields=[
            'inferred_branch', 'inferred_table_number', 'inferred_scan_at',
            'inferred_source', 'inferred_at',
        ])
        return True
    except Exception as e:
        # Подсказка — не повод потерять сообщение гостя.
        logger.warning(
            'подсказка точки: не удалось определить (conv=%s): %s',
            getattr(conv, 'pk', None), e,
        )
        return False


def inference_is_fresh(conv, at, window_hours: int) -> bool:
    """Годится ли подсказка на момент `at` (скан не старше окна)."""
    try:
        if not getattr(conv, 'inferred_branch_id', None):
            return False
        scan_at = getattr(conv, 'inferred_scan_at', None)
        if not scan_at or not at:
            return False
        return (at - scan_at) <= timedelta(hours=int(window_hours or DEFAULT_WINDOW_HOURS))
    except Exception:
        return False
