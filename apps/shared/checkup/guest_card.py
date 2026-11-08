"""
GET /api/v1/internal/checkup/guest-card/ — гость сети по номеру для карточки звонящего CheckUp (08.11.2026).

Зачем. Владелец: «баллы LoyalUP в карточке звонящего делай». Когда гость звонит
на точку, стол хостес CheckUp показывает карточку: имя, визиты, брони, жалобы —
всё, что знает CheckUp. Баллы, подарки и день рождения живут здесь. ИИ-хостес
CheckUp получает ту же сводку, но баллы вслух не называет (её правило речи).

Что в ответе и почему так:
  • `name` — имя гостя (из ВК), хостес здоровается по имени; от фамилии — одна буква;
  • `points` — баллы: сумма по точкам сети, и по каждой точке (баллы копятся на точке);
  • визиты — дни со сканом QR, последний визит и точка;
  • подарки, которые ждут гостя (не использованы, не истекли) — до трёх названий;
  • `birth_date` — день рождения строкой 'ММ-ДД', без года;
  • `is_employee` — звонит свой сотрудник, CheckUp не путает его с гостем;
  • `same_phone` — сколько ещё гостей сети с этим же номером (дубли ВК).
Телефона, VK ID, фото и фамилии полностью в ответе нет; номер не пишется в журнал.

Доступ — как у выгрузки сканов: внутренний адрес → включено → секрет → сеть из
белого списка. Секрет СВОЙ (`CHECKUP_GUEST_CARD_SECRET`, заголовок
`X-LoyalUP-Guest-Secret`): контракт 4.15 — секреты не переиспользуются; эта
ручка про конкретного человека по номеру, и утечка секрета сканов не должна её
открывать. Пустой секрет = ручка выключена (503). Сети — `CHECKUP_GUEST_CARD_TENANTS`;
пустой список = ни одной. Ручка только читает.
"""
from __future__ import annotations

import datetime
import hmac
import logging
import re

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django_tenants.utils import get_tenant_model, schema_context

from apps.shared.relay.views import _is_internal_request

from .scans_export import ExportError, _SCHEMA_RE

log = logging.getLogger(__name__)

ПОДАРКОВ = 3
# Свой заголовок, не как у выгрузки сканов: клиент CheckUp (apps/booking/loyalup_guest.py) шлёт его.
SECRET_HEADER = 'X-LoyalUP-Guest-Secret'


# ── настройки ────────────────────────────────────────────────────────────────

def card_secret() -> str:
    return str(getattr(settings, 'CHECKUP_GUEST_CARD_SECRET', '') or '')


def card_enabled() -> bool:
    return bool(card_secret())


def secret_matches(provided: str) -> bool:
    expected = card_secret()
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(provided), expected)


def allowed_tenants() -> list[str]:
    """Белый список сетей. Пустой = ни одной."""
    raw = getattr(settings, 'CHECKUP_GUEST_CARD_TENANTS', None) or []
    if isinstance(raw, str):
        raw = raw.split(',')
    return [s.strip().lower() for s in raw if s and s.strip()]


def resolve_tenant(schema: str):
    if schema not in allowed_tenants():
        raise ExportError(403, 'tenant_not_allowed',
                          f'карточка гостя для сети {schema} не включена (CHECKUP_GUEST_CARD_TENANTS)')
    Tenant = get_tenant_model()
    tenant = Tenant.objects.filter(schema_name=schema).first()
    if tenant is None or not getattr(tenant, 'is_active', True):
        raise ExportError(404, 'tenant_not_found', f'сети {schema} нет или она выключена')
    return tenant


# ── разбор запроса (чистая часть) ────────────────────────────────────────────

def phone_variants(raw: str) -> list[str]:
    """Российский номер в виде, как его хранит гость (`+7…`), и без плюса. Не номер — пусто."""
    text = str(raw or '').strip()
    digits = re.sub(r'\D', '', text)
    if text.startswith('+') and not (len(digits) == 11 and digits[0] == '7'):
        return []                      # с плюсом — только +7 и десять цифр
    if len(digits) == 10:
        digits = '7' + digits
    elif len(digits) == 11 and digits[0] == '8':
        digits = '7' + digits[1:]
    if len(digits) != 11 or digits[0] != '7':
        return []
    return ['+' + digits, digits]


def parse_query(params) -> dict:
    schema = str(params.get('tenant_schema') or '').strip().lower()
    if not schema or schema == 'public' or not _SCHEMA_RE.match(schema):
        raise ExportError(400, 'invalid_query', 'tenant_schema: имя сети LoyalUP')
    phones = phone_variants(params.get('phone'))
    if not phones:
        raise ExportError(400, 'invalid_query', 'phone: российский номер, 10–11 цифр')
    return {'tenant_schema': schema, 'phones': phones}


def birth_month_day(birth: datetime.date | None) -> str | None:
    """День рождения как 'ММ-ДД': CheckUp сам считает, через сколько дней. Год гостя не отдаём."""
    return birth.strftime('%m-%d') if birth else None


def pick_guest(profiles: list) -> tuple[int | None, int]:
    """Из гостей с одним номером — тот, у кого последний визит/карточка свежее. (id гостя, сколько ещё)."""
    by_guest: dict[int, datetime.datetime] = {}
    for p in profiles:
        moment = getattr(p, 'last_seen', None) or p.created_at
        if p.client_id not in by_guest or moment > by_guest[p.client_id]:
            by_guest[p.client_id] = moment
    if not by_guest:
        return None, 0
    chosen = max(by_guest, key=lambda g: by_guest[g])
    return chosen, len(by_guest) - 1


def assemble(guest, profiles: list, coins: dict, visit_days: list, last_visit, gifts: list,
             same_phone: int) -> dict:
    """Ответ ручки из уже собранного (без БД — его держат тесты)."""
    birth = next((p.birth_date for p in profiles if getattr(p, 'birth_date', None)), None)
    by_branch = [{'branch_id': getattr(p.branch, 'branch_id', None), 'name': p.branch.name,
                  'coins': int(coins.get(p.pk, 0))}
                 for p in profiles if p.branch_id]
    return {
        'found': True,
        'guest_id': guest.pk,
        'name': (guest.first_name or '').strip(),
        'last_initial': ((guest.last_name or '').strip()[:1] + '.') if (guest.last_name or '').strip() else '',
        'points': sum(b['coins'] for b in by_branch),
        'coins_by_branch': sorted(by_branch, key=lambda b: -b['coins']),
        'visits': len(visit_days),
        'last_visit': last_visit,
        'gifts_waiting': [g['title'] for g in gifts[:ПОДАРКОВ]],
        'gifts_waiting_count': len(gifts),
        'birth_date': birth_month_day(birth),
        'is_employee': any(getattr(p, 'is_employee', False) for p in profiles),
        'same_phone': same_phone,
    }


# ── сборка из базы сети ──────────────────────────────────────────────────────

def build_card(q: dict) -> dict:
    from apps.tenant.branch.guest360 import подарки_сейчас
    from apps.tenant.branch.models import ClientBranch, ClientBranchVisit

    profiles = list(ClientBranch.objects.select_related('client', 'branch')
                    .filter(client__phone__in=q['phones']))
    guest_id, same_phone = pick_guest(profiles)
    if guest_id is None:
        return {'found': False}
    mine = [p for p in profiles if p.client_id == guest_id]
    ids = [p.pk for p in mine]
    coins = {p.pk: p.coins_balance for p in mine}
    name = {p.pk: p.branch.name for p in mine if p.branch_id}
    days, last_visit = set(), None
    for when, cb_id in (ClientBranchVisit.objects.filter(client_id__in=ids)
                        .order_by('-visited_at').values_list('visited_at', 'client_id')[:2000]):
        local = timezone.localtime(when)
        days.add(local.date())
        if last_visit is None:
            last_visit = {'date': local.date().isoformat(), 'branch': name.get(cb_id, '')}
    try:
        gifts = подарки_сейчас(ids) or []
    except Exception:  # noqa: BLE001 — подарки не роняют карточку
        log.warning('guest-card: подарки не собрались', exc_info=True)
        gifts = []
    return assemble(mine[0].client, mine, coins, sorted(days), last_visit, gifts, same_phone)


# ── ручка ────────────────────────────────────────────────────────────────────

@method_decorator(csrf_exempt, name='dispatch')
class GuestCardView(View):
    """GET /api/v1/internal/checkup/guest-card/?tenant_schema=&phone="""
    http_method_names = ['get']

    def get(self, request):
        if not _is_internal_request(request):
            log.warning('guest-card: отклонён внешний запрос remote=%s xff=%s',
                        request.META.get('REMOTE_ADDR', ''), request.headers.get('X-Forwarded-For', ''))
            return JsonResponse({'code': 'forbidden', 'detail': 'только с внутреннего адреса сервера'},
                                status=403)
        if not card_enabled():
            return JsonResponse({'code': 'card_disabled',
                                 'detail': 'карточка гостя выключена (CHECKUP_GUEST_CARD_SECRET пуст)'},
                                status=503)
        if not secret_matches(request.headers.get(SECRET_HEADER, '')):
            log.warning('guest-card: неверный секрет remote=%s', request.META.get('REMOTE_ADDR', ''))
            return JsonResponse({'code': 'bad_secret', 'detail': f'неверный {SECRET_HEADER}'}, status=401)
        try:
            q = parse_query(request.GET)
            resolve_tenant(q['tenant_schema'])
            with schema_context(q['tenant_schema']):
                payload = build_card(q)
        except ExportError as e:
            log.info('guest-card: отказ %s %s (%s)', e.status, e.code, e.detail)
            return JsonResponse(e.as_dict(), status=e.status)
        log.info('guest-card: %s → %s', q['tenant_schema'], 'найден' if payload.get('found') else 'нет')
        return JsonResponse(payload, json_dumps_params={'ensure_ascii': False})
