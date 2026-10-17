"""Карточка гостя 360 — что сделать сейчас, прогноз, вкусы, поводы, лента (18.10.2026).

Макет утвердил владелец 18.10.2026 (артефакт «Карточка гостя 360»). Всё
считается из того, что уже лежит в базе сети, — ничего не выдумываем:

  · прогноз — по промежуткам между днями визитов гостя: обычный промежуток
    (медиана), сколько прошло с последнего, отсюда риск ухода и окно
    следующего визита; ценность за год — средний чек × ожидаемые визиты;
  · окупаемость — что гость принёс (засчитанные чеки + заказы по карте
    iikoCard) против себестоимости выданных подарков;
  · вкусы — дни недели и время визитов, любимая точка, заметки зала;
    любимых блюд нет: ни чек по QR, ни iikoCard позиций не отдают;
  · поводы — ДР (и включено ли автопоздравление), год с первого визита,
    поводы из заметок;
  · лента — визиты, покупки, подарки, рассылки, переписка, заметки — одним
    потоком;
  · «что сделать сейчас» — одно действие по правилам (ждёт ответа → ответить;
    скоро ДР → поздравить; остывает → подарок, лучше тот, что уже возвращал).

Сбой любого блока не роняет карточку: блок становится None.
"""
import logging
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from django.db.models import Q, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

ДНИ = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
ЛЕНТА = 60


def _безопасно(имя, f, *a, **kw):
    try:
        return f(*a, **kw)
    except Exception:  # noqa: BLE001 — блок карточки не роняет карточку
        logger.warning('guest360: блок %s не собрался', имя, exc_info=True)
        return None


def _дни_визитов(cb_ids):
    from apps.tenant.branch.models import ClientBranchVisit
    дни = sorted({timezone.localtime(v).date() for v in
                  ClientBranchVisit.objects.filter(client_id__in=cb_ids)
                  .order_by('-visited_at').values_list('visited_at', flat=True)[:500]})
    return дни


def прогноз(cb_ids, *, средний_чек=None, сегодня=None):
    сегодня = сегодня or timezone.localdate()
    дни = _дни_визитов(cb_ids)
    if len(дни) < 3:
        return {'enough': False, 'visits': len(дни),
                'reason': 'мало визитов для прогноза — нужно хотя бы три'}
    промежутки = [(b - a).days for a, b in zip(дни, дни[1:]) if (b - a).days > 0]
    # Не меньше трёх дней: у «каждодневных» (часто это сотрудники) пять дней
    # без визита давали риск 95 %.
    обычно = max(3, round(median(промежутки)))
    прошло = (сегодня - дни[-1]).days
    отношение = прошло / обычно
    # 0,7 обычного промежутка и меньше — риск 5 %; втрое дольше обычного — 95 %.
    риск = round(100 * min(0.95, max(0.05, (отношение - 0.7) / 2.3)))
    ждём = дни[-1] + timedelta(days=обычно)
    окно = (ждём - timedelta(days=max(1, обычно // 4)), ждём + timedelta(days=max(1, обычно // 4)))
    в_год = min(365 / обычно, 200)
    год = None
    if средний_чек:
        год = int(round(Decimal(str(средний_чек)) * Decimal(str(в_год)) * Decimal(str(1 - риск / 100)), -2))
    return {
        'enough': True, 'visits': len(дни), 'usual_gap_days': обычно, 'days_since': прошло,
        'cooling': отношение >= 1.5 and прошло >= 7, 'churn_risk_pct': риск,
        'next_visit_from': окно[0].isoformat(), 'next_visit_to': окно[1].isoformat(),
        'overdue_days': max(0, (сегодня - окно[1]).days),
        'year_value_rub': год, 'visits_per_year': round(в_год, 1),
    }


def вкусы(cb_ids, cbs, заметки):
    from apps.tenant.branch.models import ClientBranchVisit
    визиты = list(ClientBranchVisit.objects.filter(client_id__in=cb_ids)
                  .order_by('-visited_at').values_list('visited_at', 'client_id')[:300])
    по_дням, по_часам, по_точкам = {}, {}, {}
    точка = {cb.pk: (cb.branch.name if cb.branch_id else '') for cb in cbs}
    for when, cb_id in визиты:
        t = timezone.localtime(when)
        по_дням[t.weekday()] = по_дням.get(t.weekday(), 0) + 1
        полоса = 'утром' if t.hour < 12 else 'днём' if t.hour < 17 else 'вечером'
        по_часам[полоса] = по_часам.get(полоса, 0) + 1
        по_точкам[точка.get(cb_id, '')] = по_точкам.get(точка.get(cb_id, ''), 0) + 1
    дни = [ДНИ[d] for d, _ in sorted(по_дням.items(), key=lambda x: -x[1])[:2]] if визиты else []
    return {
        'days': дни,
        'time_of_day': max(по_часам, key=по_часам.get) if по_часам else None,
        'branches': [{'name': n, 'visits': c} for n, c in sorted(по_точкам.items(), key=lambda x: -x[1]) if n][:3],
        'allergies': [z['text'] for z in заметки if z['kind'] == 'allergy'],
        'notes': [z for z in заметки if z['kind'] == 'note'][:5],
        'favorite_dishes': None,
        'dishes_reason': 'позиции чеков не приходят: чек по QR и iikoCard отдают только сумму',
    }


def окупаемость(client, cb_ids):
    from apps.tenant.analytics.models import PosGuestOrder
    from apps.tenant.branch.models import ReceiptScan, ReceiptScanStatus
    from apps.tenant.inventory.models import AcquisitionSource, InventoryItem

    чеки = ReceiptScan.objects.filter(client=client, status=ReceiptScanStatus.CREDITED).aggregate(s=Sum('amount'))['s'] or 0
    касса = Decimal('0')
    for o in PosGuestOrder.objects.filter(link__client=client).values('paid_sum', 'order_sum'):
        касса += Decimal(str(o['paid_sum'] if o['paid_sum'] is not None else (o['order_sum'] or 0)))
    принесла = Decimal(str(чеки)) + касса
    подарки = list(InventoryItem.objects.filter(client_branch_id__in=cb_ids)
                   .select_related('product').values_list('acquired_from', 'product__cost_price_rub'))
    вложено = sum((Decimal(str(c or 0)) for _, c in подарки), Decimal('0'))
    извинения = sum((Decimal(str(c or 0)) for s, c in подарки if s == AcquisitionSource.APOLOGY), Decimal('0'))
    return {
        'revenue_rub': float(принесла), 'gifts_count': len(подарки),
        'gifts_cost_rub': float(вложено), 'apology_cost_rub': float(извинения),
        'ratio': round(float(принесла / вложено), 1) if вложено > 0 else None,
        'has_revenue_data': принесла > 0,
    }


def поводы(client, cbs, заметки, *, сегодня=None):
    from apps.tenant.senler.models import AutoBroadcastTemplate
    сегодня = сегодня or timezone.localdate()
    out = []

    def через(d: date):
        try:
            в_этом = d.replace(year=сегодня.year)
        except ValueError:          # 29 февраля
            в_этом = date(сегодня.year, 3, 1)
        if в_этом < сегодня:
            в_этом = в_этом.replace(year=сегодня.year + 1) if not (d.month == 2 and d.day == 29) else date(сегодня.year + 1, 3, 1)
        return в_этом, (в_этом - сегодня).days

    др = next((cb.birth_date for cb in cbs if cb.birth_date), None)
    if др:
        когда, дней = через(др)
        out.append({'kind': 'birthday', 'title': 'День рождения', 'date': когда.isoformat(), 'in_days': дней,
                    'auto': AutoBroadcastTemplate.objects.filter(type__startswith='birthday', is_active=True).exists()})
    первый = min((cb.created_at for cb in cbs), default=None)
    if первый and (сегодня - timezone.localtime(первый).date()).days >= 300:
        когда, дней = через(timezone.localtime(первый).date())
        out.append({'kind': 'anniversary', 'title': 'Год с первого визита', 'date': когда.isoformat(),
                    'in_days': дней, 'auto': False})
    for z in заметки:
        if z['kind'] == 'occasion' and z.get('occasion_date'):
            когда, дней = через(date.fromisoformat(z['occasion_date']))
            out.append({'kind': 'note', 'title': z['text'], 'date': когда.isoformat(), 'in_days': дней,
                        'auto': False, 'note_id': z['id']})
    return sorted(out, key=lambda x: x['in_days'])


def ждёт_ответа(client, cb_ids):
    from apps.tenant.branch.models import TestimonialConversation, TestimonialMessage
    conv = (TestimonialConversation.objects.filter(Q(client_id__in=cb_ids) | Q(vk_guest_id=client.pk))
            .order_by('-last_message_at').first())
    if not conv:
        return None
    last = conv.messages.order_by('-created_at').first()
    if not last or last.source == TestimonialMessage.Source.ADMIN_REPLY or conv.is_replied:
        return None
    return {'conversation_id': conv.pk, 'since': last.created_at.isoformat(), 'text': (last.text or '')[:200],
            'hours': round((timezone.now() - last.created_at).total_seconds() / 3600, 1)}


def лучший_канал(client):
    from apps.shared.guest.models import GuestChannel
    tg = GuestChannel.objects.filter(client=client, kind='tg').exclude(can_message=False).exists()
    if tg:
        return 'tg'
    if getattr(client, 'is_synthetic_vk', False):
        return None
    return 'vk'


def возвращавший_подарок(cb_ids):
    """Подарок, после которого гость приходил в течение 14 дней (последний такой)."""
    from apps.tenant.branch.models import ClientBranchVisit
    from apps.tenant.inventory.models import InventoryItem
    визиты = list(ClientBranchVisit.objects.filter(client_id__in=cb_ids).values_list('visited_at', flat=True)[:500])
    for i in InventoryItem.objects.filter(client_branch_id__in=cb_ids).select_related('product', 'catalog_item').order_by('-created_at')[:20]:
        if any(i.created_at < v <= i.created_at + timedelta(days=14) for v in визиты):
            return {'title': str(i.catalog_item or i.product or 'Подарок')[:120],
                    'catalog_item_id': i.catalog_item_id}
    return None


def что_сделать(*, ждёт, прогноз_, поводы_, канал, подарок):
    if ждёт:
        return {'action': 'reply', 'title': f'Гость ждёт ответа {ждёт["hours"]:g} ч — ответить',
                'why': f'«{ждёт["text"][:120]}»', 'channel': канал}
    др = next((p for p in (поводы_ or []) if p['kind'] == 'birthday' and p['in_days'] <= 7), None)
    if др and not др['auto']:
        return {'action': 'gift', 'title': f'День рождения через {др["in_days"]} дн. — поздравить с подарком',
                'why': 'автопоздравление с ДР в сети выключено', 'channel': канал, 'gift': подарок}
    if прогноз_ and прогноз_.get('enough') and прогноз_.get('cooling') and прогноз_.get('visits', 0) >= 3:
        what = f'«{подарок["title"]}»' if подарок else 'подарок'
        return {'action': 'gift', 'title': f'Гость остывает — отправить {what}',
                'why': (f'не был {прогноз_["days_since"]} дн. при обычных {прогноз_["usual_gap_days"]}; '
                        f'риск ухода {прогноз_["churn_risk_pct"]}%'
                        + ('; этот подарок уже возвращал гостя' if подарок else '')),
                'channel': канал, 'gift': подарок}
    return None


def лента(client, cbs, cb_ids, заметки, вкладки):
    from apps.tenant.branch.models import ClientBranchVisit, TestimonialConversation, TestimonialMessage
    точка = {cb.pk: (cb.branch.name if cb.branch_id else '') for cb in cbs}
    out = []
    for v in ClientBranchVisit.objects.filter(client_id__in=cb_ids).order_by('-visited_at')[:20]:
        out.append({'at': v.visited_at.isoformat(), 'kind': 'visit', 'title': f'Визит · {точка.get(v.client_id, "")}'})
    for o in (вкладки or {}).get('orders') or []:
        out.append({'at': o['at'], 'kind': 'order', 'title': ('Чек по QR' if o['source'] == 'receipt' else 'Заказ по карте')
                    + (f' · {o["branch_name"]}' if o.get('branch_name') else ''), 'amount': o.get('amount')})
    for p in (вкладки or {}).get('prizes') or []:
        out.append({'at': p['received_at'], 'kind': 'gift', 'title': f'Подарок «{p["title"]}»',
                    'sub': 'забрал' if p.get('used_at') else 'активировал' if p.get('activated_at') else 'ждёт'})
    for b in (вкладки or {}).get('broadcasts') or []:
        if b.get('sent_at'):
            out.append({'at': b['sent_at'], 'kind': 'broadcast', 'title': f'Рассылка «{b["name"]}»',
                        'sub': 'прочитал' if b.get('read_at') else b.get('status', '')})
    convs = list(TestimonialConversation.objects.filter(Q(client_id__in=cb_ids) | Q(vk_guest_id=client.pk))
                 .values_list('pk', flat=True)[:20])
    for m in TestimonialMessage.objects.filter(conversation_id__in=convs).order_by('-created_at')[:30]:
        от_нас = m.source == TestimonialMessage.Source.ADMIN_REPLY
        out.append({'at': m.created_at.isoformat(), 'kind': 'reply' if от_нас else 'message',
                    'title': ('Мы: ' if от_нас else 'Гость: ') + (m.text or '')[:160],
                    'sub': ('оценка ' + str(m.rating) + '★') if m.rating else '',
                    'conversation_id': m.conversation_id})
    for z in заметки:
        out.append({'at': z['created_at'], 'kind': 'note' if z['kind'] != 'message' else 'message_out',
                    'title': ('Написали гостю: ' if z['kind'] == 'message' else 'Заметка: ') + z['text'],
                    'sub': z.get('author_name', '')})
    out.sort(key=lambda x: x['at'], reverse=True)
    return out[:ЛЕНТА]


def заметки_гостя(client):
    from apps.tenant.branch.models import GuestNote
    return [{'id': n.pk, 'kind': n.kind, 'text': n.text, 'author_name': n.author_name,
             'occasion_date': n.occasion_date.isoformat() if n.occasion_date else None,
             'branch_id': n.branch_id, 'created_at': n.created_at.isoformat()}
            for n in GuestNote.objects.filter(client=client, is_active=True).order_by('-created_at')[:100]]


def собрать(client, cbs, вкладки=None):
    cb_ids = [cb.pk for cb in cbs]
    заметки = _безопасно('notes', заметки_гостя, client) or []
    средний = ((вкладки or {}).get('order_stats') or {}).get('avg_check')
    прогноз_ = _безопасно('forecast', прогноз, cb_ids, средний_чек=средний)
    поводы_ = _безопасно('occasions', поводы, client, cbs, заметки)
    ждёт = _безопасно('waiting', ждёт_ответа, client, cb_ids)
    канал = _безопасно('channel', лучший_канал, client)
    подарок = _безопасно('gift', возвращавший_подарок, cb_ids)
    сотрудник = any(getattr(cb, 'is_employee', False) for cb in cbs)
    return {
        'is_employee': сотрудник,
        'next_action': None if сотрудник else _безопасно('action', что_сделать, ждёт=ждёт, прогноз_=прогноз_, поводы_=поводы_,
                                  канал=канал, подарок=подарок),
        'forecast': прогноз_,
        'tastes': _безопасно('tastes', вкусы, cb_ids, cbs, заметки),
        'roi': _безопасно('roi', окупаемость, client, cb_ids),
        'occasions': поводы_,
        'waiting_reply': ждёт,
        'best_channel': канал,
        'notes': заметки,
        'timeline': _безопасно('timeline', лента, client, cbs, cb_ids, заметки, вкладки) or [],
    }
