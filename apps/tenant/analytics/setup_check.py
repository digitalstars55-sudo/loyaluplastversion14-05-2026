"""
«Чекап» сети и точек (ТЗ LoyalUP_technical_spec, п.10; 14.10 → полная версия 16.10.2026).

Каждый блок — состояние подключения и использования:
  state = off      — «Не подключён»;
          setup    — «Подключён, не настроен»;
          ok       — «Работает»;
          problem  — «Есть проблема»;
          nodata   — «Нет данных для проверки» (неизвестное НЕ показываем исправным).
  checked — что проверялось; checked_at — когда; action/href — следующий шаг
  и экран CheckUp, где его сделать; branches — разбивка по точкам, где она есть.
  status — прежнее ok/warn/off для старых клиентов.

`recommendations` — «Что улучшить» для отчётов: 2–3 пункта по данным именно этой
сети, с причиной и ссылкой на экран; отключённый блок — предложение настроить,
сломанный — устранить ошибку. Только чтение текущей схемы; ничего не меняет и
ничего не включает.
"""
from __future__ import annotations

from datetime import timedelta

OFF, SETUP, OK, PROBLEM, NODATA = 'off', 'setup', 'ok', 'problem', 'nodata'
STATE_LABEL = {OFF: 'Не подключён', SETUP: 'Подключён, не настроен', OK: 'Работает',
               PROBLEM: 'Есть проблема', NODATA: 'Нет данных для проверки'}
_LEGACY = {OFF: 'off', SETUP: 'warn', OK: 'ok', PROBLEM: 'warn', NODATA: 'warn'}


def block(key, title, state, text, hint='', *, checked='', action='', href='', branches=None, now=None):
    from django.utils import timezone
    return {'key': key, 'title': title, 'state': state, 'state_label': STATE_LABEL[state],
            'status': _LEGACY[state], 'text': text, 'hint': hint, 'checked': checked,
            'checked_at': (now or timezone.now()).isoformat(), 'action': action, 'href': href,
            'branches': branches or []}


def _pct(part, total):
    return f'{round(part * 100 / total)}%' if total else '—'


def _row(b, state, text):
    return {'id': b.pk, 'name': b.name, 'state': state, 'state_label': STATE_LABEL[state], 'text': text}


def build(config, now=None) -> dict:
    from django.conf import settings
    from django.db.models import Count, Max, Q
    from django.utils import timezone

    from apps.shared.guest.models import Client
    from apps.tenant.analytics.models import (GuestRFScore, KnowledgeBaseDocument, PosGuestLink,
                                              PosGuestLinkStatus)
    from apps.tenant.branch.models import (Branch, ClientBranchVisit, ReceiptScan, ReceiptScanStatus,
                                           ReviewProof, TestimonialConversation)
    from apps.tenant.senler.models import (AutoBroadcastRule, BroadcastDraft, BroadcastRecipient,
                                           RecipientStatus, SenlerConfig)

    now = now or timezone.now()
    today = timezone.localdate(now)
    week = now - timedelta(days=7)
    month = now - timedelta(days=30)
    branches = list(Branch.objects.filter(is_active=True).order_by('name'))
    n = len(branches)
    out = []
    B = lambda *a, **k: block(*a, now=now, **k)          # noqa: E731

    # 1. Оцифровка гостей: сообщество ВК у точек и сканы QR за неделю
    vk_ok = set(SenlerConfig.objects.filter(is_active=True, branch__in=branches).values_list('branch_id', flat=True))
    visits = ClientBranchVisit.objects.filter(visited_at__gte=week)
    by_branch = dict(visits.values('client__branch_id').annotate(c=Count('id')).values_list('client__branch_id', 'c'))
    rows = []
    for b in branches:
        if b.pk not in vk_ok:
            rows.append(_row(b, OFF, 'нет сообщества ВК'))
        elif not by_branch.get(b.pk):
            rows.append(_row(b, PROBLEM, 'ни одного скана за 7 дней'))
        else:
            rows.append(_row(b, OK, f'сканов за 7 дней: {by_branch[b.pk]}'))
    bad = [r for r in rows if r['state'] != OK]
    state = NODATA if not n else (OK if not bad else (OFF if all(r['state'] == OFF for r in rows) else PROBLEM))
    out.append(B('digitize', 'Оцифровка гостей', state,
                 f'сканов за 7 дней: {sum(by_branch.values())}; сообщество ВК у {len(vk_ok)} из {n} точек',
                 ('Проблема у: ' + ', '.join(r['name'] for r in bad[:5])) if bad else '',
                 checked='сообщество ВК у каждой точки и сканы QR гостями за 7 дней',
                 action='Проверьте таблички с QR и подключение сообщества' if bad else '',
                 href='/loyalty/qr', branches=rows))

    # 2. Касса (POS) и связь гостей с ней
    pos = getattr(config, 'pos_type', 'none') or 'none'
    linked = PosGuestLink.objects.filter(status=PosGuestLinkStatus.LINKED).count()
    guests_total = Client.objects.filter(is_active=True, branch_profiles__isnull=False).distinct().count()
    if pos == 'none':
        out.append(B('pos', 'POS (касса)', OFF, 'не подключена',
                     'Без кассы нет заказов, выручки и честной статистики кампаний.',
                     checked='тип кассы сети', action='Подключите iiko или Dooglys', href='/loyalty/settings'))
    else:
        field = 'iiko_organization_id' if pos == 'iiko' else 'dooglys_branch_id'
        rows = [_row(b, OK if getattr(b, field) else SETUP,
                     'код кассы указан' if getattr(b, field) else 'нет кода кассы') for b in branches]
        mode = getattr(config, 'iiko_card_mode', 'off') or 'off'
        last = PosGuestLink.objects.aggregate(m=Max('orders_synced_at'))['m']
        errors = PosGuestLink.objects.filter(status=PosGuestLinkStatus.ERROR).count()
        share = linked * 100 / guests_total if guests_total else 0
        if any(r['state'] != OK for r in rows):
            state, hint = SETUP, 'Заполните код кассы у всех точек.'
        elif mode != 'off' and (last is None or now - last > timedelta(hours=2) or errors):
            state, hint = PROBLEM, ('Заказы по картам не забирались больше 2 часов. ' if last is None or now - last > timedelta(hours=2) else '') + (f'Ошибок проверки связи: {errors}.' if errors else '')
        elif mode == 'off':
            state, hint = SETUP, 'Касса подключена, но связь гостей с ней выключена — заказы гостей не видны.'
        else:
            state = OK if share >= 20 else PROBLEM
            hint = '' if share >= 20 else f'С заказами связаны {round(share)}% гостей. Проверьте сопоставление карт и сценарий запроса телефона.'
        out.append(B('pos', 'POS (касса)', state,
                     f'{pos}: код кассы у {sum(r["state"] == OK for r in rows)} из {n} точек; связано с кассой {linked} из {guests_total} гостей ({_pct(linked, guests_total)})',
                     hint, checked='коды кассы точек, режим связи iikoCard, свежесть выгрузки заказов, доля связанных гостей',
                     action='Настройте связь с кассой' if state != OK else '', href='/loyalty/settings', branches=rows))

    # 3. Сбор телефонов
    phones = Client.objects.filter(is_active=True, branch_profiles__isnull=False, phone_consent_at__isnull=False).exclude(phone='').distinct().count()
    phone_on = bool(getattr(settings, 'GUEST_PHONE_ENABLED', False)) and bool(getattr(config, 'guest_phone_enabled', False))
    if not phone_on:
        out.append(B('phones', 'Сбор телефонов', OFF, f'телефон у {phones} из {guests_total} гостей',
                     'Включите — по телефону гость связывается с кассой.', checked='включён ли сбор телефона',
                     action='Включите сбор телефонов', href='/loyalty/settings'))
    else:
        ok = guests_total and phones * 10 >= guests_total
        out.append(B('phones', 'Сбор телефонов', OK if ok else (NODATA if not guests_total else PROBLEM),
                     f'телефон у {phones} из {guests_total} гостей ({_pct(phones, guests_total)})',
                     '' if ok else 'Меньше 10% — включите баллы за номер и авторассылку «поделитесь номером».',
                     checked='доля гостей с телефоном и согласием', action='' if ok else 'Настройте запрос телефона',
                     href='/loyalty/auto-broadcasts'))

    # 4. Отзывы: ответы на негатив, ссылки на карты, очередь скриншотов
    neg_open = TestimonialConversation.objects.filter(
        sentiment__in=('NEGATIVE', 'PARTIALLY_NEGATIVE'), is_replied=False, resolved_at__isnull=True,
        last_message_at__gte=month).count()
    pending = ReviewProof.objects.filter(status=ReviewProof.Status.PENDING).count()
    no_coins = ReviewProof.objects.filter(status=ReviewProof.Status.APPROVED, coins=0).count()
    rows = [_row(b, OK if (b.review_link_yandex or b.review_link_2gis) else SETUP,
                 'ссылки на карты есть' if (b.review_link_yandex or b.review_link_2gis) else 'нет ссылок на Яндекс/2ГИС')
            for b in branches]
    noref = [r for r in rows if r['state'] != OK]
    if neg_open or no_coins:
        state = PROBLEM
    elif noref:
        state = SETUP
    else:
        state = OK
    hints = []
    if neg_open:
        hints.append(f'{neg_open} негативных за 30 дней без ответа')
    if pending:
        hints.append(f'{pending} скриншотов ждут проверки')
    if no_coins:
        hints.append(f'{no_coins} подтверждённых без баллов')
    if noref:
        hints.append('нет ссылок на карты у: ' + ', '.join(r['name'] for r in noref[:5]))
    out.append(B('reviews', 'Отзывы', state,
                 f'негатив без ответа: {neg_open}; скриншоты на проверке: {pending}',
                 '; '.join(hints), checked='негатив без ответа и без «Решено» за 30 дней, очередь скриншотов, ссылки на карты',
                 action='Разберите отзывы' if neg_open else ('Разберите скриншоты' if pending or no_coins else ''),
                 href='/loyalty/reviews/proofs' if (pending or no_coins) and not neg_open else '/loyalty/reviews',
                 branches=rows))

    # 5. Рассылки за 30 дней
    sent30 = BroadcastDraft.objects.filter(sent_at__gte=month).count()
    out.append(B('broadcasts', 'Рассылки', OK if sent30 else SETUP,
                 f'отправлено за 30 дней: {sent30}',
                 '' if sent30 else 'За месяц не было ни одной рассылки.', checked='отправленные рассылки за 30 дней',
                 action='' if sent30 else 'Запустите рассылку по сегменту', href='/loyalty/broadcasts'))

    # 6. Авторассылки: включены, в сроке, отправляют
    rules = list(AutoBroadcastRule.objects.filter(is_active=True, is_archived=False))
    live = [r for r in rules if (not r.active_from or r.active_from <= today) and (not r.active_to or r.active_to >= today)]
    expired = [r for r in rules if r.active_to and r.active_to < today]
    sent_week = BroadcastRecipient.objects.filter(status=RecipientStatus.SENT, sent_at__gte=week,
                                                  send__auto_broadcast_rule__isnull=False).count()
    if not rules:
        state, hint = OFF, ''
    elif not live:
        state, hint = PROBLEM, f'Включено {len(rules)}, но у всех закончился срок или он ещё не начался — ничего не уходит.'
    elif not sent_week:
        state, hint = PROBLEM, 'Правила в сроке, но за неделю ничего не ушло — проверьте сегменты и токен ВК.'
    else:
        state, hint = OK, (f'У {len(expired)} правил закончился срок.' if expired else '')
    out.append(B('auto', 'Авторассылки', state,
                 f'включено {len(rules)}, в сроке {len(live)}; сообщений за 7 дней: {sent_week}', hint,
                 checked='включённые правила, их срок действия и отправки за 7 дней',
                 action='' if state == OK else 'Настройте сценарии авторассылок', href='/loyalty/auto-broadcasts'))

    # 7. Сегментация (RF)
    calc = GuestRFScore.objects.aggregate(m=Max('calculated_at'))['m']
    if calc is None:
        out.append(B('rf', 'Сегментация (RF)', NODATA, 'ещё не считалась', 'Ночной пересчёт ни разу не прошёл.',
                     checked='время последнего пересчёта RF', href='/loyalty/rf'))
    else:
        fresh = now - calc < timedelta(days=2)
        out.append(B('rf', 'Сегментация (RF)', OK if fresh else PROBLEM,
                     f'пересчитаны {timezone.localtime(calc):%d.%m %H:%M}',
                     '' if fresh else 'Ночной пересчёт не проходил больше двух суток.',
                     checked='время последнего пересчёта RF', href='/loyalty/rf'))

    # 8. Скан QR с чека
    if not getattr(config, 'receipt_scan_enabled', False):
        out.append(B('receipts', 'Скан QR с чека', OFF, 'выключен', checked='включён ли скан чека',
                     href='/loyalty/settings'))
    else:
        unknown = ReceiptScan.objects.filter(status=ReceiptScanStatus.UNKNOWN_FN, created_at__gte=month).count()
        credited = ReceiptScan.objects.filter(status=ReceiptScanStatus.CREDITED, created_at__gte=week).count()
        rows = [_row(b, OK if (b.fiscal_fns or '').strip() else SETUP,
                     'ФН указан' if (b.fiscal_fns or '').strip() else 'нет ФН кассы') for b in branches]
        state = PROBLEM if unknown else (SETUP if any(r['state'] != OK for r in rows) else OK)
        out.append(B('receipts', 'Скан QR с чека', state, f'засчитано за 7 дней: {credited}',
                     (f'{unknown} чеков за 30 дней с незнакомой кассой — добавьте их ФН точкам.' if unknown else ''),
                     checked='ФН касс точек и незнакомые чеки за 30 дней', href='/loyalty/points', branches=rows))

    # 9. AI-маркетолог: включён и есть база знаний
    try:
        from apps.tenant.marketer.models import MarketerPost, MarketerSettings
        mcfg = MarketerSettings.objects.first()
        docs = KnowledgeBaseDocument.objects.filter(is_active=True).count()
        posts30 = MarketerPost.objects.filter(published_at__gte=month).count()
        if not mcfg or not mcfg.is_enabled:
            out.append(B('marketer', 'AI маркетолог', OFF, 'выключен', checked='включён ли маркетолог',
                         action='Включите AI маркетолога', href='/loyalty/marketer'))
        elif not docs:
            out.append(B('marketer', 'AI маркетолог', SETUP, 'включён, база знаний пуста',
                         'Без базы знаний ИИ не пишет о заведении — загрузите меню, тон бренда, примеры постов.',
                         checked='включён ли маркетолог и документы базы знаний',
                         action='Загрузите базу знаний', href='/loyalty/marketer'))
        else:
            out.append(B('marketer', 'AI маркетолог', OK if posts30 else SETUP,
                         f'документов базы знаний: {docs}; опубликовано за 30 дней: {posts30}',
                         '' if posts30 else 'За месяц не опубликовано ни одного поста — составьте контент-план.',
                         checked='документы базы знаний и публикации за 30 дней', href='/loyalty/marketer'))
    except Exception:                                    # noqa: BLE001 — чекап важнее одного блока
        out.append(B('marketer', 'AI маркетолог', NODATA, 'не удалось проверить', href='/loyalty/marketer'))

    recs = recommendations(out, now=now)
    counts = {s: sum(1 for b in out if b['state'] == s) for s in STATE_LABEL}
    return {'blocks': out, 'checked_at': now.isoformat(), 'states': STATE_LABEL, 'counts': counts,
            'recommendations': recs,
            # прежние поля для старых клиентов
            'ok': counts[OK], 'total': len(out),
            'warn': counts[SETUP] + counts[PROBLEM] + counts[NODATA], 'off': counts[OFF]}


def recommendations(blocks, now=None, limit: int = 3) -> list:
    """«Что улучшить»: 2–3 пункта по состоянию этой сети — сначала проблемы, затем ненастроенное."""
    from django.utils import timezone
    by = {b['key']: b for b in blocks}
    out = []

    def add(key, text, reason):
        b = by.get(key)
        if b and len(out) < limit:
            out.append({'key': key, 'text': text, 'reason': reason, 'action': b.get('action') or text,
                        'href': b.get('href', '')})

    order = sorted(blocks, key=lambda b: {PROBLEM: 0, SETUP: 1, OFF: 2, NODATA: 3, OK: 9}[b['state']])
    for b in order:
        if len(out) >= limit or b['state'] == OK:
            continue
        k = b['key']
        if k == 'auto':
            add(k, 'Настроить сценарии авторассылок', b['hint'] or 'Нет активных авторассылок при доступной аудитории.')
        elif k == 'reviews':
            add(k, 'Разобрать отзывы и очередь начисления', b['hint'])
        elif k == 'pos':
            add(k, 'Собрать телефоны и проверить связь карт с кассой', b['hint'] or b['text'])
        elif k == 'phones' and 'pos' not in {r['key'] for r in out}:
            add(k, 'Включить сбор телефонов', b['hint'] or b['text'])
        elif k == 'digitize':
            add(k, 'Проверить таблички с QR на точках', b['hint'])
        elif k == 'marketer':
            add(k, 'Наполнить базу знаний и составить контент-план', b['hint'] or b['text'])
        elif k == 'rf':
            add(k, 'Проверить ночной пересчёт сегментов', b['hint'] or b['text'])
    # Снижение тёплой аудитории за 30 дней — повод проверить сценарии возврата.
    if len(out) < limit:
        try:
            from apps.tenant.analytics import rf_transitions
            today = timezone.localdate(now)
            t = rf_transitions.build(None, today - timedelta(days=30), today)
            ws, we = t.get('warm_start'), t.get('warm_end')
            if ws and we is not None and we < ws:
                out.append({'key': 'rf_warm', 'text': 'Проверить сценарии возврата',
                            'reason': f'Тёплая аудитория за 30 дней снизилась: {ws} → {we}.',
                            'action': 'Откройте переходы RF', 'href': '/loyalty/rf'})
        except Exception:                                # noqa: BLE001
            pass
    return out
