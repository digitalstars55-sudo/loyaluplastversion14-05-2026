"""
GET /api/v1/internal/checkup/scans/ — сканы QR для CheckUp (24.09.2026).

Зачем. Владелец сети: «скан со стола → официант этого стола». QR лежит на
столе, гость сканирует — CheckUp должен понять, чей это стол в эту минуту
(заказ кассы на этом столе → официант заказа), и посчитать официанту долю
столовых заказов со сканом. Кассы у нас нет, у CheckUp нет сканов: эта ручка —
ночная выгрузка сканов для него, строка на скан.

Что в строке и почему так:
  • время скана С ЗОНОЙ (`+03:00`), ключ QR (`src`) и сам QR — режим, номер
    стола, id стола в кассе и зал (QRCode.table_*): по ним CheckUp находит
    заказ стола. Стол берётся из ЗАПИСИ QR, а не из адреса мини-аппа — адрес
    гость может поправить руками, запись нет;
  • точка — внутренний `id` и публичный `branch_id` (тот, что знает словарь
    точек CheckUp);
  • гость — ТОЛЬКО внутренний id гостя сети (`guest.Client.pk`): ни имени, ни
    телефона, ни VK ID. Его хватает на «один гость — один скан в сутки»;
  • `is_employee` — гость отмечен сотрудником хоть в одной точке сети: свой
    скан в счёт официанту не идёт;
  • действия после скана в окне `window` минут: игра (не доставочная),
    подписка на сообщество/рассылку ИЗ ПРИЛОЖЕНИЯ, отзыв из приложения — по
    первому каждого вида, со временем. Правило «с действием в течение
    30 минут» решает CheckUp, окно здесь шире (по умолчанию 60);
  • `served_by` — официант, которого гость выбрал после игры в этом окне
    (`ClientAttempt.served_by`): это сотрудник, не гость, поэтому имя и VK ID
    отдаём — по ним CheckUp узнаёт своего человека.

Доступ — как у обмена токена: внутренний адрес → включено → секрет. Секрет
СВОЙ (`CHECKUP_SCANS_EXPORT_SECRET`, заголовок `X-LoyalUP-Export-Secret`):
контракт 4.15 — секреты не переиспользуются, утечка выгрузки не должна
открывать обмен токена. Пустой секрет = ручка выключена (503). Сеть — из
белого списка `CHECKUP_SCANS_EXPORT_TENANTS`; пустой список = НИ ОДНОЙ сети
(в отличие от обмена): выгрузка про поведение гостей, и включать её надо
осознанно, по сети.

Ручка только читает. Пагинация — по id скана (`after_id`), потому что за ночь
сканы дописываются: смещение `offset` на растущей таблице теряло бы строки.
"""
from __future__ import annotations

import datetime
import hmac
import logging
import re

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django_tenants.utils import get_tenant_model, schema_context

from apps.shared.relay.views import _is_internal_request

log = logging.getLogger(__name__)

SECRET_HEADER = 'X-LoyalUP-Export-Secret'
MAX_DAYS = 31
DEFAULT_LIMIT = 1000
MAX_LIMIT = 2000
DEFAULT_WINDOW_MIN = 60
MAX_WINDOW_MIN = 24 * 60
# Режимы «на месте»: скан доставки и сайта к столу и смене отношения не имеет.
DEFAULT_MODES = ('cafe', 'review')
ALL_MODES = ('cafe', 'review', 'delivery', 'delivery_network', 'website')
ACTION_KINDS = ('game', 'subscribe', 'review')
_SCHEMA_RE = re.compile(r'^[a-z][a-z0-9_-]{0,62}$')


class ExportError(Exception):
    def __init__(self, status: int, code: str, detail: str = ''):
        super().__init__(f'{status} {code}: {detail}')
        self.status = status
        self.code = code
        self.detail = detail

    def as_dict(self) -> dict:
        return {'code': self.code, 'detail': self.detail}


# ── настройки ────────────────────────────────────────────────────────────────

def export_secret() -> str:
    return str(getattr(settings, 'CHECKUP_SCANS_EXPORT_SECRET', '') or '')


def export_enabled() -> bool:
    return bool(export_secret())


def secret_matches(provided: str) -> bool:
    expected = export_secret()
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(provided), expected)


def allowed_tenants() -> list[str]:
    """Белый список сетей. Пустой = ни одной (выгрузку включают по сети)."""
    raw = getattr(settings, 'CHECKUP_SCANS_EXPORT_TENANTS', None) or []
    if isinstance(raw, str):
        raw = raw.split(',')
    return [s.strip().lower() for s in raw if s and s.strip()]


# ── разбор запроса ───────────────────────────────────────────────────────────

def _parse_moment(raw: str, name: str) -> datetime.datetime:
    """ISO-дата (полночь по времени сервера) или ISO-время; без зоны — зона сервера."""
    text = str(raw or '').strip()
    if not text:
        raise ExportError(400, 'invalid_query', f'{name}: обязателен (YYYY-MM-DD или ISO-время)')
    moment = None
    try:
        moment = parse_datetime(text)
    except ValueError:
        moment = None
    if moment is None:
        try:
            day = parse_date(text)
        except ValueError:
            day = None
        if day is None:
            raise ExportError(400, 'invalid_query', f'{name}: YYYY-MM-DD или ISO-время')
        moment = datetime.datetime.combine(day, datetime.time.min)
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_default_timezone())
    return moment


def _int_param(params, name: str, default: int, lo: int, hi: int) -> int:
    raw = params.get(name)
    if raw in (None, ''):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ExportError(400, 'invalid_query', f'{name}: целое число')
    return max(lo, min(value, hi))


def parse_query(params) -> dict:
    schema = str(params.get('tenant_schema') or '').strip().lower()
    if not schema or schema == 'public' or not _SCHEMA_RE.match(schema):
        raise ExportError(400, 'invalid_query', 'tenant_schema: имя сети LoyalUP')
    start = _parse_moment(params.get('from'), 'from')
    end = _parse_moment(params.get('to'), 'to')
    if end <= start:
        raise ExportError(400, 'invalid_query', 'to: позже from (правая граница не входит)')
    if end - start > datetime.timedelta(days=MAX_DAYS):
        raise ExportError(400, 'invalid_query', f'период не длиннее {MAX_DAYS} дней')
    raw_modes = str(params.get('modes') or '').strip()
    if raw_modes:
        modes = tuple(dict.fromkeys(m.strip() for m in raw_modes.split(',') if m.strip()))
        unknown = [m for m in modes if m not in ALL_MODES]
        if unknown or not modes:
            raise ExportError(400, 'invalid_query', 'modes: ' + ' | '.join(ALL_MODES))
    else:
        modes = DEFAULT_MODES
    return {
        'tenant_schema': schema,
        'start': start,
        'end': end,
        'after_id': _int_param(params, 'after_id', 0, 0, 2 ** 62),
        'limit': _int_param(params, 'limit', DEFAULT_LIMIT, 1, MAX_LIMIT),
        'window': _int_param(params, 'window', DEFAULT_WINDOW_MIN, 1, MAX_WINDOW_MIN),
        'modes': modes,
    }


def resolve_tenant(schema: str):
    allowed = allowed_tenants()
    if schema not in allowed:
        raise ExportError(403, 'tenant_not_allowed',
                          f'выгрузка сканов для сети {schema} не включена (CHECKUP_SCANS_EXPORT_TENANTS)')
    Tenant = get_tenant_model()
    tenant = Tenant.objects.filter(schema_name=schema).first()
    if tenant is None or not getattr(tenant, 'is_active', True):
        raise ExportError(404, 'tenant_not_found', f'сети {schema} нет или она выключена')
    return tenant


# ── сборка строк (чистая часть — без БД, её держат тесты) ───────────────────

def _iso(value):
    return timezone.localtime(value).isoformat() if value else None


def _first_in_window(moments, since, until):
    hits = [m for m in moments if m and since <= m < until]
    return min(hits) if hits else None


def assemble_row(scan, facts: dict, employee_guests: set, window_min: int) -> dict:
    """
    Строка выгрузки по одному скану.

    `facts` — что гость этого профиля делал вокруг скана (`collect_facts`):
    `games` — [(время, попытка-словарь)], `subscribes` — [время],
    `reviews` — [время]. Берём только то, что случилось НЕ РАНЬШЕ скана и
    раньше конца окна: подписка до скана — это не заслуга стола.
    """
    qr = scan.qr
    profile = scan.client
    since = scan.scanned_at
    until = since + datetime.timedelta(minutes=window_min)

    games = [(at, a) for at, a in facts.get('games', []) if at and since <= at < until]
    games.sort(key=lambda pair: pair[0])
    actions = []
    if games:
        actions.append({'kind': 'game', 'at': games[0][0]})
    sub_at = _first_in_window(facts.get('subscribes', []), since, until)
    if sub_at:
        actions.append({'kind': 'subscribe', 'at': sub_at})
    rev_at = _first_in_window(facts.get('reviews', []), since, until)
    if rev_at:
        actions.append({'kind': 'review', 'at': rev_at})
    actions.sort(key=lambda a: a['at'])

    served_by = None
    for at, attempt in games:
        if attempt.get('served_by_id'):
            served_by = {
                'profile_id': attempt['served_by_id'],
                'vk_id': str(attempt.get('served_vk_id') or '') or None,
                'name': ' '.join(p for p in (attempt.get('served_first_name') or '',
                                             attempt.get('served_last_name') or '') if p).strip(),
                # Официанта выбирают ПОСЛЕ игры, отдельным запросом: время
                # выбора — последняя правка попытки, а не её создание.
                'at': _iso(attempt.get('updated_at') or at),
            }
            break

    guest_id = getattr(profile, 'client_id', None)
    return {
        'scan_id': scan.pk,
        'scanned_at': _iso(scan.scanned_at),
        'qr': {
            'id': qr.pk,
            'src': qr.key,
            'mode': qr.mode,
            'name': qr.name,
            'table_number': qr.table_number,
            'table_external_id': getattr(qr, 'table_external_id', '') or '',
            'table_hall': getattr(qr, 'table_hall', '') or '',
        },
        'branch': {'id': qr.branch_id, 'branch_id': qr.branch.branch_id, 'name': qr.branch.name},
        'guest_id': guest_id,
        'is_employee': bool(getattr(profile, 'is_employee', False) or guest_id in employee_guests),
        'actions': [{'kind': a['kind'], 'at': _iso(a['at'])} for a in actions],
        'first_action': ({'kind': actions[0]['kind'], 'at': _iso(actions[0]['at'])}
                         if actions else None),
        'served_by': served_by,
    }


# ── БД ───────────────────────────────────────────────────────────────────────

def fetch_scans(q: dict) -> list:
    from apps.tenant.branch.models import QRScan
    qs = (QRScan.objects
          .select_related('qr', 'qr__branch', 'client')
          .filter(scanned_at__gte=q['start'], scanned_at__lt=q['end'],
                  qr__mode__in=q['modes'], pk__gt=q['after_id'])
          .order_by('pk'))
    return list(qs[:q['limit'] + 1])


def collect_facts(profile_ids: list[int], start, until) -> dict:
    """{profile_id: {'games': [(at, попытка)], 'subscribes': [at], 'reviews': [at]}} тремя запросами."""
    from apps.tenant.branch.models import ClientVKStatus, TestimonialMessage
    from apps.tenant.game.models import ClientAttempt

    out: dict[int, dict] = {pid: {'games': [], 'subscribes': [], 'reviews': []} for pid in profile_ids}
    if not profile_ids:
        return out
    attempts = (ClientAttempt.objects
                .filter(client_id__in=profile_ids, created_at__gte=start, created_at__lt=until,
                        delivery=False)
                .values('client_id', 'created_at', 'updated_at', 'served_by_id',
                        'served_by__client__vk_id', 'served_by__client__first_name',
                        'served_by__client__last_name'))
    for a in attempts:
        out[a['client_id']]['games'].append((a['created_at'], {
            'served_by_id': a['served_by_id'],
            'served_vk_id': a['served_by__client__vk_id'],
            'served_first_name': a['served_by__client__first_name'],
            'served_last_name': a['served_by__client__last_name'],
            'updated_at': a['updated_at'],
        }))
    # Подписка засчитывается только сделанная ИЗ ПРИЛОЖЕНИЯ (*_via_app) — так
    # же её считает индекс сканирования (get_qr_scan_count).
    for s in (ClientVKStatus.objects.filter(client_id__in=profile_ids)
              .values('client_id', 'community_via_app', 'community_joined_at',
                      'newsletter_via_app', 'newsletter_joined_at')):
        if s['community_via_app'] and s['community_joined_at']:
            out[s['client_id']]['subscribes'].append(s['community_joined_at'])
        if s['newsletter_via_app'] and s['newsletter_joined_at']:
            out[s['client_id']]['subscribes'].append(s['newsletter_joined_at'])
    for m in (TestimonialMessage.objects
              .filter(conversation__client_id__in=profile_ids,
                      source=TestimonialMessage.Source.APP,
                      created_at__gte=start, created_at__lt=until)
              .values('conversation__client_id', 'created_at')):
        out[m['conversation__client_id']]['reviews'].append(m['created_at'])
    return out


def employee_guest_ids(guest_ids) -> set:
    from apps.tenant.branch.models import ClientBranch
    if not guest_ids:
        return set()
    return set(ClientBranch.objects
               .filter(client_id__in=list(guest_ids), is_employee=True)
               .values_list('client_id', flat=True))


def build_export(q: dict) -> dict:
    """Страница выгрузки. Зовётся внутри schema_context сети."""
    scans = fetch_scans(q)
    has_more = len(scans) > q['limit']
    scans = scans[:q['limit']]
    window = datetime.timedelta(minutes=q['window'])
    facts = {}
    employees = set()
    if scans:
        first_at = min(s.scanned_at for s in scans)
        last_at = max(s.scanned_at for s in scans)
        facts = collect_facts(sorted({s.client_id for s in scans}), first_at, last_at + window)
        employees = employee_guest_ids({getattr(s.client, 'client_id', None) for s in scans})
    empty = {'games': [], 'subscribes': [], 'reviews': []}
    rows = [assemble_row(s, facts.get(s.client_id, empty), employees, q['window']) for s in scans]
    return {
        'tenant_schema': q['tenant_schema'],
        'timezone': settings.TIME_ZONE,
        'start': _iso(q['start']),
        'end': _iso(q['end']),
        'window_minutes': q['window'],
        'modes': list(q['modes']),
        'limit': q['limit'],
        'count': len(rows),
        'has_more': has_more,
        'next_after_id': rows[-1]['scan_id'] if (rows and has_more) else None,
        'results': rows,
    }


# ── ручка ────────────────────────────────────────────────────────────────────

@method_decorator(csrf_exempt, name='dispatch')
class ScansExportView(View):
    """GET /api/v1/internal/checkup/scans/?tenant_schema=&from=&to=&after_id=&limit=&window=&modes="""
    http_method_names = ['get']

    def get(self, request):
        if not _is_internal_request(request):
            log.warning('scans-export: отклонён внешний запрос remote=%s xff=%s',
                        request.META.get('REMOTE_ADDR', ''), request.headers.get('X-Forwarded-For', ''))
            return JsonResponse({'code': 'forbidden', 'detail': 'только с внутреннего адреса сервера'},
                                status=403)
        if not export_enabled():
            return JsonResponse({'code': 'export_disabled',
                                 'detail': 'выгрузка сканов выключена (CHECKUP_SCANS_EXPORT_SECRET пуст)'},
                                status=503)
        if not secret_matches(request.headers.get(SECRET_HEADER, '')):
            log.warning('scans-export: неверный секрет remote=%s', request.META.get('REMOTE_ADDR', ''))
            return JsonResponse({'code': 'bad_secret', 'detail': f'неверный {SECRET_HEADER}'}, status=401)
        try:
            q = parse_query(request.GET)
            resolve_tenant(q['tenant_schema'])
            with schema_context(q['tenant_schema']):
                payload = build_export(q)
        except ExportError as e:
            log.info('scans-export: отказ %s %s (%s)', e.status, e.code, e.detail)
            return JsonResponse(e.as_dict(), status=e.status)
        log.info('scans-export: %s %s..%s after=%s → %s строк%s', q['tenant_schema'],
                 payload['start'], payload['end'], q['after_id'], payload['count'],
                 ' (есть ещё)' if payload['has_more'] else '')
        return JsonResponse(payload, json_dumps_params={'ensure_ascii': False})
