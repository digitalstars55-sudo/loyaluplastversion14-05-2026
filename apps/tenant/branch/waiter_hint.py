"""Подсказка официанту при скане гостя (карточка 360, 18.10.2026).

Гость отсканировал QR на точке (засчитан визит) — CheckUp присылает пуш
официантам и администраторам, которые сейчас на смене этой точки: имя, какой
по счёту визит, аллергии и «нельзя», заметки зала, ждущий подарок и, если гость
о чём-то спрашивал и ему не ответили, — вопрос. Любимые блюда CheckUp
подставляет сам по номерам заказов iiko (`pos_orders`): блюда живут у него.

Включается флагом сети `ClientConfig.guest_waiter_hint_enabled` (по умолчанию
выключен). Отправка — очередью и по тому же секрету, что жалобы и стоп-лист
(`X-LoyalUP-Relay-Secret`). Сбой соседа — запись в журнал, гость ничего не ждёт.
Один гость на одной точке — не чаще раза в 3 часа (кэш).
"""
import logging

import requests
from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction

log = logging.getLogger(__name__)
TIMEOUT = 8
ПАУЗА = 3 * 3600


def _url() -> str:
    explicit = getattr(settings, 'CHECKUP_GUEST_ARRIVED_URL', '')
    if explicit:
        return explicit
    base = str(getattr(settings, 'CHECKUP_COMPLAINTS_URL', '')
               or 'https://checkupapp.ru/api/v1/loyalup/complaints/inbound/')
    return base.split('/api/v1/')[0] + '/api/v1/loyalup/guest-arrived/'


def включено() -> bool:
    cfg = getattr(getattr(connection, 'tenant', None), 'config', None)
    return bool(getattr(cfg, 'guest_waiter_hint_enabled', False)) if cfg is not None else False


def собрать(profile) -> dict:
    """Что знает о госте сеть — для пуша официанту. Без телефона и без vk-подписей."""
    from apps.tenant.branch.guest360 import (
        ждёт_ответа, заказы_iiko, заметки_гостя, подарки_сейчас,
    )
    from apps.tenant.branch.models import ClientBranch, ClientBranchVisit
    client = profile.client
    cbs = list(ClientBranch.objects.filter(client=client).select_related('branch'))
    cb_ids = [cb.pk for cb in cbs]
    заметки = заметки_гостя(client)
    ждёт = ждёт_ответа(client, cb_ids)
    return {
        'tenant': connection.schema_name,
        'point_id': str(profile.branch.branch_id),
        'vk_id': client.vk_id,
        'name': ' '.join(x for x in (client.first_name, client.last_name) if x).strip(),
        'gender': client.gender or '',
        'visit_number': ClientBranchVisit.objects.filter(client_id__in=cb_ids).count(),
        'is_employee': any(cb.is_employee for cb in cbs),
        'allergies': [z['text'] for z in заметки if z['kind'] == 'allergy'][:5],
        'notes': [z['text'] for z in заметки if z['kind'] == 'note'][:3],
        'gifts': подарки_сейчас(cb_ids)[:3],
        'question': (ждёт or {}).get('text', '')[:160],
        'pos_orders': заказы_iiko(client)[:40],
    }


def после_визита(profile) -> None:
    """Зовётся из регистрации гостя, когда визит засчитан. Ничего не роняет."""
    try:
        if not включено() or getattr(profile, 'is_employee', False):
            return
        ключ = f'g360:hint:{connection.schema_name}:{profile.client_id}:{profile.branch_id}'
        if not cache.add(ключ, 1, ПАУЗА):
            return
        schema, pk = connection.schema_name, profile.pk
        transaction.on_commit(lambda: send_guest_arrived.delay(schema, pk))
    except Exception:  # noqa: BLE001 — подсказка не повод ронять вход гостя
        log.warning('waiter hint: не поставилась в очередь', exc_info=True)


@shared_task(name='apps.tenant.branch.waiter_hint.send_guest_arrived', ignore_result=True)
def send_guest_arrived(schema: str, cb_id: int) -> dict:
    from django_tenants.utils import schema_context
    from apps.tenant.branch.models import ClientBranch
    secret = str(getattr(settings, 'LOYALUP_RELAY_SECRET', '') or '')
    if not secret:
        return {'skipped': 'no_secret'}
    with schema_context(schema):
        profile = ClientBranch.objects.select_related('client', 'branch').filter(pk=cb_id).first()
        if profile is None:
            return {'skipped': 'gone'}
        payload = собрать(profile)
    try:
        sess = requests.Session()
        sess.trust_env = False
        r = sess.post(_url(), json=payload, timeout=TIMEOUT,
                      headers={'X-LoyalUP-Relay-Secret': secret, 'Content-Type': 'application/json'})
    except Exception as e:  # noqa: BLE001
        log.warning('waiter hint: CheckUp не ответил (%s)', e)
        return {'ok': False}
    if r.status_code != 200:
        log.warning('waiter hint: CheckUp ответил %s: %s', r.status_code, r.text[:200])
        return {'ok': False, 'status': r.status_code}
    return {'ok': True, **(r.json() if r.content else {})}
