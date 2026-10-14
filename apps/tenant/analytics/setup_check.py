"""
«Чекап» сети: здорово ли подключены блоки LoyalUP (ТЗ маркетолога п.10, 14.10.2026).

Каждый блок — {key, title, status, text, hint}:
  ok   — работает;
  warn — включено, но что-то не так (подсказка, что поправить);
  off  — не включено (это не ошибка, но и пользы нет).
Только чтение текущей схемы; ничего не меняет.
"""
from __future__ import annotations

from datetime import timedelta

OK, WARN, OFF = 'ok', 'warn', 'off'


def block(key, title, status, text, hint=''):
    return {'key': key, 'title': title, 'status': status, 'text': text, 'hint': hint}


def _pct(part, total):
    return f'{round(part * 100 / total)}%' if total else '—'


def build(config, now=None) -> dict:
    from django.conf import settings
    from django.db.models import Max
    from django.utils import timezone

    from apps.shared.guest.models import Client
    from apps.tenant.analytics.models import GuestRFScore, PosGuestLink, PosGuestLinkStatus
    from apps.tenant.branch.models import Branch, ClientBranchVisit, ReceiptScan, ReceiptScanStatus
    from apps.tenant.senler.models import AutoBroadcastRule, BroadcastRecipient, RecipientStatus, SenlerConfig

    now = now or timezone.now()
    week = now - timedelta(days=7)
    branches = list(Branch.objects.filter(is_active=True))
    n = len(branches)
    out = []

    # 1. ВКонтакте: сообщество у каждой точки
    vk_ok = set(SenlerConfig.objects.filter(is_active=True, branch__in=branches).values_list('branch_id', flat=True))
    missing = [b.name for b in branches if b.pk not in vk_ok]
    out.append(block('vk', 'Сообщество ВК', OK if n and not missing else WARN,
                     f'подключено у {len(vk_ok)} из {n} точек',
                     ('Нет сообщества у: ' + ', '.join(missing[:5])) if missing else ''))

    # 2. Сканы гостей за 7 дней
    visits = ClientBranchVisit.objects.filter(visited_at__gte=week)
    by_branch = set(visits.values_list('client__branch_id', flat=True).distinct())
    silent = [b.name for b in branches if b.pk not in by_branch]
    total_visits = visits.count()
    out.append(block('scans', 'Сканы QR гостями', OK if total_visits and not silent else WARN,
                     f'{total_visits} за 7 дней',
                     ('Ни одного скана за неделю у: ' + ', '.join(silent[:5]) +
                      ' — проверьте, стоят ли таблички с QR.') if silent else ''))

    # 3. Касса
    pos = getattr(config, 'pos_type', 'none') or 'none'
    if pos == 'none':
        out.append(block('pos', 'Касса', OFF, 'не подключена',
                         'Подключите iiko или Dooglys — без этого нет заказов доставки и выручки.'))
    else:
        field = 'iiko_organization_id' if pos == 'iiko' else 'dooglys_branch_id'
        bound = [b for b in branches if getattr(b, field)]
        out.append(block('pos', 'Касса', OK if len(bound) == n else WARN,
                         f'{pos}: точек с кодом кассы {len(bound)} из {n}',
                         '' if len(bound) == n else 'Заполните код кассы у остальных точек.'))

    # 4. iikoCard
    mode = getattr(config, 'iiko_card_mode', 'off') or 'off'
    if mode == 'off':
        out.append(block('iikocard', 'Связь гостей с iikoCard', OFF, 'выключена'))
    else:
        last = PosGuestLink.objects.aggregate(m=Max('orders_synced_at'))['m']
        errors = PosGuestLink.objects.filter(status=PosGuestLinkStatus.ERROR).count()
        linked = PosGuestLink.objects.filter(status=PosGuestLinkStatus.LINKED).count()
        stale = last is None or now - last > timedelta(hours=2)
        out.append(block('iikocard', 'Связь гостей с iikoCard', WARN if stale or errors else OK,
                         f'режим «{"Карта гостя" if mode == "card" else "только связь"}», связано {linked}',
                         ('Заказы не забирались больше 2 часов. ' if stale else '') +
                         (f'Ошибок проверки: {errors}.' if errors else '')))

    # 5. Телефоны
    guests = Client.objects.filter(is_active=True, branch_profiles__isnull=False).distinct()
    total = guests.count()
    phones = guests.filter(phone_consent_at__isnull=False).exclude(phone='').count()
    phone_on = bool(getattr(settings, 'GUEST_PHONE_ENABLED', False)) and bool(getattr(config, 'guest_phone_enabled', False))
    out.append(block('phones', 'Сбор телефонов', OFF if not phone_on else (OK if total and phones * 10 >= total else WARN),
                     f'телефон у {phones} из {total} гостей ({_pct(phones, total)})',
                     '' if not phone_on else ('' if total and phones * 10 >= total else
                                              'Меньше 10% — включите баллы за номер и авторассылку «поделитесь номером».')))

    # 6. Скан чеков
    if not getattr(config, 'receipt_scan_enabled', False):
        out.append(block('receipts', 'Скан QR с чека', OFF, 'выключен'))
    else:
        with_fn = [b for b in branches if (b.fiscal_fns or '').strip()]
        unknown = ReceiptScan.objects.filter(status=ReceiptScanStatus.UNKNOWN_FN, created_at__gte=now - timedelta(days=30)).count()
        credited = ReceiptScan.objects.filter(status=ReceiptScanStatus.CREDITED, created_at__gte=week).count()
        out.append(block('receipts', 'Скан QR с чека', OK if len(with_fn) == n and not unknown else WARN,
                         f'засчитано за 7 дней: {credited}; ФН указан у {len(with_fn)} из {n} точек',
                         (f'{unknown} чеков за 30 дней с незнакомой кассой — добавьте их ФН точкам. ' if unknown else '') +
                         ('Укажите ФН касс у всех точек.' if len(with_fn) != n else '')))

    # 7. Авторассылки
    rules = AutoBroadcastRule.objects.filter(is_active=True).count()
    sent_week = BroadcastRecipient.objects.filter(status=RecipientStatus.SENT, sent_at__gte=week).count()
    out.append(block('auto', 'Авторассылки', OFF if not rules else (OK if sent_week else WARN),
                     f'включено правил: {rules}; сообщений за 7 дней: {sent_week}',
                     '' if not rules or sent_week else 'Правила включены, но за неделю ничего не ушло — проверьте сегменты и токен ВК.'))

    # 8. Ссылки на отзывы
    linked_reviews = [b for b in branches if b.review_link_yandex or b.review_link_2gis]
    out.append(block('reviews', 'Ссылки на отзывы (Яндекс, 2ГИС)', OK if len(linked_reviews) == n else WARN,
                     f'указаны у {len(linked_reviews)} из {n} точек',
                     '' if len(linked_reviews) == n else 'Без ссылок довольный гость не дойдёт до карт.'))

    # 9. RF-сегменты
    calc = GuestRFScore.objects.aggregate(m=Max('calculated_at'))['m']
    out.append(block('rf', 'RF-сегменты', OK if calc and now - calc < timedelta(days=2) else WARN,
                     f'пересчитаны {timezone.localtime(calc):%d.%m %H:%M}' if calc else 'ещё не считались',
                     '' if calc and now - calc < timedelta(days=2) else 'Ночной пересчёт не проходил больше двух суток.'))

    score = sum(1 for b in out if b['status'] == OK)
    return {'blocks': out, 'ok': score, 'total': len(out),
            'warn': sum(1 for b in out if b['status'] == WARN),
            'off': sum(1 for b in out if b['status'] == OFF)}
