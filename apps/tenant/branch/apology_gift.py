"""
Подарок-извинение за низкую оценку (15.10.2026, волна 3 ТЗ маркетолога).

Гость оставил в мини-приложении отзыв с оценкой не выше `apology_gift_max_rating`
— LoyalUP сам кладёт ему подарок из каталога наград (категория
`apology_gift_tier`) и пишет в ВК «извините». В CheckUp по той же жалобе
управляющему уходит пуш «извиниться до ухода» (`apps/guests/apology.py`) — гость
получает и подарок, и живого человека.

Правила:
  • настройка сети `apology_gift_enabled` (по умолчанию выключено);
  • гость должен быть зарегистрирован на точке отзыва (иначе подарок некуда
    положить — он его не увидит);
  • не чаще одного подарка-извинения гостю за 30 дней (по всей сети);
  • подарок — тем же каталогом и лимитами, что RF-награды (себестоимость
    `rf_reward_max_cost_rub`, лимиты выдач позиции, без трёх последних);
  • сообщение в ВК — в тот же тред отзыва (`send_vk_reply`, отзыв остаётся
    неотвеченным для администратора). Не ушло (нет токена, гость закрыл
    сообщения) — подарок всё равно лежит в «Моих подарках».
Приём отзыва не роняется никогда: всё — в celery после коммита.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

log = logging.getLogger('loyalup.apology')

COOLDOWN_DAYS = 30
DEFAULT_TEXT = (
    '{name}, нам очень жаль, что сегодня всё прошло не так. Спасибо, что сказали честно — '
    'мы разберёмся. В знак извинения в приложении вас ждёт подарок: {gift}. '
    'Он действует {days} дн. Будем рады исправиться!'
)


def _config():
    from apps.tenant.senler.engine import _tenant_client_config
    return _tenant_client_config()


def should_offer(rating, config) -> bool:
    if not config or not getattr(config, 'apology_gift_enabled', False):
        return False
    if rating is None:
        return False
    try:
        return int(rating) <= int(getattr(config, 'apology_gift_max_rating', 3) or 3)
    except (TypeError, ValueError):
        return False


def schedule(message) -> None:
    """Из submit_app_review: поставить выдачу после коммита. Никогда не бросает."""
    try:
        if not should_offer(message.rating, _config()):
            return
        from django.db import connection, transaction
        from apps.tenant.branch.tasks import apology_gift_task
        schema = connection.schema_name
        transaction.on_commit(lambda: apology_gift_task.delay(message.pk, schema))
    except Exception:
        log.exception('apology: не удалось поставить задачу msg=%s', getattr(message, 'pk', None))


def _render(config, *, name: str, gift: str, days: int) -> str:
    template = (getattr(config, 'apology_gift_text', '') or '').strip() or DEFAULT_TEXT
    try:
        return template.format(name=name or 'Здравствуйте', gift=gift, days=days)
    except (KeyError, IndexError, ValueError):
        return DEFAULT_TEXT.format(name=name or 'Здравствуйте', gift=gift, days=days)


def grant(message_id: int) -> tuple[object | None, str]:
    """
    Выдать подарок-извинение за отзыв. (InventoryItem | None, причина).
    Причины отказа: off, rating, not_found, no_guest, cooldown, no_gift,
    unknown_gift_cost, limit_exhausted.
    """
    from apps.tenant.branch.models import ClientBranch, TestimonialMessage
    from apps.tenant.inventory import reward_catalog
    from apps.tenant.inventory.models import AcquisitionSource, InventoryItem
    from apps.tenant.senler.pilot_controls import limits

    config = _config()
    msg = (TestimonialMessage.objects
           .select_related('conversation', 'branch').filter(pk=message_id).first())
    if msg is None:
        return None, 'not_found'
    if not getattr(config, 'apology_gift_enabled', False):
        return None, 'off'
    if not should_offer(msg.rating, config):
        return None, 'rating'

    conv = msg.conversation
    branch = msg.branch or conv.branch
    try:
        vk_id = int(conv.vk_sender_id)
    except (TypeError, ValueError):
        return None, 'no_guest'
    cb = (ClientBranch.objects.select_related('client')
          .filter(branch=branch, client__vk_id=vk_id, client__is_active=True).first())
    if cb is None:
        return None, 'no_guest'

    since = timezone.now() - timedelta(days=COOLDOWN_DAYS)
    if InventoryItem.objects.filter(client_branch__client_id=cb.client_id,
                                    acquired_from=AcquisitionSource.APOLOGY,
                                    created_at__gte=since).exists():
        return None, 'cooldown'

    recent_ids = list(
        InventoryItem.objects.filter(client_branch__client_id=cb.client_id, catalog_item__isnull=False)
        .order_by('-created_at').values_list('catalog_item_id', flat=True)[:3]
    )
    cost_cap, _ = limits(config)
    options = {'max_cost_price': cost_cap} if cost_cap is not None else {}
    item = reward_catalog.pick_reward(getattr(config, 'apology_gift_tier', 'G1') or 'G1',
                                      branch, exclude_ids=recent_ids, **options)
    if item is None:
        return None, 'no_gift'
    if cost_cap is not None and item.effective_cost_price <= 0:
        return None, 'unknown_gift_cost'

    issued = reward_catalog.issue_to_guest(
        item, cb, source=AcquisitionSource.APOLOGY,
        description=f'Подарок-извинение за отзыв #{msg.pk} (оценка {msg.rating})',
    )
    if issued is None:
        return None, 'limit_exhausted'

    days = item.default_lifetime_days or 0
    text = _render(config, name=cb.client.first_name, gift=item.product.name, days=days)
    try:
        from apps.tenant.branch.api.services import send_vk_reply
        send_vk_reply(conv, text, sender_name='LoyalUP', mark_replied=False)
    except Exception as e:  # noqa: BLE001 — подарок уже лежит у гостя
        log.warning('apology: подарок выдан, сообщение не ушло msg=%s: %s', msg.pk, e)
    return issued, ''
