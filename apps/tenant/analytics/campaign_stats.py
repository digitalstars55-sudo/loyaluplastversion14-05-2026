"""
Статистика рекламных кампаний (ТЗ маркетолога п.6, волна 2, 14.10.2026).

Правила:
  • окно — 14 дней после отправки;
  • атрибуция к ПОСЛЕДНЕЙ отправке: возврат гостя (визит, чек, заказ по карте)
    засчитывается одной рассылке — последней, которую он получил до события и
    не раньше чем за окно; двойного учёта между кампаниями нет;
  • деньги — только известные: засчитанные чеки (`ReceiptScan`) и заказы по
    карте iikoCard (`PosGuestOrder`); чек и заказ одного дня — одна покупка
    (берётся большее); у кого денег не видно — «нет данных», а не 0, и
    подпись покрытия «по N из M гостей»;
  • контрольная группа — гости аудитории рассылки, не получившие сообщения
    (`BroadcastControlMember`); «+N ₽» считаем, только когда и у получивших,
    и у контрольной группы достаточно гостей с известными деньгами.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

WINDOW = timedelta(days=14)
MIN_COVERED = 10        # меньше гостей с известными деньгами — «+N ₽» не считаем


@dataclass
class Group:
    guests: set = field(default_factory=set)
    returned: set = field(default_factory=set)
    covered: set = field(default_factory=set)
    revenue: Decimal = Decimal('0')
    events: int = 0

    def rate(self):
        return round(len(self.returned) * 100 / len(self.guests), 1) if self.guests else None

    def per_covered(self):
        return (self.revenue / len(self.covered)) if self.covered else None


def daily_purchases(receipts, orders):
    """[(гость, время, сумма)] чеков и заказов → по (гость, день) одна покупка: большая сумма."""
    best = {}
    for guest, at, amount in list(receipts) + list(orders):
        key = (guest, at.date())
        if key not in best or (amount or 0) > (best[key][1] or 0):
            best[key] = (at, amount or Decimal('0'))
    return [(g, at, amount) for (g, _), (at, amount) in best.items()]


def attribute(sends, visits, purchases, covered_guests, window=WINDOW):
    """
    sends     — [(гость, время, ключ кампании)]
    visits    — [(гость, время)]
    purchases — [(гость, время, сумма)]
    covered_guests — гости, у которых деньги вообще видны (связь с кассой / чеки)
    → {ключ: Group}
    """
    by_guest = defaultdict(list)
    groups = defaultdict(Group)
    for guest, at, key in sends:
        by_guest[guest].append((at, key))
        g = groups[key]
        g.guests.add(guest)
        if guest in covered_guests:
            g.covered.add(guest)
    times = {}
    for guest, rows in by_guest.items():
        rows.sort(key=lambda r: r[0])
        times[guest] = [r[0] for r in rows]

    def owner(guest, at):
        rows = by_guest.get(guest)
        if not rows:
            return None
        i = bisect_right(times[guest], at) - 1
        if i < 0 or at - rows[i][0] > window:
            return None
        return rows[i][1]

    for guest, at in visits:
        key = owner(guest, at)
        if key is not None:
            groups[key].returned.add(guest)
            groups[key].events += 1
    for guest, at, amount in purchases:
        key = owner(guest, at)
        if key is not None:
            groups[key].returned.add(guest)
            groups[key].revenue += amount or Decimal('0')
    return groups


def control_group(members, visits, purchases, covered_guests, window=WINDOW):
    """members — [(гость, время отправки рассылки)] контрольной группы → Group."""
    start = dict(members)
    g = Group(guests=set(start))
    g.covered = {m for m in start if m in covered_guests}
    for guest, at in visits:
        t0 = start.get(guest)
        if t0 is not None and t0 < at <= t0 + window:
            g.returned.add(guest)
            g.events += 1
    for guest, at, amount in purchases:
        t0 = start.get(guest)
        if t0 is not None and t0 < at <= t0 + window:
            g.returned.add(guest)
            g.revenue += amount or Decimal('0')
    return g


def compare(sent: Group, control: Group | None) -> dict:
    out = {'uplift_pp': None, 'extra_returns': None, 'extra_revenue': None}
    if control is None or not control.guests or not sent.guests:
        return out
    rs, rc = sent.rate(), control.rate()
    out['uplift_pp'] = round(rs - rc, 1)
    out['extra_returns'] = round((rs - rc) * len(sent.guests) / 100)
    if len(sent.covered) >= MIN_COVERED and len(control.covered) >= MIN_COVERED:
        diff = sent.per_covered() - control.per_covered()
        out['extra_revenue'] = float(round(diff * len(sent.covered)))
    return out


def group_dict(g: Group) -> dict:
    return {
        'guests': len(g.guests), 'returned': len(g.returned), 'return_rate': g.rate(),
        'covered': len(g.covered),
        'revenue': float(g.revenue) if g.covered else None,
    }


# ── загрузка из базы ──────────────────────────────────────────────────────────

def build(start, end, now=None, branch_ids=None) -> dict:
    """Кампании с первой отправкой в [start, end) и их итоги за 14 дней."""
    from django.db.models import Min
    from django.utils import timezone

    from apps.tenant.analytics.models import PosGuestLink, PosGuestLinkStatus, PosGuestOrder
    from apps.tenant.branch.models import ClientBranchVisit, ReceiptScan, ReceiptScanStatus
    from apps.tenant.senler.models import BroadcastControlMember, BroadcastRecipient, RecipientStatus

    now = now or timezone.now()
    recipients = BroadcastRecipient.objects.filter(
        status=RecipientStatus.SENT, sent_at__gte=start - WINDOW, sent_at__lt=end, client_branch__isnull=False)
    if branch_ids:
        recipients = recipients.filter(client_branch__branch_id__in=branch_ids)
    rows = list(recipients
                .values_list('client_branch__client_id', 'sent_at', 'read_at', 'send__draft_id',
                             'send__auto_broadcast_rule_id', 'send__broadcast_id',
                             'send__draft__name', 'send__auto_broadcast_rule__name', 'send__broadcast__name'))
    meta, sends, reads = {}, [], defaultdict(set)
    for guest, at, read_at, draft_id, rule_id, bc_id, d_name, r_name, b_name in rows:
        if draft_id:
            key, kind, name = f'draft:{draft_id}', 'broadcast', d_name or f'Рассылка #{draft_id}'
        elif rule_id:
            key, kind, name = f'rule:{rule_id}', 'auto', r_name or f'Авторассылка #{rule_id}'
        else:
            key, kind, name = f'bc:{bc_id}', 'broadcast', b_name or f'Рассылка #{bc_id}'
        m = meta.setdefault(key, {'key': key, 'kind': kind, 'name': name, 'first_at': at, 'last_at': at})
        m['first_at'], m['last_at'] = min(m['first_at'], at), max(m['last_at'], at)
        sends.append((guest, at, key))
        if read_at:
            reads[key].add(guest)
    keep = {k for k, m in meta.items() if start <= m['first_at'] < end}

    guests = {g for g, _, _ in sends}
    controls = defaultdict(list)
    draft_ids = [int(k.split(':')[1]) for k in keep if k.startswith('draft:')]
    for draft_id, guest, sent_at in (BroadcastControlMember.objects
                                     .filter(draft_id__in=draft_ids,
                                             **({'client_branch__branch_id__in': branch_ids} if branch_ids else {}))
                                     .values_list('draft_id', 'client_branch__client_id', 'draft__sent_at')):
        controls[f'draft:{draft_id}'].append((guest, sent_at))
        guests.add(guest)

    until = end + WINDOW
    visits = list(ClientBranchVisit.objects
                  .filter(client__client_id__in=guests, visited_at__gte=start - WINDOW, visited_at__lt=until)
                  .values_list('client__client_id', 'visited_at'))
    receipts = list(ReceiptScan.objects
                    .filter(client_id__in=guests, status=ReceiptScanStatus.CREDITED,
                            receipt_at__gte=start - WINDOW, receipt_at__lt=until)
                    .values_list('client_id', 'receipt_at', 'amount'))
    orders = [(g, at, paid if paid is not None else total) for g, at, paid, total in
              PosGuestOrder.objects
              .filter(link__client_id__in=guests, closed_at__gte=start - WINDOW, closed_at__lt=until)
              .values_list('link__client_id', 'closed_at', 'paid_sum', 'order_sum')]
    covered = set(PosGuestLink.objects.filter(client_id__in=guests, status=PosGuestLinkStatus.LINKED)
                  .values_list('client_id', flat=True))
    covered |= set(ReceiptScan.objects.filter(client_id__in=guests).values_list('client_id', flat=True))
    purchases = daily_purchases(receipts, orders)

    groups = attribute(sends, visits, purchases, covered)
    out = []
    for key in sorted(keep, key=lambda k: meta[k]['first_at'], reverse=True):
        m, g = meta[key], groups[key]
        ctrl = control_group(controls[key], visits, purchases, covered) if controls.get(key) else None
        out.append({
            **{k: m[k] for k in ('key', 'kind', 'name')},
            'sent_at': m['first_at'].isoformat(),
            'window_closed': now >= m['last_at'] + WINDOW,
            'read': len(reads[key]),
            **group_dict(g),
            'control': group_dict(ctrl) if ctrl else None,
            **compare(g, ctrl),
        })
    return {'window_days': WINDOW.days, 'campaigns': out}
