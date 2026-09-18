"""
Точки контакта (QR) для внешнего кабинета CheckUp — контракт платформы, №26/27.

Что это. «Точка контакта» — именованный отслеживаемый QR (`QRCode`,
apps/tenant/branch/models.py:385): владелец создаёт его под конкретное
размещение («флаер у кассы», «стол 7»), получает ссылку с меткой `src` и видит
по ней воронку — сканы → подписки → игры → активации подарка.

Почему новый модуль, а не правка `/api/v1/analytics/contact-points/`. Та ручка
живёт с мобилкой и отдаёт точку ИМЕНЕМ, а режим — человеческой подписью
(analytics/api/services.py:727). Контракт требует внутренний `id` и публичный
`branch_id` рядом, а ломать мобилку нельзя. Поэтому старая ручка остаётся как
есть, а здесь — форма по контракту (раздел 3.2) и действия (создание, правка,
удаление, пакет столов).

Решения, принятые сознательно (v1.5):
  • `src` (он же `QRCode.key`) задаёт СЕРВЕР. Поле в POST не принимается: на
    метке висит печать, и подмена ключа обнулила бы статистику размещения.
  • Смена режима, точки и номера стола разрешена только пока по QR нет ни
    одного скана и ни одного события воронки — иначе `409 has_scans`: у
    напечатанного кода нельзя менять смысл под ногами у гостя. «Сканов нет» ≠
    «не напечатан», поэтому PATCH всегда возвращает пересобранный `url`.
  • DELETE — только для QR без сканов и событий. Дальше это уже история
    воронки: у `QRScan`/`ContactPointEvent` внешний ключ каскадный, и удаление
    точки стёрло бы её.
  • Чужая или недоступная точка — всегда `404 not_found`, существование не
    раскрываем (как в рассылках, senler/api/broadcasts.py:410).
  • Доступ к разделам (`feature_access`) здесь не проверяем: по контракту
    раздел гейтит CheckUp, а у пользователей из обмена токена ограничений нет.
  • Картинки QR модуль не отдаёт: серверной библиотеки в проекте нет
    (в админке PNG рисует браузер). Отдаём `url` — QR по нему рисует кабинет.

Ошибки — единой формой `{code, detail}`: `invalid_payload` и `table_required`
(400), `not_found` (404), `has_scans` (409).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import Count, Max
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.guest.models import Client as GuestClient
from apps.shared.users.access import current_schema_name, effective_branch_ids
from apps.tenant.analytics.api.serializers import StatsQuerySerializer
from apps.tenant.analytics.api.services import get_contact_point_funnel
from apps.tenant.branch.models import Branch, ContactPointEvent, QRCode, QRScan

from .qr_links import build_branch_link, build_qr_link, current_company_id

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
# Потолок пакета столов: один вызов не должен родить тираж на всё заведение.
BATCH_TABLES_MAX = 200
MAX_NAME_LEN = 120
FUNNEL_DAYS = 30

STAGES = ('scan', 'subscribe', 'play', 'activate')
STAGE_LABELS = {
    'scan':      'Сканировали',
    'subscribe': 'Подписались',
    'play':      'Сыграли',
    'activate':  'Активировали подарок',
}
EMPTY_FUNNEL = {'scans': 0, 'guests': 0, 'subscribed': 0, 'played': 0,
                'activated': 0, 'conversion': 0}


# ── общие помощники (форма как в senler/api/broadcasts.py) ────────────────────

def _error(code: str, detail: str, status_code: int, **extra):
    payload = {'code': code, 'detail': detail}
    payload.update(extra)
    return Response(payload, status=status_code)


def _iso(value):
    return value.isoformat() if value else None


def _page_params(request):
    """limit/offset с потолком, чтобы кабинет не выкачал таблицу целиком."""
    try:
        limit = int(request.query_params.get('limit') or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        offset = int(request.query_params.get('offset') or 0)
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, MAX_LIMIT)), max(0, offset)


def _allowed_branches(request):
    """None — доступны все точки; [-1] — доступа нет; иначе список внутренних id."""
    return effective_branch_ids(request.user, current_schema_name(), None)


def _period(request):
    """Период как у остальной аналитики (period | start/end). Ошибка → None."""
    ser = StatsQuerySerializer(data=request.query_params)
    if not ser.is_valid():
        return None, ser.errors
    return ser.validated_data, None


def _qr_scope(request, requested_branch_ids):
    """Точки, по которым сотруднику можно смотреть QR (с учётом ?branch_ids)."""
    return effective_branch_ids(request.user, current_schema_name(), requested_branch_ids)


def _get_qr(pk, allowed):
    """QR с учётом прав. None — нет такого или чужой (отвечаем 404)."""
    qs = QRCode.objects.select_related('branch').filter(pk=pk)
    if allowed is not None:
        qs = qs.filter(branch_id__in=allowed)
    return qs.first()


def _has_scans(qr) -> bool:
    """Есть ли по QR хоть один скан или событие воронки (история)."""
    if QRScan.objects.filter(qr_id=qr.pk).exists():
        return True
    return ContactPointEvent.objects.filter(qr_id=qr.pk).exists()


def _scan_windows(qr_ids: list[int]) -> dict:
    """Сканы за 7 / 30 дней и за всё время — тремя сгруппированными запросами."""
    out = {int(i): {'d7': 0, 'd30': 0, 'all': 0} for i in qr_ids}
    if not qr_ids:
        return out
    now = timezone.now()
    base = QRScan.objects.filter(qr_id__in=qr_ids)
    windows = (('d7', now - timedelta(days=7)), ('d30', now - timedelta(days=30)), ('all', None))
    for key, since in windows:
        rows = base if since is None else base.filter(scanned_at__gte=since)
        for row in rows.values('qr_id').annotate(n=Count('id')):
            out[int(row['qr_id'])][key] = row['n']
    return out


def _funnel_map(scope, start, end) -> dict:
    """
    Воронка по QR за период — ТЕМ ЖЕ сервисом, что веб и мобилка.

    Своей арифметики здесь нет намеренно: определения («гости» — уникальные,
    `conversion` = подписки / гости) должны совпадать с остальным продуктом,
    иначе цифры в CheckUp разойдутся с цифрами в кабинете.
    """
    rows = get_contact_point_funnel(scope, start, end)
    return {int(r['id']): r for r in rows}


def _row(qr, company_id: str, scans: dict, funnel: dict) -> dict:
    f = funnel or EMPTY_FUNNEL
    return {
        'id': qr.pk,
        'name': qr.name,
        'branch': {'id': qr.branch_id, 'branch_id': qr.branch.branch_id, 'name': qr.branch.name},
        'mode': qr.mode,
        'mode_label': qr.get_mode_display(),
        'table_number': qr.table_number,
        'src': qr.key,
        'url': build_qr_link(qr, company_id),
        'is_active': qr.is_active,
        'created_at': _iso(qr.created_at),
        'scans': scans or {'d7': 0, 'd30': 0, 'all': 0},
        'funnel': {
            'scans': f.get('scans', 0),
            'guests': f.get('guests', 0),
            'subscribed': f.get('subscribed', 0),
            'played': f.get('played', 0),
            'activated': f.get('activated', 0),
            'conversion': f.get('conversion', 0),
        },
    }


def _rows_for(qrs: list, funnel: dict) -> list[dict]:
    ids = [q.pk for q in qrs]
    scans = _scan_windows(ids)
    company_id = current_company_id()
    return [_row(q, company_id, scans.get(q.pk), funnel.get(q.pk)) for q in qrs]


def _sum_funnel(rows) -> dict:
    """Сумма воронки по набору строк + конверсия по сумме (не средняя)."""
    out = dict(EMPTY_FUNNEL)
    for row in rows:
        if not row:
            continue
        for key in ('scans', 'guests', 'subscribed', 'played', 'activated'):
            out[key] += row.get(key, 0)
    out['conversion'] = (round(out['subscribed'] / out['guests'] * 100) if out['guests'] else 0)
    return out


# ── создание и правка: разбор тела ───────────────────────────────────────────

class PayloadError(Exception):
    """Беда в теле запроса: (code, detail) для единой формы ошибки."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _parse_name(value) -> str:
    name = str(value or '').strip()
    if not name:
        raise PayloadError('invalid_payload', 'name: назовите размещение — по нему его узнают в отчёте')
    if len(name) > MAX_NAME_LEN:
        raise PayloadError('invalid_payload', f'name: не длиннее {MAX_NAME_LEN} символов')
    return name


def _parse_mode(value) -> str:
    mode = str(value or '').strip()
    if mode not in QRCode.Mode.values:
        raise PayloadError('invalid_payload',
                           'mode: ' + ' | '.join(QRCode.Mode.values))
    return mode


def _parse_table_number(value, mode: str):
    """Стол обязателен у «отзыва со стола» и запрещён у остальных режимов."""
    if value in (None, ''):
        if mode == QRCode.Mode.REVIEW:
            raise PayloadError('table_required',
                               'table_number: для «отзыва со стола» номер стола обязателен — '
                               'без него гость не сможет отправить отзыв')
        return None
    try:
        table = int(value)
    except (TypeError, ValueError):
        raise PayloadError('invalid_payload', 'table_number: целое число')
    if table <= 0:
        raise PayloadError('invalid_payload', 'table_number: положительное число')
    if mode != QRCode.Mode.REVIEW:
        raise PayloadError('invalid_payload',
                           'table_number: только для режима review («отзыв со стола»)')
    return table


def _parse_is_active(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('true', '1', 'yes')


def _reject_src(data):
    """`src` задаёт сервер. Молча игнорировать нельзя — кабинет не заметит."""
    if 'src' in data or 'key' in data:
        raise PayloadError('invalid_payload',
                           'src задаёт сервер: метка печатается на QR и менять её нельзя')


def _branch_or_none(branch_id, allowed):
    qs = Branch.objects.filter(pk=branch_id)
    if allowed is not None:
        qs = qs.filter(pk__in=allowed)
    return qs.first()


# ── список и создание ────────────────────────────────────────────────────────

class ContactPointListCreateAPIView(APIView):
    """
    GET  /api/v1/contact-points/ — список точек контакта с воронкой за период.
    POST /api/v1/contact-points/ — создать точку контакта.

    Фильтры GET: `branch_ids` (внутренние id, через запятую), `mode`,
    `is_active`, `q` (по названию и метке), `period|start|end`, `limit`/`offset`.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        params, errors = _period(request)
        if errors is not None:
            return _error('invalid_payload', f'параметры периода: {errors}',
                          http_status.HTTP_400_BAD_REQUEST)
        scope = _qr_scope(request, params.get('branch_ids'))
        limit, offset = _page_params(request)

        qs = QRCode.objects.select_related('branch').order_by('-created_at')
        # Пустой scope = «все точки» (так effective_branch_ids отвечает
        # неограниченному сотруднику без ?branch_ids); [-1] = доступа нет и
        # выдача честно пустая. Проверять `is not None` здесь НЕЛЬЗЯ —
        # суперадмин увидел бы ноль точек контакта.
        if scope:
            qs = qs.filter(branch_id__in=scope)

        mode = request.query_params.get('mode')
        if mode:
            if mode not in QRCode.Mode.values:
                return _error('invalid_payload', 'mode: ' + ' | '.join(QRCode.Mode.values),
                              http_status.HTTP_400_BAD_REQUEST)
            qs = qs.filter(mode=mode)
        is_active = request.query_params.get('is_active')
        if is_active not in (None, ''):
            qs = qs.filter(is_active=_parse_is_active(is_active))
        search = (request.query_params.get('q') or '').strip()
        if search:
            from django.db.models import Q
            qs = qs.filter(Q(name__icontains=search) | Q(key__icontains=search))

        # Один проход по отфильтрованным id: и total, и итоги по всему фильтру.
        all_ids = list(qs.values_list('pk', flat=True))
        page = list(qs[offset:offset + limit])
        funnel = _funnel_map(scope, params['start'], params['end'])
        rows = _rows_for(page, funnel)
        return Response({
            'total': len(all_ids), 'limit': limit, 'offset': offset,
            'results': rows,
            # Две суммы намеренно: `totals` сходится с тем, что видно в
            # таблице сейчас, `totals_filtered` — со всем фильтром, включая
            # страницы, которые кабинет не листал (★1 ревью CheckUp).
            'totals': _sum_funnel([r['funnel'] for r in rows]),
            'totals_filtered': _sum_funnel([funnel.get(i) for i in all_ids]),
            'meta': {'start': str(params['start']), 'end': str(params['end']),
                     'branch_ids': scope or []},
        })

    def post(self, request):
        data = request.data or {}
        allowed = _allowed_branches(request)
        try:
            _reject_src(data)
            name = _parse_name(data.get('name'))
            mode = _parse_mode(data.get('mode'))
            table_number = _parse_table_number(data.get('table_number'), mode)
        except PayloadError as exc:
            return _error(exc.code, exc.detail, http_status.HTTP_400_BAD_REQUEST)

        branch = _branch_or_none(data.get('branch_id'), allowed)
        if branch is None:
            return _error('not_found', 'Точка не найдена', http_status.HTTP_404_NOT_FOUND)

        qr = QRCode.objects.create(branch=branch, name=name, mode=mode,
                                   table_number=table_number,
                                   is_active=_parse_is_active(data.get('is_active', True)))
        log.info('contact point created: %s qr=%s branch=%s mode=%s by=%s',
                 current_schema_name(), qr.pk, branch.pk, mode, request.user)
        company_id = current_company_id()
        return Response(_row(qr, company_id, None, None), status=http_status.HTTP_201_CREATED)


# ── карточка, правка, удаление ───────────────────────────────────────────────

class ContactPointDetailAPIView(APIView):
    """
    GET    /api/v1/contact-points/{id}/ — карточка + воронка по дням.
    PATCH  /api/v1/contact-points/{id}/ — name, is_active всегда; mode,
           branch_id, table_number — только пока сканов нет (иначе 409).
    DELETE /api/v1/contact-points/{id}/ — только пока сканов нет (иначе 409).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk: int):
        allowed = _allowed_branches(request)
        qr = _get_qr(pk, allowed)
        if qr is None:
            return _error('not_found', 'Точка контакта не найдена', http_status.HTTP_404_NOT_FOUND)
        params, errors = _period(request)
        if errors is not None:
            return _error('invalid_payload', f'параметры периода: {errors}',
                          http_status.HTTP_400_BAD_REQUEST)

        row = _rows_for([qr], _funnel_map([qr.branch_id], params['start'], params['end']))[0]
        row['funnel_by_day'] = _funnel_by_day(qr, params['start'], params['end'])
        return Response(row)

    def patch(self, request, pk: int):
        allowed = _allowed_branches(request)
        qr = _get_qr(pk, allowed)
        if qr is None:
            return _error('not_found', 'Точка контакта не найдена', http_status.HTTP_404_NOT_FOUND)
        data = request.data or {}
        try:
            _reject_src(data)
        except PayloadError as exc:
            return _error(exc.code, exc.detail, http_status.HTTP_400_BAD_REQUEST)

        fields: list[str] = []
        # Смена смысла напечатанного кода: только пока по нему никто не приходил.
        risky = [k for k in ('mode', 'branch_id', 'table_number') if k in data]
        if risky and _has_scans(qr):
            return _error('has_scans',
                          'По этой точке контакта уже есть сканы: режим, точку и стол менять '
                          'нельзя — напечатанный QR уведёт гостя не туда. Выключите её '
                          '(is_active=false) и создайте новую.',
                          http_status.HTTP_409_CONFLICT, fields=risky)

        try:
            if 'name' in data:
                qr.name = _parse_name(data.get('name'))
                fields.append('name')
            if 'mode' in data:
                qr.mode = _parse_mode(data.get('mode'))
                fields.append('mode')
            if 'table_number' in data or 'mode' in data:
                if 'table_number' in data:
                    raw_table = data.get('table_number')
                else:
                    # Режим сменили, стол не передали: у «отзыва со стола» он
                    # обязателен (ниже будет table_required), у остальных —
                    # просто снимаем, иначе в ссылке останется чужой &table=.
                    raw_table = qr.table_number if qr.mode == QRCode.Mode.REVIEW else None
                qr.table_number = _parse_table_number(raw_table, qr.mode)
                fields.append('table_number')
        except PayloadError as exc:
            return _error(exc.code, exc.detail, http_status.HTTP_400_BAD_REQUEST)

        if 'branch_id' in data:
            branch = _branch_or_none(data.get('branch_id'), allowed)
            if branch is None:
                return _error('not_found', 'Точка не найдена', http_status.HTTP_404_NOT_FOUND)
            qr.branch = branch
            fields.append('branch')
        if 'is_active' in data:
            qr.is_active = _parse_is_active(data.get('is_active'))
            fields.append('is_active')

        if fields:
            qr.save(update_fields=sorted(set(fields)))
            log.info('contact point patched: %s qr=%s fields=%s by=%s',
                     current_schema_name(), qr.pk, sorted(set(fields)), request.user)
        # url отдаём всегда: «сканов нет» не значит «не напечатан» — по новой
        # ссылке видно, что старый тираж устарел.
        return Response(_row(qr, current_company_id(), None, None))

    def delete(self, request, pk: int):
        allowed = _allowed_branches(request)
        qr = _get_qr(pk, allowed)
        if qr is None:
            return _error('not_found', 'Точка контакта не найдена', http_status.HTTP_404_NOT_FOUND)
        if _has_scans(qr):
            return _error('has_scans',
                          'По этой точке контакта есть сканы — удаление стёрло бы историю '
                          'воронки. Выключите её (is_active=false): старая статистика '
                          'останется, новые сканы учитываться не будут.',
                          http_status.HTTP_409_CONFLICT)
        qr.delete()
        log.info('contact point deleted: %s qr=%s by=%s', current_schema_name(), pk, request.user)
        return Response(status=http_status.HTTP_204_NO_CONTENT)


def _funnel_by_day(qr, start, end) -> list[dict]:
    """Сканы и уникальные гости по дням — для графика в карточке."""
    rows = (QRScan.objects
            .filter(qr_id=qr.pk, scanned_at__date__gte=start, scanned_at__date__lte=end)
            .annotate(day=TruncDate('scanned_at'))
            .values('day')
            .annotate(scans=Count('id'), guests=Count('client__client_id', distinct=True))
            .order_by('day'))
    return [{'date': str(r['day']), 'scans': r['scans'], 'guests': r['guests']} for r in rows]


# ── гости стадии воронки ─────────────────────────────────────────────────────

class ContactPointGuestsAPIView(APIView):
    """
    GET /api/v1/contact-points/{id}/guests/?stage=scan|subscribe|play|activate

    Гости одной стадии: по одной строке на ГОСТЯ (не на событие), `at` — время
    последнего события этой стадии. Так `total` сходится с `funnel.guests` из
    карточки; список сканов с каждым отдельным открытием тут не нужен — у
    скана есть кулдаун 6 ч, и одному гостю соответствует несколько записей.

    Права проверяются по точке QR (⚠️ веб-страница детализации этого не делает
    — apps/tenant/analytics/views.py:275; здесь дырку не повторяем).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk: int):
        allowed = _allowed_branches(request)
        qr = _get_qr(pk, allowed)
        if qr is None:
            return _error('not_found', 'Точка контакта не найдена', http_status.HTTP_404_NOT_FOUND)
        params, errors = _period(request)
        if errors is not None:
            return _error('invalid_payload', f'параметры периода: {errors}',
                          http_status.HTTP_400_BAD_REQUEST)
        stage = (request.query_params.get('stage') or 'scan').strip()
        if stage not in STAGES:
            return _error('invalid_payload', 'stage: ' + ' | '.join(STAGES),
                          http_status.HTTP_400_BAD_REQUEST)
        limit, offset = _page_params(request)
        start, end = params['start'], params['end']

        if stage == 'scan':
            base = QRScan.objects.filter(qr_id=qr.pk,
                                         scanned_at__date__gte=start, scanned_at__date__lte=end)
            grouped = base.values('client__client_id').annotate(at=Max('scanned_at'))
        else:
            base = ContactPointEvent.objects.filter(qr_id=qr.pk, stage=stage,
                                                    created_at__date__gte=start,
                                                    created_at__date__lte=end)
            grouped = base.values('client__client_id').annotate(at=Max('created_at'))

        grouped = grouped.order_by('-at')
        total = grouped.count()
        page = list(grouped[offset:offset + limit])
        guest_ids = [r['client__client_id'] for r in page]
        guests = {g.pk: g for g in GuestClient.objects
                  .filter(pk__in=guest_ids)
                  .select_related('rf_score', 'rf_score__segment')}

        results = []
        for row in page:
            guest = guests.get(row['client__client_id'])
            if guest is None:
                continue
            segment = getattr(getattr(guest, 'rf_score', None), 'segment', None)
            results.append({
                'guest_id': guest.pk,
                # Строкой: у VK ID 2.0 до 12 знаков, и приёмник CheckUp хранит
                # его строкой — пусть тип совпадает с обеих сторон (м6 ревью).
                'vk_id': str(guest.vk_id),
                'name': f'{guest.first_name} {guest.last_name}'.strip(),
                'at': _iso(row['at']),
                'segment': ({'code': segment.code, 'name': segment.name} if segment else None),
                'branch': {'id': qr.branch_id, 'branch_id': qr.branch.branch_id,
                           'name': qr.branch.name},
            })
        return Response({
            'total': total, 'limit': limit, 'offset': offset,
            'stage': stage, 'stage_label': STAGE_LABELS[stage],
            'results': results,
            'meta': {'start': str(start), 'end': str(end)},
        })


# ── пакет QR по столам ───────────────────────────────────────────────────────

class ContactPointBatchTablesAPIView(APIView):
    """
    POST /api/v1/contact-points/batch-tables/ {branch_id, from, to, name_template?}

    Заводит QR «отзыв со стола» на диапазон столов. Столы, у которых уже есть
    АКТИВНЫЙ такой QR, пропускаются (в схеме уникального индекса на пару
    «точка + стол» нет — защищаемся здесь, чтобы не печатать два кода на один
    стол). Потолок — BATCH_TABLES_MAX за вызов.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        allowed = _allowed_branches(request)
        try:
            _reject_src(data)
            first = int(data.get('from'))
            last = int(data.get('to'))
        except PayloadError as exc:
            return _error(exc.code, exc.detail, http_status.HTTP_400_BAD_REQUEST)
        except (TypeError, ValueError):
            return _error('invalid_payload', 'from и to: целые номера столов',
                          http_status.HTTP_400_BAD_REQUEST)
        if first <= 0 or last < first:
            return _error('invalid_payload', 'диапазон столов: from ≥ 1 и to ≥ from',
                          http_status.HTTP_400_BAD_REQUEST)
        if last - first + 1 > BATCH_TABLES_MAX:
            return _error('invalid_payload',
                          f'за один вызов не больше {BATCH_TABLES_MAX} столов',
                          http_status.HTTP_400_BAD_REQUEST)

        branch = _branch_or_none(data.get('branch_id'), allowed)
        if branch is None:
            return _error('not_found', 'Точка не найдена', http_status.HTTP_404_NOT_FOUND)

        template = str(data.get('name_template') or 'Отзыв со стола {table}').strip()
        existing = set(QRCode.objects
                       .filter(branch=branch, mode=QRCode.Mode.REVIEW, is_active=True,
                               table_number__gte=first, table_number__lte=last)
                       .values_list('table_number', flat=True))
        created, skipped = [], []
        company_id = current_company_id()
        for table in range(first, last + 1):
            if table in existing:
                skipped.append({'table_number': table, 'reason': 'already_exists'})
                continue
            qr = QRCode.objects.create(branch=branch, name=template.format(table=table)[:MAX_NAME_LEN],
                                       mode=QRCode.Mode.REVIEW, table_number=table, is_active=True)
            created.append(_row(qr, company_id, None, None))
        log.info('contact points batch: %s branch=%s created=%s skipped=%s by=%s',
                 current_schema_name(), branch.pk, len(created), len(skipped), request.user)
        return Response({'created': created, 'skipped': skipped},
                        status=http_status.HTTP_201_CREATED if created else http_status.HTTP_200_OK)


# ── материалы точки (№27) ────────────────────────────────────────────────────

class BranchMaterialsAPIView(APIView):
    """
    GET /api/v1/mobile/branches/{id}/materials/

    Готовые ссылки и список QR точки — чтобы «скопировать» в кабинете CheckUp
    работало без сборки ссылок на их стороне.

    ⚠️ `links.mini_app_vk` и `links.delivery` идут БЕЗ метки `src`: это ссылки
    «просто войти», в воронку точек контакта они не попадают. Для печати нужен
    QR из `qr` — у него метка есть. `links.site` без QR режима website не
    существует вовсе (метка `web=<src>` там обязательна), поэтому там `null`.
    Ссылки на Телеграм в v1.5 нет: ТГ-мини-апп живёт на своём домене и другим
    бандлом, формат подтверждает CheckUp.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk: int):
        allowed = _allowed_branches(request)
        qs = Branch.objects.filter(pk=pk)
        if allowed is not None:
            qs = qs.filter(pk__in=allowed)
        branch = qs.first()
        if branch is None:
            return _error('not_found', 'Точка не найдена', http_status.HTTP_404_NOT_FOUND)

        company_id = current_company_id()
        by_mode: dict[str, list] = {m: [] for m in QRCode.Mode.values}
        for qr in (QRCode.objects.select_related('branch')
                   .filter(branch=branch).order_by('-created_at')):
            item = {'id': qr.pk, 'name': qr.name, 'is_active': qr.is_active,
                    'url': build_qr_link(qr, company_id)}
            if qr.mode == QRCode.Mode.REVIEW:
                item['table_number'] = qr.table_number
            by_mode[qr.mode].append(item)

        site = next((i['url'] for i in by_mode[QRCode.Mode.WEBSITE] if i['is_active']), None)
        return Response({
            'branch': {'id': branch.pk, 'branch_id': branch.branch_id, 'name': branch.name},
            'links': {
                'mini_app_vk': build_branch_link(branch, company_id),
                'delivery': build_branch_link(branch, company_id, delivery=True),
                'site': site,
            },
            'qr': by_mode,
            'print_hint': (
                'Для печати берите ссылку из раздела «qr» — в ней есть метка src, '
                'по которой считаются сканы и воронка размещения. Ссылки из «links» '
                'годятся, чтобы просто переслать гостю: статистику по ним не увидите.'
            ),
        })
