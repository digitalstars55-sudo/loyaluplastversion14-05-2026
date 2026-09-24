"""
Выгрузка выданных подарков для загрузки в 1С:Предприятие.

Запрос сети «БИРФЕСТ» (24.09.2026): бар, день или период, наименование
подарка, количество. Страница — /analytics/gifts-1c/ (веб-админка), то же для
автоматизации — GET /api/v1/analytics/gifts-1c/.

Что считается «выданным подарком»
---------------------------------
Подарок, который гость АКТИВИРОВАЛ в баре — показал сотруднику на кассе:

  • InventoryItem (суперприз из игры, покупка за баллы, подарок на ДР,
    выдано вручную, RF/RFM-награды) — момент выдачи = activated_at;
  • StoryGiftEntry (подарок из сториз / с сайта / приветственный из VK-каталога) —
    activated_at ставится только после ввода кода дня в кафе.

Та же граница, что у «Экономики клиента» (GiftCostEvent пишется при активации) и у
метрики «Активировали подарок». Кнопку «Выдан» (used_at) персонал на практике не
нажимает, поэтому по ней считать нельзя; если used_at всё же стоит без
activated_at, датой выдачи берётся он — такой подарок тоже не теряется.

SuperPrizeEntry отдельно не считается: выбор суперприза создаёт InventoryItem
(acquired_from='super_prize'), он и учитывается — иначе было бы задвоение.

Бар — где подарок забрали:
  • сторис/сайт/VK-каталог — StoryGiftEntry.activated_branch (сетевой подарок),
    иначе точка профиля гостя;
  • RF/RFM-награды (сетевой код дня) — точка из GiftCostEvent той же активации
    (ключ client_branch + activated_at, как в backfill_gift_costs), иначе точка
    профиля гостя;
  • остальное — точка профиля гостя (ClientBranch.branch).
Если точку определить не удалось — строка «Без точки» (не теряем).

Дата — по местному времени проекта (settings.TIME_ZONE), календарные сутки.

Модуль не импортирует модели при загрузке: чистые функции (агрегация, CSV,
XLSX) тестируются без базы. XLSX собирается стандартной библиотекой (zipfile +
SpreadsheetML): openpyxl в зависимостях нет, а ради одного файла пересобирать
образ не нужно.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from xml.sax.saxutils import escape

HEADERS = ('Бар', 'Дата', 'Наименование', 'Количество')

NO_BRANCH_LABEL = 'Без точки'
DELETED_PRODUCT_LABEL = '(подарок удалён)'

GROUP_DAY = 'day'
GROUP_PERIOD = 'period'
GROUP_CHOICES = (
    (GROUP_DAY, 'По дням'),
    (GROUP_PERIOD, 'За период целиком'),
)

FORMAT_XLSX = 'xlsx'
FORMAT_CSV = 'csv'
FORMAT_CHOICES = (
    (FORMAT_XLSX, 'Excel (XLSX)'),
    (FORMAT_CSV, 'CSV (UTF-8, разделитель «;»)'),
)
CONTENT_TYPES = {
    FORMAT_XLSX: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    FORMAT_CSV: 'text/csv; charset=utf-8',
}

# AcquisitionSource.RFM / RF_AUTO — награды с сетевым кодом дня: бар активации
# известен только из GiftCostEvent. Строками, чтобы не тянуть модели при импорте.
RF_SOURCES = frozenset({'rfm', 'rf_auto'})

PERIOD_DASH = '–'  # «01.09.2026–24.09.2026»


# ── Данные ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GiftIssue:
    """Один факт выдачи подарка: где, что, в какой день (местное время)."""
    branch_id: int | None
    branch_name: str | None
    product_name: str
    issued_on: date


@dataclass(frozen=True)
class ExportRow:
    """Строка выгрузки. is_period — строка за весь период (дата — диапазон)."""
    bar: str
    date_from: date
    date_to: date
    name: str
    qty: int
    branch_id: int | None = None
    is_period: bool = False

    @property
    def date_label(self) -> str:
        if self.is_period:
            return period_label(self.date_from, self.date_to)
        return format_date(self.date_from)


def format_date(d: date) -> str:
    return d.strftime('%d.%m.%Y')


def period_label(start: date, end: date) -> str:
    return f'{format_date(start)}{PERIOD_DASH}{format_date(end)}'


def _sort_text(value: str) -> str:
    return (value or '').casefold().replace('ё', 'е')


def _product_name(raw) -> str:
    return (raw or '').strip() or DELETED_PRODUCT_LABEL


# ── Агрегация (чистая функция) ────────────────────────────────────────────────

def aggregate_issues(issues, start: date, end: date, group: str = GROUP_DAY) -> list[ExportRow]:
    """
    Факты выдачи → строки выгрузки.

    group='day'    — строка на (бар, день, наименование);
    group='period' — строка на (бар, наименование) за весь период start..end.
    Факты вне start..end отбрасываются. Одинаковые наименования разных карточек
    подарка (в каталоге бывают дубли) складываются в одну строку — для 1С важна
    номенклатура, а не карточка LoyalUP. Сортировка: бар, дата, наименование;
    «Без точки» — в конце.
    """
    if group not in (GROUP_DAY, GROUP_PERIOD):
        raise ValueError(f'Неизвестная группировка: {group!r}')

    counts: Counter = Counter()
    bar_names: dict = {}
    for it in issues:
        if it.issued_on < start or it.issued_on > end:
            continue
        bucket = it.issued_on if group == GROUP_DAY else start
        counts[(it.branch_id, bucket, it.product_name)] += 1
        if it.branch_id is None:
            bar_names[None] = NO_BRANCH_LABEL
        else:
            bar_names.setdefault(it.branch_id, (it.branch_name or '').strip() or f'Точка #{it.branch_id}')

    rows = [
        ExportRow(
            bar=bar_names[branch_id],
            date_from=bucket,
            date_to=bucket if group == GROUP_DAY else end,
            name=name,
            qty=qty,
            branch_id=branch_id,
            is_period=(group == GROUP_PERIOD),
        )
        for (branch_id, bucket, name), qty in counts.items()
    ]
    rows.sort(key=lambda r: (
        r.branch_id is None, _sort_text(r.bar), r.branch_id or 0,
        r.date_from, _sort_text(r.name), r.name,
    ))
    return rows


def total_qty(rows) -> int:
    return sum(r.qty for r in rows)


# ── Сбор фактов из базы ──────────────────────────────────────────────────────

def local_bounds(start: date, end: date):
    """[начало start; начало дня после end) в часовом поясе проекта."""
    from django.utils import timezone
    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(start, time.min), tz)
    end_dt = timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz)
    return start_dt, end_dt


def _local_date(dt) -> date:
    from django.utils import timezone
    return timezone.localtime(dt).date()


def _issued_window_q(start_dt, end_dt):
    """Активирован в окне, либо (без активации) отмечен выданным в окне."""
    from django.db.models import Q
    return (
        Q(activated_at__gte=start_dt, activated_at__lt=end_dt)
        | Q(activated_at__isnull=True, used_at__gte=start_dt, used_at__lt=end_dt)
    )


def _fetch_inventory_rows(start_dt, end_dt) -> list[dict]:
    from apps.tenant.inventory.models import InventoryItem
    return list(
        InventoryItem.objects
        .filter(_issued_window_q(start_dt, end_dt))
        .order_by()
        .values(
            'acquired_from', 'activated_at', 'used_at', 'client_branch_id',
            'client_branch__branch_id', 'client_branch__branch__name', 'product__name',
        )
    )


def _fetch_story_rows(start_dt, end_dt) -> list[dict]:
    from apps.tenant.inventory.models import StoryGiftEntry
    return list(
        StoryGiftEntry.objects
        .filter(_issued_window_q(start_dt, end_dt))
        .order_by()
        .values(
            'activated_at', 'used_at', 'activated_branch_id', 'activated_branch__name',
            'client_branch__branch_id', 'client_branch__branch__name', 'product__name',
        )
    )


def _fetch_rf_activation_branches(client_branch_ids, start_dt, end_dt) -> dict:
    """(client_branch_id, activated_at) → (branch_id, branch_name) по GiftCostEvent."""
    from apps.tenant.inventory.models import GiftCostEvent
    rows = (
        GiftCostEvent.objects
        .filter(
            kind=GiftCostEvent.Kind.INVENTORY,
            client_branch_id__in=list(client_branch_ids),
            activated_at__gte=start_dt, activated_at__lt=end_dt,
        )
        .order_by()
        .values_list('client_branch_id', 'activated_at', 'branch_id', 'branch__name')
    )
    return {(cb, at): (bid, bname) for cb, at, bid, bname in rows}


def collect_issues(start: date, end: date, branch_ids=None) -> list[GiftIssue]:
    """
    Факты выдачи за период start..end (включительно, местное время).

    branch_ids — список Branch.pk; пусто/None = все точки И строки «Без точки».
    Фильтр по точке применяется к ФАКТИЧЕСКОМУ бару выдачи (для сетевых
    подарков он может отличаться от точки профиля гостя), поэтому — после
    определения бара, а не в запросе.
    """
    start_dt, end_dt = local_bounds(start, end)
    issues: list[GiftIssue] = []

    inv_rows = _fetch_inventory_rows(start_dt, end_dt)
    rf_cbs = {
        r['client_branch_id'] for r in inv_rows
        if r['acquired_from'] in RF_SOURCES and r['activated_at']
    }
    rf_branches = _fetch_rf_activation_branches(rf_cbs, start_dt, end_dt) if rf_cbs else {}

    for r in inv_rows:
        issued_at = r['activated_at'] or r['used_at']
        if issued_at is None or not (start_dt <= issued_at < end_dt):
            continue
        branch_id = r['client_branch__branch_id']
        branch_name = r['client_branch__branch__name']
        if r['acquired_from'] in RF_SOURCES and r['activated_at']:
            hit = rf_branches.get((r['client_branch_id'], r['activated_at']))
            if hit:
                branch_id, branch_name = hit
        issues.append(GiftIssue(branch_id, branch_name, _product_name(r['product__name']),
                                _local_date(issued_at)))

    for r in _fetch_story_rows(start_dt, end_dt):
        issued_at = r['activated_at'] or r['used_at']
        if issued_at is None or not (start_dt <= issued_at < end_dt):
            continue
        if r['activated_branch_id']:
            branch_id, branch_name = r['activated_branch_id'], r['activated_branch__name']
        else:
            branch_id, branch_name = r['client_branch__branch_id'], r['client_branch__branch__name']
        issues.append(GiftIssue(branch_id, branch_name, _product_name(r['product__name']),
                                _local_date(issued_at)))

    if branch_ids:
        wanted = {int(b) for b in branch_ids}
        issues = [i for i in issues if i.branch_id is not None and int(i.branch_id) in wanted]
    return issues


def build_gift_export(start: date, end: date, *, branch_ids=None, group: str = GROUP_DAY) -> list[ExportRow]:
    """Готовые строки выгрузки: бар, дата/период, наименование, количество."""
    return aggregate_issues(collect_issues(start, end, branch_ids), start, end, group)


# ── Файлы ─────────────────────────────────────────────────────────────────────

def rows_to_csv(rows) -> bytes:
    """CSV для 1С: UTF-8 с BOM, разделитель «;», первая строка — заголовки."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';', lineterminator='\r\n')
    w.writerow(HEADERS)
    for r in rows:
        w.writerow([r.bar, r.date_label, r.name, r.qty])
    return ('﻿' + buf.getvalue()).encode('utf-8')


_ILLEGAL_XML_CHARS = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]')
_EXCEL_EPOCH = date(1899, 12, 30)
_COL_WIDTHS = (34, 24, 42, 13)

_XLSX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
    '</Types>'
)
_XLSX_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    '</Relationships>'
)
_XLSX_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
    '</Relationships>'
)
# Стили ячеек: 0 — обычный, 1 — жирный заголовок, 2 — дата дд.мм.гггг.
_XLSX_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="dd.mm.yyyy"/></numFmts>'
    '<fonts count="2">'
    '<font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
    '</fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="3">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>'
)


def _xml_text(value) -> str:
    return escape(_ILLEGAL_XML_CHARS.sub('', str(value)), {'"': '&quot;'})


def _col_letter(idx: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    out = ''
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def rows_to_xlsx(rows, sheet_title: str = 'Подарки') -> bytes:
    """
    XLSX для 1С (один лист, первая строка — заголовки). В выгрузке «по дням»
    дата — настоящая ячейка-дата с форматом дд.мм.гггг; «за период» — текст
    «дд.мм.гггг–дд.мм.гггг». Количество — число.
    """
    strings: list[str] = []
    string_idx: dict[str, int] = {}

    def s(value) -> int:
        value = str(value)
        if value not in string_idx:
            string_idx[value] = len(strings)
            strings.append(value)
        return string_idx[value]

    def str_cell(ref, value, style=0):
        st = f' s="{style}"' if style else ''
        return f'<c r="{ref}" t="s"{st}><v>{s(value)}</v></c>'

    def num_cell(ref, value, style=0):
        st = f' s="{style}"' if style else ''
        return f'<c r="{ref}"{st}><v>{value}</v></c>'

    rows = list(rows)
    xml_rows = []
    header = ''.join(str_cell(f'{_col_letter(i)}1', h, style=1) for i, h in enumerate(HEADERS))
    xml_rows.append(f'<row r="1">{header}</row>')
    for n, r in enumerate(rows, start=2):
        if r.is_period:
            date_cell = str_cell(f'B{n}', r.date_label)
        else:
            date_cell = num_cell(f'B{n}', (r.date_from - _EXCEL_EPOCH).days, style=2)
        xml_rows.append(
            f'<row r="{n}">'
            f'{str_cell(f"A{n}", r.bar)}{date_cell}{str_cell(f"C{n}", r.name)}'
            f'{num_cell(f"D{n}", int(r.qty))}'
            f'</row>'
        )

    last_col = _col_letter(len(HEADERS) - 1)
    cols = ''.join(
        f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
        for i, w in enumerate(_COL_WIDTHS)
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<dimension ref="A1:{last_col}{len(rows) + 1}"/>'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        f'<cols>{cols}</cols>'
        f'<sheetData>{"".join(xml_rows)}</sheetData>'
        '</worksheet>'
    )
    total_refs = len(HEADERS) + 2 * len(rows)  # строковых ячеек: заголовки + бар + наименование
    total_refs += sum(1 for r in rows if r.is_period)
    sst = ''.join(f'<si><t xml:space="preserve">{_xml_text(v)}</t></si>' for v in strings)
    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{total_refs}" uniqueCount="{len(strings)}">{sst}</sst>'
    )
    title = re.sub(r'[\[\]:*?/\\]', ' ', sheet_title).strip()[:31] or 'Лист1'
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{_xml_text(title)}" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('[Content_Types].xml', _XLSX_CONTENT_TYPES)
        zf.writestr('_rels/.rels', _XLSX_ROOT_RELS)
        zf.writestr('xl/workbook.xml', workbook_xml)
        zf.writestr('xl/_rels/workbook.xml.rels', _XLSX_WORKBOOK_RELS)
        zf.writestr('xl/styles.xml', _XLSX_STYLES)
        zf.writestr('xl/sharedStrings.xml', shared_xml)
        zf.writestr('xl/worksheets/sheet1.xml', sheet_xml)
    return buf.getvalue()


def export_filename(schema_name: str, start: date, end: date, fmt: str) -> str:
    """podarki_Birfest_2026-09-01_2026-09-24.xlsx (только ASCII — без проблем с заголовком)."""
    safe = re.sub(r'[^A-Za-z0-9_-]+', '', schema_name or '') or 'loyalup'
    return f'podarki_{safe}_{start.isoformat()}_{end.isoformat()}.{fmt}'


def render_file(rows, fmt: str) -> bytes:
    if fmt == FORMAT_XLSX:
        return rows_to_xlsx(rows)
    if fmt == FORMAT_CSV:
        return rows_to_csv(rows)
    raise ValueError(f'Неизвестный формат: {fmt!r}')


def file_response(rows, fmt: str, *, schema_name: str, start: date, end: date):
    """HttpResponse-вложение с файлом выгрузки (веб-страница и API)."""
    from django.http import HttpResponse
    resp = HttpResponse(render_file(rows, fmt), content_type=CONTENT_TYPES[fmt])
    resp['Content-Disposition'] = (
        f'attachment; filename="{export_filename(schema_name, start, end, fmt)}"'
    )
    resp['Cache-Control'] = 'no-store'
    return resp


def row_to_dict(row: ExportRow) -> dict:
    """JSON-представление строки для API (даты ISO — удобнее разбирать в 1С)."""
    return {
        'bar': row.bar,
        'branch_id': row.branch_id,
        'date_from': row.date_from.isoformat(),
        'date_to': row.date_to.isoformat(),
        'date_label': row.date_label,
        'name': row.name,
        'qty': row.qty,
    }
