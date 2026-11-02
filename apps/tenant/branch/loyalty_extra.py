"""Этап 1 (02.11.2026): приглашение друга и уровни гостей.

Друг: ссылка `/go/?branch=&from=<профиль>&is_referral=1` уже записывает `invited_by`. Монеты
обоим — один раз, по первому подтверждённому заказу (чек или карта на кассе), а не по
переходу: иначе награду легко накрутить. Старый гость, открывший чужую ссылку, наград не даёт.

Уровень: визиты гостя по сети за 365 дней. Бонус — отдельной строкой монет к заказу.
"""
import logging
from datetime import timedelta

from django.db import connection, transaction
from django.utils import timezone

from .models import (
    ClientBranch, ClientBranchVisit, CoinTransaction, ReferralReward, TransactionSource, TransactionType,
)

log = logging.getLogger(__name__)

ЗАКАЗ = (TransactionSource.RECEIPT, TransactionSource.POS)
НОВЫЙ_ГОСТЬ_ДНЕЙ = 30
УРОВНИ = [
    {'name': 'Гость', 'visits': 0, 'bonus_pct': 0},
    {'name': 'Свой', 'visits': 5, 'bonus_pct': 5},
    {'name': 'Постоянный', 'visits': 15, 'bonus_pct': 10},
]


def _конфиг():
    tenant = getattr(connection, 'tenant', None)
    return getattr(tenant, 'config', None) if tenant is not None else None


def уровни(cfg) -> list:
    чистые = []
    for с in getattr(cfg, 'levels', None) or []:
        try:
            чистые.append({'name': str(с['name'])[:30], 'visits': max(0, int(с['visits'])),
                           'bonus_pct': min(100, max(0, int(с.get('bonus_pct') or 0)))})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(чистые or УРОВНИ, key=lambda с: с['visits'])


def визитов(guest_id: int) -> int:
    return ClientBranchVisit.objects.filter(
        client__client_id=guest_id, visited_at__gte=timezone.now() - timedelta(days=365)).count()


def уровень_гостя(guest_id: int, cfg) -> dict:
    сетка = уровни(cfg)
    n = визитов(guest_id)
    текущий = сетка[0]
    следующий = None
    for с in сетка:
        if n >= с['visits']:
            текущий = с
        else:
            следующий = с
            break
    return {'name': текущий['name'], 'bonus_pct': текущий['bonus_pct'], 'visits': n,
            'next_name': следующий['name'] if следующий else '',
            'next_left': следующий['visits'] - n if следующий else 0}


def _монеты(профиль, сумма, источник, текст):
    CoinTransaction.objects.create_transfer(
        профиль, сумма, TransactionType.INCOME, источник, текст[:255])


def наградить_за_друга(профиль, cfg, когда=None) -> bool:
    """Обе стороны — одной транзакцией базы; повтор отсекает OneToOne у друга."""
    пригласил = профиль.invited_by
    if пригласил is None or пригласил.pk == профиль.pk or пригласил.client_id == профиль.client_id:
        return False
    if профиль.is_employee or пригласил.is_employee:
        return False
    if (когда or timezone.now()) - профиль.created_at > timedelta(days=НОВЫЙ_ГОСТЬ_ДНЕЙ):
        return False  # старый гость: ссылка его не «привела»
    другу = int(getattr(cfg, 'referral_friend_coins', 0) or 0)
    ему = int(getattr(cfg, 'referral_inviter_coins', 0) or 0)
    with transaction.atomic():
        _, новая = ReferralReward.objects.get_or_create(
            friend=профиль, defaults={'inviter': пригласил, 'friend_coins': другу, 'inviter_coins': ему})
        if not новая:
            return False
        if другу:
            _монеты(профиль, другу, TransactionSource.REFERRAL, 'Первый заказ по приглашению')
        if ему:
            _монеты(пригласил, ему, TransactionSource.REFERRAL, 'Друг сделал первый заказ')
    return True


def после_заказа(tx) -> None:
    if tx.type != TransactionType.INCOME or tx.source not in ЗАКАЗ:
        return
    cfg = _конфиг()
    if cfg is None:
        return
    профиль = tx.client
    if getattr(cfg, 'referral_enabled', False) and профиль.invited_by_id:
        наградить_за_друга(профиль, cfg, tx.created_at)
    if getattr(cfg, 'levels_enabled', False):
        процент = уровень_гостя(профиль.client_id, cfg)['bonus_pct']
        бонус = tx.amount * процент // 100
        if бонус:
            _монеты(профиль, бонус, TransactionSource.LEVEL, f'Бонус уровня {процент}%')


def сводка_друзей(профиль, cfg) -> dict:
    ссылка = f'/go/?branch={профиль.branch.branch_id}&from={профиль.pk}&is_referral=1'
    return {
        'path': ссылка,
        'inviter_coins': int(getattr(cfg, 'referral_inviter_coins', 0) or 0),
        'friend_coins': int(getattr(cfg, 'referral_friend_coins', 0) or 0),
        'invited': ClientBranch.objects.filter(invited_by=профиль).count(),
        'rewarded': ReferralReward.objects.filter(inviter=профиль).count(),
    }
