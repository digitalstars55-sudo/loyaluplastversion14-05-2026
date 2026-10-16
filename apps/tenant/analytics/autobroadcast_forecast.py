"""
Прогноз выручки авторассылок на 30 дней: N × p × k × S (ТЗ LoyalUP_technical_spec, п.7; 16.10.2026).

  N — уникальные гости, которых сценарий достанет за 30 дней. Для «не приходил
      N дней» — те, у кого порог наступит в ближайшие 30 дней (по дате
      последнего визита); для остальных событий — темп последних 30 дней;
  p — доля гостей с подтверждённым возвратом после этого сценария (визит,
      чек, заказ по карте в окне 14 дней, атрибуция к последней отправке —
      тем же законом, что «Статистика кампаний»);
  k — покупок на вернувшегося гостя, у которого деньги видны (связь с кассой
      или засчитанный чек);
  S — средняя покупка этих гостей.

История — отправки сценария за последние 90 дней с закрытым окном (отправлены
раньше чем 14 дней назад). Меньше MIN_SAMPLE получателей или ни одной видимой
покупки — «Недостаточно данных», числа не придумываем. Это оценка выручки после
контакта, не прибыль и не факт.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from .campaign_stats import WINDOW, attribute, daily_purchases

HISTORY = timedelta(days=90)
HORIZON_DAYS = 30
MIN_SAMPLE = 30


def _n_no_visit(rule, today) -> int:
    from django.db.models import Max
    from apps.tenant.branch.models import ClientBranch
    from apps.tenant.senler.engine import _apply_audience

    n = rule.delay_days or 0
    if n <= 0:
        return 0
    lo, hi = today - timedelta(days=n - 1), today + timedelta(days=HORIZON_DAYS - n)
    qs = (ClientBranch.objects
          .filter(is_employee=False, client__is_active=True, client__vk_id__isnull=False)
          .annotate(last_visit=Max('visits__visited_at'))
          .filter(last_visit__date__gte=lo, last_visit__date__lte=hi))
    return _apply_audience(qs, rule).values('client__vk_id').distinct().count()


def for_rules(rules, now=None, branch_ids=None) -> dict:
    """{rule_id: прогноз} — одним проходом по истории всех отправок."""
    from django.utils import timezone

    from apps.tenant.analytics.models import PosGuestLink, PosGuestLinkStatus, PosGuestOrder
    from apps.tenant.branch.models import ClientBranchVisit, ReceiptScan, ReceiptScanStatus
    from apps.tenant.senler.models import AutoBroadcastType as T, BroadcastRecipient, RecipientStatus

    now = now or timezone.now()
    today = timezone.localdate(now)
    rules = list(rules)
    if not rules:
        return {}
    since, closed_before = now - HISTORY, now - WINDOW

    # Все отправки периода — чтобы возврат достался ПОСЛЕДНЕЙ, как в статистике кампаний.
    qs = BroadcastRecipient.objects.filter(status=RecipientStatus.SENT, sent_at__gte=since - WINDOW,
                                           sent_at__lt=now, client_branch__isnull=False)
    if branch_ids:
        qs = qs.filter(client_branch__branch_id__in=branch_ids)
    rows = list(qs.values_list('client_branch__client_id', 'sent_at', 'send__auto_broadcast_rule_id',
                             'send__draft_id', 'send__broadcast_id'))
    sends = []
    for guest, at, rule_id, draft_id, bc_id in rows:
        key = f'rule:{rule_id}' if rule_id else (f'draft:{draft_id}' if draft_id else f'bc:{bc_id}')
        sends.append((guest, at, key))
    guests = {g for g, _, _ in sends}
    until = now
    visits = list(ClientBranchVisit.objects
                  .filter(client__client_id__in=guests, visited_at__gte=since - WINDOW, visited_at__lt=until)
                  .values_list('client__client_id', 'visited_at'))
    receipts = list(ReceiptScan.objects
                    .filter(client_id__in=guests, status=ReceiptScanStatus.CREDITED,
                            receipt_at__gte=since - WINDOW, receipt_at__lt=until)
                    .values_list('client_id', 'receipt_at', 'amount'))
    orders = [(g, at, paid if paid is not None else total) for g, at, paid, total in
              PosGuestOrder.objects
              .filter(link__client_id__in=guests, closed_at__gte=since - WINDOW, closed_at__lt=until)
              .values_list('link__client_id', 'closed_at', 'paid_sum', 'order_sum')]
    covered = set(PosGuestLink.objects.filter(client_id__in=guests, status=PosGuestLinkStatus.LINKED)
                  .values_list('client_id', flat=True))
    covered |= set(ReceiptScan.objects.filter(client_id__in=guests).values_list('client_id', flat=True))
    purchases = daily_purchases(receipts, orders)

    # История — только отправки с закрытым окном; покупки/визиты считаем по тем же правилам.
    history = [(g, at, key) for g, at, key in sends if since <= at < closed_before]
    groups = attribute(history, visits, purchases, covered)
    # Покупки, засчитанные каждой кампании: повторяем распределение attribute()
    # по покупкам, чтобы посчитать k (штук), а не только сумму.
    from bisect import bisect_right
    from collections import defaultdict
    by_guest = defaultdict(list)
    for g, at, key in history:
        by_guest[g].append((at, key))
    for g in by_guest:
        by_guest[g].sort(key=lambda r: r[0])
    count = defaultdict(int)
    for g, at, amount in purchases:
        rows_g = by_guest.get(g)
        if not rows_g:
            continue
        i = bisect_right([r[0] for r in rows_g], at) - 1
        if i >= 0 and at - rows_g[i][0] <= WINDOW:
            count[rows_g[i][1]] += 1

    recent = {}
    for g, at, key in sends:
        if at >= now - timedelta(days=HORIZON_DAYS):
            recent.setdefault(key, set()).add(g)

    out = {}
    for rule in rules:
        key = f'rule:{rule.pk}'
        grp = groups.get(key)
        sample = len(grp.guests) if grp else 0
        returned = len(grp.returned) if grp else 0
        ret_cov = len(grp.returned & grp.covered) if grp else 0
        revenue = grp.revenue if grp else Decimal('0')
        bought = count.get(key, 0)
        if rule.event == T.NO_VISIT_DAYS:
            n, basis = _n_no_visit(rule, today), 'threshold'
        else:
            n, basis = len(recent.get(key, ())), 'pace'
        p = (returned / sample) if sample else None
        k = (bought / ret_cov) if ret_cov else None
        s = (revenue / bought) if bought else None
        enough = sample >= MIN_SAMPLE and bought > 0 and p is not None and k is not None and s is not None
        out[rule.pk] = {
            'n': n, 'n_basis': basis,
            'p': round(p, 4) if p is not None else None,
            'k': round(k, 2) if k is not None else None,
            's': float(round(s, 2)) if s is not None else None,
            'sample_guests': sample, 'returned': returned, 'returned_covered': ret_cov,
            'purchases': bought,
            'coverage_pct': round(ret_cov * 100 / returned, 1) if returned else None,
            'forecast': float(round(Decimal(n) * Decimal(str(p)) * Decimal(str(k)) * s, 0)) if enough else None,
            'enough_data': enough,
            'min_sample': MIN_SAMPLE, 'history_days': HISTORY.days, 'window_days': WINDOW.days,
            'horizon_days': HORIZON_DAYS,
        }
    return out
