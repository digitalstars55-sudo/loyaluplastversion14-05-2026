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

18.10.2026, «на 100 % как в макете»: сводка для шести плиток, каналы и
согласия со скоростью прочтения, RF-оценка, карта iikoCard, ждущие подарки,
«возможно, тот же человек», жалобы и отзывы в ленте, заказы с номером iiko —
по нему CheckUp подставляет блюда, стол и официанта (`pos_orders`). Уровней
лояльности и сгорания баллов в сети нет — их и не показываем.
"""
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import median

from collections import Counter

from django.core.cache import cache
from django.db import connection
from django.db.models import Q, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

ДНИ = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']
ЛЕНТА = 150


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
    часы = []
    точка = {cb.pk: (cb.branch.name if cb.branch_id else '') for cb in cbs}
    for when, cb_id in визиты:
        t = timezone.localtime(when)
        по_дням[t.weekday()] = по_дням.get(t.weekday(), 0) + 1
        полоса = 'утром' if t.hour < 12 else 'днём' if t.hour < 17 else 'вечером'
        по_часам[полоса] = по_часам.get(полоса, 0) + 1
        часы.append(t.hour)
        по_точкам[точка.get(cb_id, '')] = по_точкам.get(точка.get(cb_id, ''), 0) + 1
    дни = [ДНИ[d] for d, _ in sorted(по_дням.items(), key=lambda x: -x[1])[:2]] if визиты else []
    return {
        'days': дни,
        'time_of_day': max(по_часам, key=по_часам.get) if по_часам else None,
        'branches': [{'name': n, 'visits': c} for n, c in sorted(по_точкам.items(), key=lambda x: -x[1]) if n][:3],
        'allergies': [z['text'] for z in заметки if z['kind'] == 'allergy'],
        'notes': [z for z in заметки if z['kind'] == 'note'][:5],
        # Обычный час прихода: нижняя четверть часов визитов — «после 19:00».
        'usual_hour': sorted(часы)[len(часы) // 4] if len(часы) >= 3 else None,
        # Блюда, стол и официант — из CheckUp по номерам заказов iiko (pos_orders).
        'favorite_dishes': None,
        'dishes_reason': 'блюда видны по заказам, закрытым по карте гостя iikoCard; чек по QR отдаёт только сумму',
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
    from apps.tenant.branch.models import CoinTransaction, TransactionType
    списано = abs(CoinTransaction.objects.filter(client_id__in=cb_ids, type=TransactionType.EXPENSE)
                  .aggregate(s=Sum('amount'))['s'] or 0)
    return {
        'points_spent': int(списано),
        'revenue_rub': float(принесла), 'gifts_count': len(подарки),
        'gifts_cost_rub': float(вложено), 'apology_cost_rub': float(извинения),
        'ratio': round(float(принесла / вложено), 1) if вложено > 0 else None,
        'has_revenue_data': принесла > 0,
    }


def _флаг(имя) -> bool:
    try:
        cfg = getattr(getattr(connection, 'tenant', None), 'config', None)
        return bool(getattr(cfg, имя, False)) if cfg is not None else False
    except Exception:  # noqa: BLE001 — настройки сети не прочитались: считаем выключенным
        logger.warning('guest360: флаг %s не прочитался', имя, exc_info=True)
        return False


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
                    'in_days': дней, 'auto': _флаг('guest_occasions_auto_enabled')})
    for z in заметки:
        if z['kind'] == 'occasion' and z.get('occasion_date'):
            когда, дней = через(date.fromisoformat(z['occasion_date']))
            out.append({'kind': 'note', 'title': z['text'], 'date': когда.isoformat(), 'in_days': дней,
                        'auto': _флаг('guest_occasions_auto_enabled'), 'note_id': z['id']})
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
    черновик = '' if getattr(conv, 'ai_draft_rejected', False) else (getattr(conv, 'ai_draft', '') or '')
    return {'conversation_id': conv.pk, 'since': last.created_at.isoformat(), 'text': (last.text or '')[:200],
            'hours': round((timezone.now() - last.created_at).total_seconds() / 3600, 1),
            # Черновик ответа, который уже подготовил ИИ отзывов (база знаний сети).
            'draft': черновик[:800]}


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
                    'catalog_item_id': i.catalog_item_id, 'returned_at': i.created_at.isoformat()}
    return None


def _текст_подарка(client, подарок):
    имя = client.first_name or 'Здравствуйте'
    что = f'подарок «{подарок["title"]}»' if подарок else 'подарок'
    return f'{имя}, мы соскучились! Приготовили для вас {что} — он уже в «Моих подарках», ждём вас.'


def что_сделать(*, ждёт, прогноз_, поводы_, канал, подарок, client=None, каналы_=None, средний_чек=None):
    скорость = next((c.get('read_hint') for c in (каналы_ or []) if c.get('kind') == канал and c.get('read_hint')), '')
    if ждёт:
        return {'action': 'reply', 'title': f'Гость ждёт ответа {ждёт["hours"]:g} ч — ответить',
                'why': f'«{ждёт["text"][:120]}»', 'channel': канал, 'read_hint': скорость}
    др = next((p for p in (поводы_ or []) if p['kind'] == 'birthday' and p['in_days'] <= 7), None)
    if др and not др['auto']:
        return {'action': 'gift', 'title': f'День рождения через {др["in_days"]} дн. — поздравить с подарком',
                'why': 'автопоздравление с ДР в сети выключено', 'channel': канал, 'gift': подарок,
                'read_hint': скорость, 'draft_text': _текст_подарка(client, подарок) if client else ''}
    if прогноз_ and прогноз_.get('enough') and прогноз_.get('cooling') and прогноз_.get('visits', 0) >= 3:
        what = f'«{подарок["title"]}»' if подарок else 'подарок'
        месяц = None
        if средний_чек and прогноз_.get('usual_gap_days'):
            месяц = int(round(float(средний_чек) * max(1.0, 30 / прогноз_['usual_gap_days']), -2))
        return {'action': 'gift', 'title': f'Гость остывает — отправить {what}',
                'why': (f'не был {прогноз_["days_since"]} дн. при обычных {прогноз_["usual_gap_days"]}; '
                        f'риск ухода {прогноз_["churn_risk_pct"]}%'
                        + ('; этот подарок уже возвращал гостя' if подарок else '')),
                'channel': канал, 'gift': подарок, 'read_hint': скорость,
                'risk_pct': прогноз_['churn_risk_pct'], 'month_value_rub': месяц,
                'gift_returned_at': (подарок or {}).get('returned_at'),
                'draft_text': _текст_подарка(client, подарок) if client else ''}
    return None


def _коротко(name: str) -> str:
    """«Levone | Ленина» → «Ленина»: в карточке сети имя сети лишнее."""
    return (name or '').split('|')[-1].strip()


def _жалобы_и_отзывы(client, cbs, cb_ids):
    """Оценённые сообщения гостя: ≤3★ — жалоба, иначе отзыв. С ответом и исходом."""
    from apps.tenant.branch.models import TestimonialConversation, TestimonialMessage
    from apps.tenant.inventory.models import AcquisitionSource, InventoryItem
    извинения = list(InventoryItem.objects.filter(client_branch_id__in=cb_ids, acquired_from=AcquisitionSource.APOLOGY)
                     .select_related('product', 'catalog_item').values_list('created_at', 'product__name'))
    out = []
    convs = {c.pk: c for c in TestimonialConversation.objects.filter(Q(client_id__in=cb_ids) | Q(vk_guest_id=client.pk))
             .select_related('branch')[:30]}
    for m in (TestimonialMessage.objects.filter(conversation_id__in=list(convs), rating__isnull=False)
              .exclude(source=TestimonialMessage.Source.ADMIN_REPLY).order_by('-created_at')[:20]):
        conv = convs[m.conversation_id]
        ответ = (TestimonialMessage.objects.filter(conversation_id=m.conversation_id, created_at__gt=m.created_at,
                                                    source=TestimonialMessage.Source.ADMIN_REPLY)
                 .order_by('created_at').values_list('created_at', flat=True).first())
        части = []
        if ответ:
            мин = int((ответ - m.created_at).total_seconds() // 60)
            части.append(f'ответ сообщества за {мин} мин' if мин < 120 else f'ответ сообщества через {мин // 60} ч')
        elif m.rating <= 3:
            части.append('без ответа')
        подарок = next((n for t, n in извинения if t >= m.created_at and t <= m.created_at + timedelta(days=3)), None)
        if подарок:
            части.append(f'извинились подарком «{подарок}»')
        if conv.resolved_at or conv.checkup_resolved_at:
            части.append('решено')
        точка = _коротко(conv.branch.name) if conv.branch_id else ''
        жалоба = m.rating <= 3
        out.append({'at': m.created_at.isoformat(), 'kind': 'complaint' if жалоба else 'review', 'channel': 'vk',
                    'title': (('Жалоба: «' if жалоба else 'Отзыв: «') + (m.text or '')[:120] + '»'
                              + (f' · {точка}' if точка else '')),
                    'sub': ' · '.join(части), 'rating': m.rating, 'conversation_id': m.conversation_id,
                    'message_id': m.pk})
    return out


def _скриншоты_отзывов(cb_ids):
    from apps.tenant.branch.models import ReviewProof
    out = []
    for r in ReviewProof.objects.filter(client_id__in=cb_ids).order_by('-created_at')[:10]:
        где = r.get_platform_display() if hasattr(r, 'get_platform_display') else r.platform
        статус = r.get_status_display() if hasattr(r, 'get_status_display') else r.status
        out.append({'at': r.created_at.isoformat(), 'kind': 'review', 'channel': 'vk',
                    'title': f'Отзыв на картах · {где} · скриншот: {статус.lower()}',
                    'sub': f'+{r.coins} баллов' if r.coins else ''})
    return out


def лента(client, cbs, cb_ids, заметки, вкладки):
    from apps.tenant.branch.models import ClientBranchVisit, TestimonialConversation, TestimonialMessage
    точка = {cb.pk: _коротко(cb.branch.name if cb.branch_id else '') for cb in cbs}
    канал_рассылок = 'tg' if getattr(client, 'is_synthetic_vk', False) else 'vk'
    out = []
    заказы = list((вкладки or {}).get('orders') or [])
    дни_заказов = {(o['at'][:10], _коротко(o.get('branch_name') or '')) for o in заказы}
    визиты = list(ClientBranchVisit.objects.filter(client_id__in=cb_ids).order_by('-visited_at')
                  .values_list('visited_at', 'client_id')[:60])
    дни_визитов = [timezone.localtime(v) for v, _ in визиты]
    for (v, cb_id), местное in zip(визиты, дни_визитов):
        # Визит в день, когда есть чек или заказ этой точки, — одна строка «визит + чек».
        if (местное.date().isoformat(), точка.get(cb_id, '')) in дни_заказов:
            continue
        out.append({'at': v.isoformat(), 'kind': 'visit', 'title': f'Визит · {точка.get(cb_id, "")} · скан QR'})
    for o in заказы:
        где = _коротко(o.get('branch_name') or '')
        вид = 'чек по QR' if o['source'] == 'receipt' else 'заказ по карте iikoCard'
        out.append({'at': o['at'], 'kind': 'order', 'title': f'Визит · {где} · {вид}' if где else f'Покупка · {вид}',
                    'sub': f'+{o["coins"]} баллов' if o.get('coins') else '', 'amount': o.get('amount'),
                    'pos_number': o.get('order_number') or '', 'pos_org': o.get('organization_id') or ''})
    for p in (вкладки or {}).get('prizes') or []:
        out.append({'at': p['received_at'], 'kind': 'gift', 'title': f'Подарок «{p["title"]}»',
                    'sub': 'забрал' if p.get('used_at') else 'активировал' if p.get('activated_at') else 'ждёт'})
    for b in (вкладки or {}).get('broadcasts') or []:
        if not b.get('sent_at'):
            continue
        отправлено = datetime.fromisoformat(b['sent_at'])
        пришёл = next((d for d in дни_визитов if отправлено < d <= отправлено + timedelta(days=14)), None)
        if b.get('read_at'):
            исход = ('прочитал' + (f', пришёл через {max(0, (пришёл - отправлено).days)} дн.' if пришёл else ', не пришёл'))
        else:
            исход = 'не прочитал' if b.get('status') in ('sent', 'delivered', '') else b.get('status', '')
        out.append({'at': b['sent_at'], 'kind': 'broadcast', 'channel': канал_рассылок,
                    'title': f'Рассылка «{b["name"]}»' + (' · автоматическая' if b.get('kind') == 'auto' else ''),
                    'sub': исход, 'came': bool(пришёл)})
    convs = list(TestimonialConversation.objects.filter(Q(client_id__in=cb_ids) | Q(vk_guest_id=client.pk))
                 .values_list('pk', flat=True)[:20])
    for m in TestimonialMessage.objects.filter(conversation_id__in=convs, rating__isnull=True).order_by('-created_at')[:40]:
        от_нас = m.source == TestimonialMessage.Source.ADMIN_REPLY
        out.append({'at': m.created_at.isoformat(), 'kind': 'reply' if от_нас else 'message', 'channel': 'vk',
                    'title': ('Мы: ' if от_нас else 'Гость: ') + (m.text or '')[:160],
                    'sub': ('прочитал' if от_нас and m.read_at else ''),
                    'conversation_id': m.conversation_id})
    out += _безопасно('complaints', _жалобы_и_отзывы, client, cbs, cb_ids) or []
    out += _безопасно('review_proofs', _скриншоты_отзывов, cb_ids) or []
    for z in заметки:
        if z['kind'] in ('not_same',):
            continue
        вид = {'message': 'message_out', 'greeted': 'message_out'}.get(z['kind'], 'note')
        подпись = {'message': 'Написали гостю: ', 'greeted': 'Поздравили: ', 'allergy': 'Нельзя: ',
                   'occasion': 'Повод: '}.get(z['kind'], 'Заметка: ')
        out.append({'at': z['created_at'], 'kind': вид, 'title': подпись + z['text'],
                    'sub': z.get('author_name', '')})
    out.sort(key=lambda x: x['at'], reverse=True)
    return out[:ЛЕНТА]


def заметки_гостя(client):
    from apps.tenant.branch.models import GuestNote
    return [{'id': n.pk, 'kind': n.kind, 'text': n.text, 'author_name': n.author_name,
             'occasion_date': n.occasion_date.isoformat() if n.occasion_date else None,
             'branch_id': n.branch_id, 'created_at': n.created_at.isoformat()}
            for n in GuestNote.objects.filter(client=client, is_active=True).order_by('-created_at')[:100]]


def _средний_чек_сети() -> float | None:
    """Средний чек сети за 180 дней (чеки по QR + заказы по карте). Кэш 6 часов."""
    from apps.tenant.analytics.models import PosGuestOrder
    from apps.tenant.branch.models import ReceiptScan, ReceiptScanStatus
    ключ = f'g360:netavg:{getattr(connection, "schema_name", "")}'
    v = cache.get(ключ)
    if v is not None:
        return v or None
    с = timezone.now() - timedelta(days=180)
    суммы = [float(x) for x in ReceiptScan.objects.filter(status=ReceiptScanStatus.CREDITED, receipt_at__gte=с)
             .values_list('amount', flat=True)[:20000]]
    суммы += [float(p if p is not None else o or 0) for p, o in PosGuestOrder.objects.filter(closed_at__gte=с)
              .values_list('paid_sum', 'order_sum')[:20000]]
    v = round(sum(суммы) / len(суммы)) if суммы else 0
    cache.set(ключ, v, 6 * 3600)
    return v or None


def сводка(client, cbs, cb_ids, вкладки, roi):
    """Шесть плиток: визиты, потрачено, средний чек к сети, последний визит.

    Визит — день, когда гость был на точке: скан QR или чек/заказ по карте
    (заказы iiko по карте бывают и без скана — их тоже считаем).
    """
    from apps.tenant.branch.models import ClientBranchVisit
    точка = {cb.pk: _коротко(cb.branch.name if cb.branch_id else '') for cb in cbs}
    заказы = list((вкладки or {}).get('orders') or [])
    дни = {}
    for when, cb_id in ClientBranchVisit.objects.filter(client_id__in=cb_ids).values_list('visited_at', 'client_id')[:5000]:
        t = timezone.localtime(when)
        дни.setdefault((t.date(), точка.get(cb_id, '')), t)
    for o in заказы:
        t = timezone.localtime(datetime.fromisoformat(o['at']))
        где = _коротко(o.get('branch_name') or '')
        if not any(d == t.date() for d, _ in дни):
            дни.setdefault((t.date(), где), t)
    по_точкам = Counter(где for (_, где) in дни if где)
    stats = (вкладки or {}).get('order_stats') or {}
    средний = stats.get('avg_check')
    сеть = _средний_чек_сети()
    last = None
    if дни:
        (день, где), t = max(дни.items(), key=lambda x: x[1])
        сумма = sum(o['amount'] or 0 for o in заказы
                    if timezone.localtime(datetime.fromisoformat(o['at'])).date() == день) or None
        last = {'at': t.isoformat(), 'branch': где, 'amount': сумма}
    return {
        'visits': len({d for d, _ in дни}), 'branches_count': len(по_точкам),
        'top_branch': по_точкам.most_common(1)[0][0] if по_точкам else '',
        'spent_rub': (roi or {}).get('revenue_rub'), 'orders_total': stats.get('orders_total') or 0,
        'avg_check': средний, 'network_avg_check': сеть,
        'avg_vs_network_pct': round((средний / сеть - 1) * 100) if средний and сеть else None,
        'last_visit': last,
    }


def _маска(phone: str) -> str:
    цифры = ''.join(ch for ch in (phone or '') if ch.isdigit())
    if len(цифры) < 10:
        return phone or ''
    ц = цифры[-10:]
    return f'+7 {ц[:3]} ··· {ц[6:8]} {ц[8:]}'


def _минуты_словами(мин: float) -> str:
    if мин < 60:
        return f'{max(1, round(мин))} мин'
    if мин < 48 * 60:
        return f'{round(мин / 60)} ч'
    return f'{round(мин / 1440)} дн.'


def каналы_гостя(client, cbs):
    """Каналы и согласия: Телеграм, ВКонтакте, телефон, MAX. Скорость прочтения — по рассылкам ВК."""
    from apps.shared.guest.models import GuestChannel
    from apps.tenant.branch.models import ClientVKStatus
    from apps.tenant.senler.models import BroadcastRecipient
    tg = GuestChannel.objects.filter(client=client, kind='tg').order_by('-last_seen').first()
    статусы = list(ClientVKStatus.objects.filter(client__in=cbs))
    рассылка = any(x.is_newsletter_subscriber for x in статусы)
    сообщество = any(x.is_community_member for x in статусы)
    задержки = [(r - s_).total_seconds() / 60 for s_, r in BroadcastRecipient.objects.filter(
        client_branch__in=cbs, sent_at__isnull=False, read_at__isnull=False).order_by('-id')
        .values_list('sent_at', 'read_at')[:30] if r >= s_]
    vk_read = median(задержки) if len(задержки) >= 2 else None
    out = []
    if tg:
        можно = tg.can_message is not False
        out.append({'kind': 'tg', 'state': 'ok' if можно else 'blocked', 'username': tg.username or '',
                    'label': ('пишем' if можно else 'гость закрыл бота'), 'read_hint': ''})
    else:
        out.append({'kind': 'tg', 'state': 'none', 'label': 'не открывал бота', 'read_hint': ''})
    if getattr(client, 'is_synthetic_vk', False):
        out.append({'kind': 'vk', 'state': 'none', 'label': 'ВК не привязан', 'read_hint': ''})
    else:
        хинт = f'читает за {_минуты_словами(vk_read)}' if vk_read is not None else ''
        out.append({'kind': 'vk', 'state': 'ok' if рассылка else 'member' if сообщество else 'no',
                    'vk_id': client.vk_id,
                    'label': 'сообщения разрешены' if рассылка else 'в сообществе, сообщения не разрешал' if сообщество
                    else 'сообщения не разрешал', 'read_hint': хинт})
    if client.phone:
        доказан = (client.phone_source or '') == 'vk'
        out.append({'kind': 'phone', 'state': 'ok' if client.phone_consent_at else 'no_consent',
                    'masked': _маска(client.phone), 'proven': доказан,
                    'consent_at': client.phone_consent_at.isoformat() if client.phone_consent_at else None,
                    'label': ''})
    else:
        out.append({'kind': 'phone', 'state': 'none', 'label': 'не оставлял', 'read_hint': ''})
    out.append({'kind': 'max', 'state': 'none', 'label': 'нет', 'read_hint': ''})
    return out


def rf_оценка(client):
    from apps.tenant.analytics.models import GuestRFScore
    r = GuestRFScore.objects.filter(client=client).select_related('segment').first()
    if r is None:
        return None
    return {'r': r.r_score, 'f': r.f_score, 'segment': r.segment.name if r.segment_id else '',
            'segment_code': r.segment.code if r.segment_id else ''}


def карта_iiko(client):
    from apps.tenant.analytics.models import PosGuestLink
    link = PosGuestLink.objects.filter(client=client).first()
    if link is None or not (link.card_number or link.iiko_customer_id):
        return None
    номер = link.card_number or ''
    return {'card': (f'{номер[:4]} {номер[-4:]}' if len(номер) > 8 else номер), 'status': link.status}


def подарки_сейчас(cb_ids):
    """Подарки, которые ждут гостя: не использованы и не истекли."""
    from apps.tenant.inventory.models import InventoryItem
    сейчас = timezone.now()
    out = []
    for i in (InventoryItem.objects.filter(client_branch_id__in=cb_ids, used_at__isnull=True)
              .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=сейчас))
              .filter(Q(claim_expires_at__isnull=True) | Q(claim_expires_at__gt=сейчас))
              .select_related('product', 'catalog_item').order_by('-created_at')[:5]):
        до = i.expires_at or i.claim_expires_at
        out.append({'title': str(i.catalog_item or i.product or i.description or 'Подарок')[:120],
                    'until': до.isoformat() if до else None, 'activated': bool(i.activated_at)})
    return out


def похожие(client, заметки):
    """«Возможно, тот же человек»: живая карточка с тем же телефоном (кроме отклонённых)."""
    from apps.shared.guest.models import Client
    if not client.phone:
        return []
    нет = {z['text'] for z in заметки if z['kind'] == 'not_same'}
    out = []
    for c in (Client.objects.filter(phone=client.phone, merged_into__isnull=True, is_active=True)
              .exclude(pk=client.pk).order_by('-id')[:3]):
        if str(c.vk_id) in нет:
            continue
        имя = ' '.join(x for x in (c.first_name, (c.last_name or '')[:1] + ('.' if c.last_name else '')) if x).strip()
        out.append({'vk_id': c.vk_id, 'name': имя or 'Без имени', 'is_synthetic': c.is_synthetic_vk,
                    'why': 'тот же номер телефона' + (', пришла из бота Телеграма' if c.is_synthetic_vk else ''),
                    'created_at': getattr(c, 'created_at', None) and c.created_at.isoformat()})
    return out


def заказы_iiko(client):
    """Заказы по карте гостя с номером iiko — CheckUp находит по ним блюда, стол, официанта."""
    from apps.tenant.analytics.models import PosGuestOrder
    return [{'number': o.order_number, 'org': o.organization_id, 'closed_at': o.closed_at.isoformat(),
             'branch_id': o.branch_id}
            for o in PosGuestOrder.objects.filter(link__client=client).exclude(order_number='')
            .order_by('-closed_at')[:100]]


def возраст(cbs, сегодня=None):
    сегодня = сегодня or timezone.localdate()
    др = next((cb.birth_date for cb in cbs if cb.birth_date), None)
    if not др or др.year < 1920:
        return None
    return сегодня.year - др.year - ((сегодня.month, сегодня.day) < (др.month, др.day))


def собрать(client, cbs, вкладки=None):
    cb_ids = [cb.pk for cb in cbs]
    заметки = _безопасно('notes', заметки_гостя, client) or []
    средний = ((вкладки or {}).get('order_stats') or {}).get('avg_check')
    прогноз_ = _безопасно('forecast', прогноз, cb_ids, средний_чек=средний)
    поводы_ = _безопасно('occasions', поводы, client, cbs, заметки)
    ждёт = _безопасно('waiting', ждёт_ответа, client, cb_ids)
    канал = _безопасно('channel', лучший_канал, client)
    подарок = _безопасно('gift', возвращавший_подарок, cb_ids)
    каналы_ = _безопасно('channels', каналы_гостя, client, cbs) or []
    roi = _безопасно('roi', окупаемость, client, cb_ids)
    сотрудник = any(getattr(cb, 'is_employee', False) for cb in cbs)
    # «Гость с …» — самое раннее, что о нём известно: карточка в сети или первый
    # заказ по карте iikoCard (заказы приходят из истории iiko и бывают раньше карточки).
    первый = min((cb.created_at for cb in cbs), default=None)
    заказы_ = _безопасно('pos_orders', заказы_iiko, client) or []
    if заказы_:
        ранний = datetime.fromisoformat(заказы_[-1]['closed_at'])
        первый = min(первый, ранний) if первый else ранний
    return {
        'is_employee': сотрудник,
        'gender': getattr(client, 'gender', '') or '',
        'since': первый.isoformat() if первый else None,
        'age': _безопасно('age', возраст, cbs),
        'next_action': None if сотрудник else _безопасно('action', что_сделать, ждёт=ждёт, прогноз_=прогноз_, поводы_=поводы_,
                                  канал=канал, подарок=подарок, client=client, каналы_=каналы_,
                                  средний_чек=средний),
        'forecast': прогноз_,
        'tastes': _безопасно('tastes', вкусы, cb_ids, cbs, заметки),
        'roi': roi,
        'summary': _безопасно('summary', сводка, client, cbs, cb_ids, вкладки, roi),
        'channels': каналы_,
        'rf': _безопасно('rf', rf_оценка, client),
        'iiko_card': _безопасно('iiko_card', карта_iiko, client),
        'gifts_waiting': _безопасно('gifts_waiting', подарки_сейчас, cb_ids) or [],
        'same_person': _безопасно('same_person', похожие, client, заметки) or [],
        'pos_orders': заказы_,
        'waiter_hint_enabled': _флаг('guest_waiter_hint_enabled'),
        'occasions_auto_enabled': _флаг('guest_occasions_auto_enabled'),
        'occasions': поводы_,
        'waiting_reply': ждёт,
        'best_channel': канал,
        'notes': [z for z in заметки if z['kind'] != 'not_same'],
        'timeline': _безопасно('timeline', лента, client, cbs, cb_ids, заметки, вкладки) or [],
    }
