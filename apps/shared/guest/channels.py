"""Каналы гостя: записать, показать (склейка гостя, шаг А — 17.10.2026).

Шаг А только собирает каналы в одну карточку и ничего не склеивает сам:
  · ВК — у каждого гостя с настоящим VK ID (доказан подписью запуска или VK ID);
  · Телеграм — из привязки `TelegramLink` (initData подписана ботом);
  · телефон — номер с согласием; доказан, если подпись ВК сошлась.
Склейка двух карточек (тот же Телеграм или тот же доказанный номер у другого
гостя) — шаг Б; здесь такой случай только пишется в журнал.
"""
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

ПОРЯДОК = {'tg': 0, 'vk': 1, 'max': 2, 'phone': 3}
#: Служебные номера карточек гостей без ВК — выше любых настоящих VK ID.
СЛУЖЕБНЫЙ_VK_ОТ = 9_000_000_000_000


def отметить(client, kind, external_id, *, username='', verified=True, can_message=None, now=None):
    """Записать (или освежить) канал гостя. Чужой канал не перехватывает. → GuestChannel | None."""
    from .models import GuestChannel

    if client is None or not external_id:
        return None
    now = now or timezone.now()
    ext = str(external_id)[:64]
    try:
        with transaction.atomic():
            ch, создан = GuestChannel.objects.get_or_create(
                kind=kind, external_id=ext,
                defaults={'client': client, 'username': username[:128], 'verified': verified,
                          'can_message': can_message, 'first_seen': now, 'last_seen': now})
    except IntegrityError:
        ch, создан = GuestChannel.objects.get(kind=kind, external_id=ext), False
    if ch.client_id != client.pk:
        # Тот же канал у другой карточки — кандидат на склейку (шаг Б).
        logger.warning('guest.channels: %s:%s уже у гостя %s, пришёл гость %s — нужна склейка',
                       kind, ext, ch.client_id, client.pk)
        return None
    if not создан:
        поля = {'last_seen': now}
        if username and ch.username != username[:128]:
            поля['username'] = username[:128]
        if verified and not ch.verified:
            поля['verified'] = True
        if can_message is not None and ch.can_message != can_message:
            поля['can_message'] = can_message
        GuestChannel.objects.filter(pk=ch.pk).update(**поля)
    return ch


def убрать(client, kind, external_id=None):
    from .models import GuestChannel
    qs = GuestChannel.objects.filter(client=client, kind=kind)
    if external_id is not None:
        qs = qs.filter(external_id=str(external_id))
    qs.delete()


def каналы(client):
    """Каналы гостя для карточки: [{'kind','label','external_id','username','verified','can_message','last_seen'}]."""
    from .models import GuestChannel
    if client is None:
        return []
    out = []
    for ch in GuestChannel.objects.filter(client=client):
        out.append({'kind': ch.kind, 'label': ch.get_kind_display(), 'external_id': ch.external_id,
                    'username': ch.username, 'verified': ch.verified, 'can_message': ch.can_message,
                    'last_seen': ch.last_seen.isoformat() if ch.last_seen else None})
    out.sort(key=lambda x: (ПОРЯДОК.get(x['kind'], 9), x['last_seen'] or ''))
    return out
