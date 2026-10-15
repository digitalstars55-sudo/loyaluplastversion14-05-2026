"""
«Удалить мои данные» (решение владельца 15.10.2026): гость сам просит удалить
свои данные — обезличиваем его во всех сетях и пишем строку журнала.

Что стирается:
  • карточка гостя (`guest.Client`): имя, фото, пол, телефон и согласие;
    VK ID заменяется условным (10**15 + id карточки) — история больше не
    связывается с живой страницей; привязки Телеграма удаляются;
  • в каждой сети: дата рождения у профилей точек, связь с iikoCard, тексты
    его отзывов и переписки (с телефоном и черновиками ИИ), VK ID в журналах
    рассылок;
  • общие: VK ID в «Открытиях» (discovery).
Что остаётся без имени: баллы, визиты, суммы — для отчётов сети.

Вернувшийся гость начнёт с чистого листа: его VK ID карточки больше нет.
"""

from __future__ import annotations

import hashlib
import logging

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)

УДАЛЕНО = '[удалено по просьбе гостя]'
_СДВИГ = 10 ** 15


def хэш_vk(vk_id: int) -> str:
    return hashlib.sha256(f'{settings.SECRET_KEY}:forget:{int(vk_id)}'.encode()).hexdigest()


def условный_vk(client) -> int:
    return _СДВИГ + int(client.pk)


def _в_сети(client, old_vk: int, new_vk: int) -> dict:
    """Обезличивание внутри текущей схемы. Нет таблицы в сети — пропуск."""
    from apps.tenant.branch.models import ClientBranch, TestimonialConversation, TestimonialMessage

    out = {}
    out['profiles'] = ClientBranch.objects.filter(client=client).update(
        birth_date=None, birth_date_set_at=None)

    convs = TestimonialConversation.objects.filter(
        models_q_client(client, old_vk))
    ids = list(convs.values_list('id', flat=True))
    out['messages'] = TestimonialMessage.objects.filter(conversation_id__in=ids).exclude(
        source=TestimonialMessage.Source.ADMIN_REPLY).update(text=УДАЛЕНО, phone='')
    out['conversations'] = TestimonialConversation.objects.filter(id__in=ids).update(
        vk_sender_id='', ai_comment='', ai_draft='')

    try:
        from apps.tenant.analytics.models import PosGuestLink
        out['pos_links'] = PosGuestLink.objects.filter(client=client).delete()[0]
    except Exception:
        logger.exception('forget: PosGuestLink')

    try:
        from apps.tenant.senler.models import AutoBroadcastLog, BroadcastRecipient
        out['broadcast_logs'] = (
            AutoBroadcastLog.objects.filter(vk_id=old_vk).update(vk_id=new_vk)
            + BroadcastRecipient.objects.filter(vk_id=old_vk).update(vk_id=new_vk))
    except Exception:
        logger.exception('forget: senler')
    return {k: v for k, v in out.items() if v}


def models_q_client(client, old_vk):
    from django.db.models import Q
    return Q(client__client=client) | Q(vk_guest=client) | Q(vk_sender_id=str(old_vk))


def забыть(client, via: str = '') -> dict:
    """Обезличить гостя везде. Возвращает счётчики по сетям."""
    from django_tenants.utils import get_tenant_model, schema_context

    from apps.shared.guest.models import GuestForgetLog, TelegramLink

    old_vk = int(client.vk_id)
    new_vk = условный_vk(client)
    if old_vk == new_vk:
        return {}
    counts: dict = {}

    for tenant in get_tenant_model().objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name), transaction.atomic():
                сеть = _в_сети(client, old_vk, new_vk)
            if сеть:
                counts[tenant.schema_name] = сеть
        except Exception:
            logger.exception('forget: сеть %s', tenant.schema_name)
            counts[tenant.schema_name] = {'error': True}

    with transaction.atomic():
        try:
            from apps.shared.discovery.models import DiscoveryClaim, DiscoveryEvent
            n = DiscoveryEvent.objects.filter(vk_id=old_vk).update(vk_id=new_vk)
            n += DiscoveryClaim.objects.filter(vk_id=old_vk).update(vk_id=new_vk)
            if n:
                counts['discovery'] = n
        except Exception:
            logger.exception('forget: discovery')
        counts['telegram_links'] = TelegramLink.objects.filter(client=client).delete()[0]

        client.vk_id = new_vk
        client.first_name = 'Гость'
        client.last_name = ''
        client.photo_url = ''
        client.gender = None
        for field in ('phone', 'phone_source', 'phone_placement'):
            if hasattr(client, field):
                setattr(client, field, '')
        for field in ('phone_consent_at', 'phone_reward_at'):
            if hasattr(client, field):
                setattr(client, field, None)
        if hasattr(client, 'is_active'):
            client.is_active = False
        client.save()

        GuestForgetLog.objects.create(
            vk_id_hash=хэш_vk(old_vk), client_pk=client.pk, via=via[:16], counts=counts)
    logger.warning('forget: гость #%s обезличен (%s)', client.pk, via)
    return counts
