"""Автопоздравления с поводами гостя (карточка 360, 18.10.2026).

Утром в день повода — «год с первого визита» и даты из заметок зала
(«годовщина свадьбы 18.11») — гостю уходит поздравление тем же путём, что и
рассылки (`send_vk_message`: ВК, а гостю без ВК — Телеграм). ДР не трогаем:
его поздравляет прежняя автоверсия (`AutoBroadcastTemplate birthday*`).

Включается флагом сети `ClientConfig.guest_occasions_auto_enabled` (выкл по
умолчанию). Повтор не задваивает: поздравление пишется заметкой вида
«greeted» с ключом «повод · год», и второй раз за год не уходит.
"""
import logging
from datetime import date

from django.db import connection
from django.utils import timezone

log = logging.getLogger(__name__)
ТЕКСТ = ('{name}, сегодня {occasion}! Поздравляем и будем рады видеть вас у нас — '
         'загляните, отметим вместе.')


def _в_этот_день(d: date, сегодня: date) -> bool:
    if d.month == 2 and d.day == 29 and not (сегодня.year % 4 == 0 and (сегодня.year % 100 != 0 or сегодня.year % 400 == 0)):
        return сегодня.month == 3 and сегодня.day == 1
    return (d.month, d.day) == (сегодня.month, сегодня.day)


def поводы_сегодня(сегодня=None):
    """[(client, cb, повод_словами, ключ)] — кого поздравлять сегодня в этой сети."""
    from apps.tenant.branch.models import ClientBranch, GuestNote
    сегодня = сегодня or timezone.localdate()
    out = []
    for n in (GuestNote.objects.filter(kind=GuestNote.Kind.OCCASION, is_active=True, occasion_date__isnull=False)
              .select_related('client')):
        if _в_этот_день(n.occasion_date, сегодня):
            cb = ClientBranch.objects.filter(client=n.client).select_related('branch').order_by('-created_at').first()
            if cb:
                out.append((n.client, cb, n.text.strip().rstrip('.').lower(), f'note:{n.pk}:{сегодня.year}'))
    год_назад = [сегодня.replace(year=сегодня.year - k) for k in range(1, 6) if not (сегодня.month == 2 and сегодня.day == 29)]
    for первый in год_назад:
        for cb in (ClientBranch.objects.filter(created_at__date=первый, is_employee=False)
                   .select_related('client', 'branch')):
            if ClientBranch.objects.filter(client=cb.client, created_at__lt=cb.created_at).exists():
                continue                 # первым был визит в другой точке сети — не тот день
            лет = сегодня.year - первый.year
            повод = 'год, как вы с нами' if лет == 1 else f'{лет} года, как вы с нами' if лет < 5 else f'{лет} лет, как вы с нами'
            out.append((cb.client, cb, повод, f'anniversary:{сегодня.year}'))
    return out


def поздравить_сегодня(сегодня=None, *, send=None) -> dict:
    from apps.tenant.branch.models import GuestNote
    from apps.tenant.senler.services import send_vk_message
    cfg = getattr(getattr(connection, 'tenant', None), 'config', None)
    if cfg is None or not getattr(cfg, 'guest_occasions_auto_enabled', False):
        return {'skipped': 'off'}
    send = send or send_vk_message
    шаблон = (getattr(cfg, 'guest_occasion_text', '') or '').strip() or ТЕКСТ
    итог = {'sent': 0, 'failed': 0, 'already': 0}
    for client, cb, повод, ключ in поводы_сегодня(сегодня):
        if getattr(client, 'merged_into_id', None) or not client.is_active:
            continue
        if GuestNote.objects.filter(client=client, kind=GuestNote.Kind.GREETED, author_ref=ключ).exists():
            итог['already'] += 1
            continue
        config = getattr(cb.branch, 'senler_config', None)
        if config is None:
            continue
        текст = шаблон.replace('{name}', client.first_name or 'Здравствуйте').replace('{occasion}', повод)
        ok, err, _ = send(config, client.vk_id, текст)
        GuestNote.objects.create(client=client, branch=cb.branch, kind=GuestNote.Kind.GREETED,
                                 text=(повод if ok else f'{повод} (не ушло: {err})')[:500],
                                 author_name='Автопоздравление', author_ref=ключ)
        итог['sent' if ok else 'failed'] += 1
    return итог
