"""
Каталог наград (пул призов RFM и RF-авторассылок) для кабинета CheckUp —
контракт платформы v1.8, возможность №49.

ЧТО ЭТО
───────
`RewardCatalogItem` (inventory/models.py:720) — тонкая надстройка над подарком
`catalog.Product`: тир G1/G2/G3, вес выбора внутри тира, лимит выдач, срок
жизни и период доступности. Сам подарок остаётся Product — каталог наград его
НЕ дублирует. Позицию выбирает движок (`inventory/reward_catalog.py`), когда
RFM-кампания или RF-авторассылка кладёт гостю подарок.

ПОЧЕМУ МОДУЛЬ ПЕРЕКРЫВАЕТ СТАРЫЙ ПУТЬ, А НЕ ПРАВИТ ЕГО
──────────────────────────────────────────────────────
`GET /api/v1/analytics/rf/reward-catalog/` уже жил в
`analytics/api/views.py:1746` (RFMRewardCatalogAPIView) и отдавал плоский
список для выпадашки «награда кампании». Ломать его нельзя — на нём висит веб
RFM. Поэтому ручка не правится: `main/urls.py` подключает ЭТОТ urls-модуль
ВЫШЕ `analytics.api.urls`, и Django резолвит путь сюда, а старая вьюха
остаётся в коде нетронутой (её маршрут просто перестаёт срабатывать).

Отсюда жёсткое требование: ответ GET — НАДМНОЖЕСТВО старого. Ключ `items`
сохранён, старые ключи (`id`, `name`, `tier`, `cost_price`,
`min_order_amount`, `default_lifetime_days`, `remaining_issues`, `branch` —
ИМЯ точки или null) сохранены с теми же типами, а без query-параметров
сохранён и старый фильтр: активные, не архивные, `available_for_rfm`, с
привязанным подарком, в периоде. Тест `LegacyShapeTest` держит это гвоздями.

РЕЖИМЫ СПИСКА
─────────────
  • без `include_*`            — «как старая ручка»: то, что кампания реально
                                 может назначить прямо сейчас;
  • `include_inactive=1`       — рабочий каталог целиком: выключенные, без
                                 подарка, без `available_for_rfm` и вне
                                 периода (экран редактирования);
  • `include_archived=1`       — плюс архив.
Любой из флагов переводит список в «режим каталога» (фильтры пригодности к
RFM снимаются) — иначе редактор не увидел бы позицию, которую сам выключил.

РЕШЕНИЯ, ПРИНЯТЫЕ СОЗНАТЕЛЬНО
─────────────────────────────
  • RBAC: позиция БЕЗ точки (branch=null) — сетевая, её видят все; точечная
    видна только тем, у кого есть доступ к её точке. Чужая или
    несуществующая позиция — всегда `404 not_found`, существование не
    раскрываем (как в рассылках, senler/api/broadcasts.py).
  • `branch_id` в карточке — ВНУТРЕННИЙ id точки (PK), как в точках контакта;
    `branch` рядом — ИМЯ точки, это старый ключ.
  • Писать (POST/PATCH/DELETE) может администратор сети и суперадмин; роль
    `client` — только читать (`403 role_not_allowed`). Сетевую позицию
    (`branch_id: null`) заводит только тот, у кого нет ограничения по точкам:
    иначе сотрудник одной точки выпустил бы приз на всю сеть.
  • Картинка (`image`) в v1 НЕ принимается: позиция берёт картинку подарка
    (`display_image`), multipart кабинету не нужен. Ключ `image` в теле —
    `400 invalid_payload` со словами, а не тихое игнорирование.
  • `issued_count` считает система (движок выдачи, `register_issue`). В теле
    запроса — `400 invalid_payload` с `read_only`.
  • Удаления НЕТ: DELETE архивирует (`is_archived=True, is_active=False`).
    На позиции висят выданные подарки гостей (`InventoryItem.catalog_item`) и
    история кампаний — физическое удаление обнулило бы их (паттерн 3 CLAUDE.md).
  • «Занята» (`in_use`) = есть RFM-кампания в статусе `processing` ЛИБО живые
    выданные подарки. Живой подарок — то же условие, что гейт движка
    (senler/engine.py:694): не использован и либо не активирован и срок забора
    не вышел, либо активирован и не истёк. Пока позиция занята, у неё нельзя
    менять подарок, тир и точку — гость уже держит в руках карточку с этими
    данными. Остальные поля (название, вес, лимит, срок, флаги) можно всегда.
  • Предупреждение `last_item_in_tier` — НЕ блокер: архивируя последнюю живую
    позицию тира, на который настроена включённая авторассылка, кабинет
    получает `warning` и список правил. Правило после этого уйдёт на запасной
    текст M0 (senler/engine.py), но само не сломается.

Ошибки — единой формой `{code, detail}`: `invalid_payload`, `product_required`,
`tier_invalid`, `period_invalid` (400), `role_not_allowed` (403), `not_found`
(404), `limit_below_issued`, `in_use` (409).
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from decimal import Decimal, InvalidOperation

from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from drf_spectacular.utils import (
    OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer,
)
from rest_framework import serializers as drf_serializers
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.users.access import current_schema_name, effective_branch_ids
from apps.tenant.analytics.models import RFMCampaign, RFMCampaignStatus
from apps.tenant.branch.models import Branch
from apps.tenant.catalog.models import Product
from apps.tenant.inventory.models import InventoryItem, RewardCatalogItem, RewardTier
from apps.tenant.senler.models import AutoBroadcastRule

log = logging.getLogger(__name__)

MAX_LIMIT = 200
MAX_NAME_LEN = 255
MAX_CODE_LEN = 64
# Сколько кампаний перечислить в 409 in_use: кабинету нужно показать, из-за
# чего заблокировано, а не выгрузить историю.
CAMPAIGNS_IN_ERROR = 20

TIERS = [{'code': code, 'label': label} for code, label in RewardTier.choices]
TIER_LABELS = {row['code']: row['label'] for row in TIERS}

# Поля, которые принимает POST (те же принимает PATCH).
CREATE_FIELDS = (
    'product_id', 'tier', 'name', 'internal_code', 'description',
    'cost_price', 'min_order_amount', 'weight', 'default_lifetime_days',
    'activation_limit', 'available_from', 'available_to', 'branch_id',
    'available_for_rfm', 'is_active',
)
# PATCH дополнительно умеет вернуть позицию из архива: DELETE туда кладёт, и
# без этого ключа дороги обратно из кабинета не было бы.
PATCH_FIELDS = CREATE_FIELDS + ('is_archived',)
# Считает система или собирает ответ — в теле запроса это ошибка, а не шум.
READ_ONLY = (
    'id', 'issued_count', 'remaining_issues', 'is_available_now', 'in_use',
    'created_at', 'updated_at', 'tier_label', 'product', 'branch', 'image_url',
)
# Поля, которые нельзя менять, пока позиция занята (подарок уже у гостей).
LOCKED_FIELDS = ('product_id', 'tier', 'branch_id')


# ── общие помощники (форма как в senler/api/broadcasts.py) ───────────────────

def _error(code: str, detail: str, status_code: int, **extra):
    payload = {'code': code, 'detail': detail}
    payload.update(extra)
    return Response(payload, status=status_code)


def _iso(value):
    return value.isoformat() if value else None


def _allowed_branches(request):
    """None — доступны все точки; [-1] — доступа нет; иначе список внутренних id."""
    return effective_branch_ids(request.user, current_schema_name(), None)


def _may_write(user) -> bool:
    """Править каталог наград может админ сети и суперадмин (роль client — нет)."""
    return bool(getattr(user, 'is_superuser', False)
                or getattr(user, 'is_superadmin', False)
                or getattr(user, 'is_network_admin', False))


def _may_write_network(user, allowed) -> bool:
    """Сетевую позицию (на всю сеть) заводит только неограниченный по точкам."""
    return _may_write(user) and allowed is None


def _page_params(request):
    """
    limit/offset. `limit` не передан — отдаём весь список (каталог по ТЗ это
    7-10 позиций, страницы ему не нужны), а в ответе `limit` = `total`.
    """
    raw = request.query_params.get('limit')
    if raw in (None, ''):
        limit = None
    else:
        try:
            limit = max(1, min(int(raw), MAX_LIMIT))
        except (TypeError, ValueError):
            limit = MAX_LIMIT
    try:
        offset = max(0, int(request.query_params.get('offset') or 0))
    except (TypeError, ValueError):
        offset = 0
    return limit, offset


def _flag(request, name) -> bool:
    return str(request.query_params.get(name) or '').strip().lower() in ('1', 'true', 'yes')


def _network_domain() -> str:
    """
    Primary-домен сети, например `levone.levelupapp.ru`.

    Из НЕГО собирается ссылка на картинку, а не из запроса: CheckUp ходит к
    нам через loopback с подменённым `Host`, и `build_absolute_uri` дал бы
    `http://127.0.0.1:7000/media/...` (★5 ревью CheckUp, тот же приём в
    branch/api/story_settings.py:249).
    """
    from django.db import connection
    from apps.shared.clients.models import Domain
    company = getattr(connection, 'tenant', None)
    domain = (Domain.objects.filter(tenant=company, is_primary=True).first()
              or Domain.objects.filter(tenant=company).first())
    return domain.domain if domain else ''


def _image_url(item) -> str | None:
    """Своя картинка позиции, иначе картинка подарка. Полным адресом."""
    try:
        image = item.display_image
        if not image or not getattr(image, 'name', ''):
            return None
        domain = _network_domain()
        return f'https://{domain}{image.url}' if domain else image.url
    except Exception:  # картинка не повод отдать 500
        return None


# ── занятость позиции ────────────────────────────────────────────────────────

def _live_gift_q(now):
    """
    Живой выданный подарок — ТО ЖЕ условие, что гейт движка
    (senler/engine.py:694-706): не использован и либо не активирован и срок
    забора не вышел, либо активирован и не истёк.
    """
    return (
        Q(activated_at__isnull=True)
        & (Q(claim_expires_at__isnull=True) | Q(claim_expires_at__gt=now))
        | Q(activated_at__isnull=False)
        & (Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    )


def _in_use_map(item_ids, now=None) -> dict:
    """{id: {'campaigns': N, 'live_gifts': N}} — двумя сгруппированными запросами."""
    ids = [int(i) for i in (item_ids or [])]
    out = {i: {'campaigns': 0, 'live_gifts': 0} for i in ids}
    if not ids:
        return out
    now = now or timezone.now()
    rows = (RFMCampaign.objects
            .filter(catalog_item_id__in=ids, status=RFMCampaignStatus.PROCESSING)
            .values('catalog_item_id').annotate(n=Count('id')))
    for row in rows:
        out[int(row['catalog_item_id'])]['campaigns'] = row['n']
    rows = (InventoryItem.objects
            .filter(catalog_item_id__in=ids, used_at__isnull=True)
            .filter(_live_gift_q(now))
            .values('catalog_item_id').annotate(n=Count('id')))
    for row in rows:
        out[int(row['catalog_item_id'])]['live_gifts'] = row['n']
    return out


def _processing_campaigns(item_id) -> list[dict]:
    """Кампании, которые прямо сейчас начисляют эту позицию."""
    qs = (RFMCampaign.objects
          .filter(catalog_item_id=item_id, status=RFMCampaignStatus.PROCESSING)
          .order_by('-pk')[:CAMPAIGNS_IN_ERROR])
    return [{'id': c.pk, 'name': c.name, 'status': c.status} for c in qs]


def _tier_warning(item) -> dict | None:
    """
    Последняя живая позиция тира ушла, а на тир настроена включённая
    авторассылка → предупреждаем (не блокируем): правило уйдёт на запасной
    текст M0. `gift_tier` — строка вида 'G1' или 'G1,G2', FK там нет.
    """
    try:
        left = (RewardCatalogItem.objects
                .filter(tier=item.tier, is_active=True, is_archived=False)
                .exclude(pk=item.pk).count())
        if left:
            return None
        rules = []
        for rule in AutoBroadcastRule.objects.filter(
                is_active=True, is_archived=False, gift_tier__icontains=item.tier):
            codes = {part.strip().upper()
                     for part in (rule.gift_tier or '').split(',') if part.strip()}
            if item.tier in codes:
                rules.append({'id': rule.pk, 'name': rule.name})
        if not rules:
            return None
        return {'warning': 'last_item_in_tier', 'rules': rules}
    except Exception:  # предупреждение не повод уронить сохранение
        log.exception('reward catalog: tier warning failed')
        return None


# ── карточка ─────────────────────────────────────────────────────────────────

def _card(item, in_use=None) -> dict:
    """
    Карточка позиции. Старые ключи ручки analytics сохранены с теми же типами
    (`name`, `tier`, `cost_price`, `min_order_amount`, `default_lifetime_days`,
    `remaining_issues`, `branch` — имя точки или null).
    """
    product = item.product if item.product_id else None
    return {
        'id': item.pk,
        'name': item.display_name,
        'tier': item.tier,
        'tier_label': TIER_LABELS.get(item.tier, item.tier),
        'product': {'id': product.pk, 'name': product.name} if product else None,
        'product_id': item.product_id,
        'internal_code': item.internal_code or '',
        'description': item.display_description,
        'image_url': _image_url(item),
        'cost_price': float(item.effective_cost_price or 0),
        'min_order_amount': float(item.min_order_amount or 0),
        'weight': item.weight,
        'default_lifetime_days': item.default_lifetime_days,
        'activation_limit': item.activation_limit,
        'issued_count': item.issued_count,
        'remaining_issues': item.remaining_issues,
        'available_from': _iso(item.available_from),
        'available_to': _iso(item.available_to),
        'branch_id': item.branch_id,
        'branch': item.branch.name if item.branch_id else None,
        'is_active': item.is_active,
        'is_archived': item.is_archived,
        'available_for_rfm': item.available_for_rfm,
        'is_available_now': item.is_available_now(),
        'in_use': dict(in_use or {'campaigns': 0, 'live_gifts': 0}),
        'created_at': _iso(item.created_at),
        'updated_at': _iso(item.updated_at),
    }


def _cards(items) -> list[dict]:
    in_use = _in_use_map([i.pk for i in items])
    return [_card(i, in_use.get(i.pk)) for i in items]


# ── выборка ──────────────────────────────────────────────────────────────────

def _visibility_q(allowed):
    """Сетевая позиция видна всем; точечная — только при доступе к её точке."""
    if allowed is None:
        return None
    return Q(branch__isnull=True) | Q(branch_id__in=list(allowed))


def _catalog_queryset(*, allowed, now, include_inactive=False, include_archived=False,
                      tiers=None, branch_scope=None):
    qs = RewardCatalogItem.objects.select_related('product', 'branch')
    vis = _visibility_q(allowed)
    if vis is not None:
        qs = qs.filter(vis)
    if not include_archived:
        qs = qs.filter(is_archived=False)
    if not include_inactive:
        qs = qs.filter(is_active=True)
    if not include_inactive and not include_archived:
        # Режим старой ручки: только то, что кампания может назначить сейчас.
        qs = qs.filter(available_for_rfm=True, product__isnull=False)
        qs = qs.filter(Q(available_from__isnull=True) | Q(available_from__lte=now),
                       Q(available_to__isnull=True) | Q(available_to__gte=now))
    if tiers:
        qs = qs.filter(tier__in=list(tiers))
    if branch_scope:
        # Сетевые позиции работают на любой точке — их фильтр по точкам не прячет.
        qs = qs.filter(Q(branch__isnull=True) | Q(branch_id__in=list(branch_scope)))
    return qs.order_by('tier', 'name', 'pk')


def _get_item(pk, allowed):
    """Позиция с учётом прав. None — нет такой или чужая (отвечаем 404)."""
    qs = RewardCatalogItem.objects.select_related('product', 'branch').filter(pk=pk)
    vis = _visibility_q(allowed)
    if vis is not None:
        qs = qs.filter(vis)
    return qs.first()


def _branch_or_none(branch_id, allowed):
    qs = Branch.objects.filter(pk=branch_id)
    if allowed is not None:
        qs = qs.filter(pk__in=list(allowed))
    return qs.first()


def _product_or_none(product_id):
    """Архивный подарок выдавать нельзя — для каталога его как бы нет."""
    return Product.objects.filter(pk=product_id, is_archived=False).first()


# ── разбор тела запроса ──────────────────────────────────────────────────────

class PayloadError(Exception):
    """Беда в теле запроса: code + detail + доп. ключи единой формы ошибки."""

    def __init__(self, code: str, detail: str, **extra):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = extra


def _parse_str(field, raw, max_len):
    if raw is None:
        return ''
    text = str(raw).strip()
    if max_len and len(text) > max_len:
        raise PayloadError('invalid_payload', f'{field}: не длиннее {max_len} символов')
    return text


def _parse_int(field, raw, *, allow_null=False):
    if raw is None or raw == '':
        if allow_null:
            return None
        raise PayloadError('invalid_payload', f'{field}: целое число')
    if isinstance(raw, bool):
        raise PayloadError('invalid_payload', f'{field}: целое число')
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise PayloadError('invalid_payload', f'{field}: целое число')
    if value < 0:
        raise PayloadError('invalid_payload', f'{field}: не может быть отрицательным')
    return value


def _parse_money(field, raw):
    if raw is None or raw == '':
        return Decimal('0')
    if isinstance(raw, bool):
        raise PayloadError('invalid_payload', f'{field}: число в рублях')
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise PayloadError('invalid_payload', f'{field}: число в рублях')
    if value < 0:
        raise PayloadError('invalid_payload', f'{field}: не может быть отрицательным')
    return value.quantize(Decimal('0.01'))


def _parse_bool(field, raw):
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ('true', '1', 'yes'):
        return True
    if text in ('false', '0', 'no'):
        return False
    raise PayloadError('invalid_payload', f'{field}: true или false')


def _parse_dt(field, raw):
    if raw in (None, ''):
        return None
    value = parse_datetime(str(raw))
    if value is None:
        day = parse_date(str(raw))
        if day is None:
            raise PayloadError('invalid_payload',
                               f'{field}: дата и время в формате ISO 8601 '
                               '(2026-09-20T10:00:00+03:00) либо ГГГГ-ММ-ДД')
        value = datetime.combine(day, time.min)
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return value


def _parse_tier(raw):
    tier = str(raw or '').strip().upper()
    if tier not in set(RewardTier.values):
        raise PayloadError('tier_invalid', 'tier: ' + ' | '.join(RewardTier.values))
    return tier


def _collect(data, *, editable):
    """
    Белый список: read-only и чужие ключи — ошибка, а не тихое молчание.

    Возвращает {api-имя поля: разобранное значение} только для переданных
    ключей — PATCH правит ровно то, что прислали.
    """
    data = data or {}
    keys = list(data.keys())

    read_only = sorted(k for k in keys if k in READ_ONLY)
    if read_only:
        raise PayloadError('invalid_payload',
                           'эти поля считает система: ' + ', '.join(read_only),
                           read_only=read_only, editable=list(editable))
    if 'image' in keys:
        raise PayloadError('invalid_payload',
                           'image: картинка в v1 не принимается — позиция показывает '
                           'картинку выбранного подарка',
                           editable=list(editable))
    unknown = sorted(k for k in keys if k not in editable)
    if unknown:
        raise PayloadError('invalid_payload', 'неизвестные поля: ' + ', '.join(unknown),
                           editable=list(editable))

    values = {}
    if 'product_id' in data:
        raw = data['product_id']
        if raw in (None, ''):
            raise PayloadError('product_required',
                               'product_id: выберите подарок — без него гостю нечего '
                               'показать в «Моих подарках»')
        values['product_id'] = _parse_int('product_id', raw)
    if 'tier' in data:
        values['tier'] = _parse_tier(data['tier'])
    if 'name' in data:
        values['name'] = _parse_str('name', data['name'], MAX_NAME_LEN)
    if 'internal_code' in data:
        values['internal_code'] = _parse_str('internal_code', data['internal_code'], MAX_CODE_LEN)
    if 'description' in data:
        values['description'] = _parse_str('description', data['description'], 0)
    if 'cost_price' in data:
        values['cost_price'] = _parse_money('cost_price', data['cost_price'])
    if 'min_order_amount' in data:
        values['min_order_amount'] = _parse_money('min_order_amount', data['min_order_amount'])
    if 'weight' in data:
        values['weight'] = _parse_int('weight', data['weight'])
    if 'default_lifetime_days' in data:
        values['default_lifetime_days'] = _parse_int('default_lifetime_days',
                                                     data['default_lifetime_days'])
    if 'activation_limit' in data:
        values['activation_limit'] = _parse_int('activation_limit', data['activation_limit'],
                                                allow_null=True)
    if 'available_from' in data:
        values['available_from'] = _parse_dt('available_from', data['available_from'])
    if 'available_to' in data:
        values['available_to'] = _parse_dt('available_to', data['available_to'])
    if 'branch_id' in data:
        raw = data['branch_id']
        values['branch_id'] = None if raw in (None, '') else _parse_int('branch_id', raw)
    if 'available_for_rfm' in data:
        values['available_for_rfm'] = _parse_bool('available_for_rfm', data['available_for_rfm'])
    if 'is_active' in data:
        values['is_active'] = _parse_bool('is_active', data['is_active'])
    if 'is_archived' in data:
        values['is_archived'] = _parse_bool('is_archived', data['is_archived'])
    return values


def _check_period(start, end):
    if start and end and start > end:
        raise PayloadError('period_invalid',
                           'available_from не может быть позже available_to')


# ── OpenAPI (только описание, на поведение не влияет) ────────────────────────

ERROR_CODES = [
    ('invalid_payload',    '400 — тело запроса не разобрано (+editable, +read_only)'),
    ('product_required',   '400 — не выбран подарок'),
    ('tier_invalid',       '400 — неизвестный тир'),
    ('period_invalid',     '400 — available_from позже available_to'),
    ('role_not_allowed',   '403 — править каталог может только администратор сети'),
    ('not_found',          '404 — позиция/точка/подарок не найдены или вне доступа'),
    ('limit_below_issued', '409 — лимит меньше уже выданного'),
    ('in_use',             '409 — позиция занята (+campaigns, +live_gifts, +blocked_fields)'),
]

_ERR = inline_serializer('RewardCatalogError', fields={
    'code': drf_serializers.ChoiceField(choices=ERROR_CODES),
    'detail': drf_serializers.CharField(),
})


def _card_schema(name='RewardCatalogCard', extra=None, **kw):
    fields = {
        'id': drf_serializers.IntegerField(),
        'name': drf_serializers.CharField(help_text='display_name: своё или название подарка'),
        'tier': drf_serializers.ChoiceField(choices=RewardTier.choices),
        'tier_label': drf_serializers.CharField(),
        'product': drf_serializers.DictField(allow_null=True, help_text='{id, name} | null'),
        'product_id': drf_serializers.IntegerField(allow_null=True),
        'internal_code': drf_serializers.CharField(allow_blank=True),
        'description': drf_serializers.CharField(allow_blank=True),
        'image_url': drf_serializers.CharField(allow_null=True),
        'cost_price': drf_serializers.FloatField(help_text='effective: своя либо подарка'),
        'min_order_amount': drf_serializers.FloatField(),
        'weight': drf_serializers.IntegerField(help_text='0 — запасная позиция'),
        'default_lifetime_days': drf_serializers.IntegerField(),
        'activation_limit': drf_serializers.IntegerField(allow_null=True, help_text='null — без лимита'),
        'issued_count': drf_serializers.IntegerField(read_only=True),
        'remaining_issues': drf_serializers.IntegerField(allow_null=True),
        'available_from': drf_serializers.DateTimeField(allow_null=True),
        'available_to': drf_serializers.DateTimeField(allow_null=True),
        'branch_id': drf_serializers.IntegerField(allow_null=True, help_text='внутренний id точки; null — вся сеть'),
        'branch': drf_serializers.CharField(allow_null=True, help_text='имя точки (старый ключ)'),
        'is_active': drf_serializers.BooleanField(),
        'is_archived': drf_serializers.BooleanField(),
        'available_for_rfm': drf_serializers.BooleanField(),
        'is_available_now': drf_serializers.BooleanField(),
        'in_use': drf_serializers.DictField(help_text='{campaigns, live_gifts}'),
        'created_at': drf_serializers.DateTimeField(),
        'updated_at': drf_serializers.DateTimeField(),
    }
    fields.update(extra or {})
    return inline_serializer(name, fields=fields, **kw)


_LIST_OUT = inline_serializer('RewardCatalogList', fields={
    # Отдельное имя компонента: два инлайн-сериализатора с ОДНИМ именем
    # drf-spectacular считает конфликтом схемы (предупреждение в срезе).
    'items': _card_schema('RewardCatalogCardRow', many=True),
    'total': drf_serializers.IntegerField(),
    'limit': drf_serializers.IntegerField(),
    'offset': drf_serializers.IntegerField(),
    'tiers': drf_serializers.ListField(child=drf_serializers.DictField(),
                                       help_text='[{code, label}] — справочник тиров'),
})
_CARD_OUT = _card_schema()
# Ответ PATCH — та же карточка плюс список изменённых полей и (не всегда)
# предупреждение про последнюю позицию тира.
_PATCH_OUT = _card_schema('RewardCatalogPatchedCard', extra={
    'changed': drf_serializers.ListField(child=drf_serializers.CharField()),
    'warning': drf_serializers.CharField(required=False, allow_null=True,
                                         help_text='last_item_in_tier'),
    'rules': drf_serializers.ListField(required=False, child=drf_serializers.DictField(),
                                       help_text='[{id, name}] — авторассылки этого тира'),
})
_DELETE_OUT = inline_serializer('RewardCatalogArchived', fields={
    'id': drf_serializers.IntegerField(),
    'is_archived': drf_serializers.BooleanField(),
    'is_active': drf_serializers.BooleanField(),
    'warning': drf_serializers.CharField(required=False, allow_null=True),
    'rules': drf_serializers.ListField(required=False, child=drf_serializers.DictField()),
})
_IN = inline_serializer('RewardCatalogIn', fields={
    'product_id': drf_serializers.IntegerField(),
    'tier': drf_serializers.ChoiceField(choices=RewardTier.choices),
    'name': drf_serializers.CharField(required=False, allow_blank=True),
    'internal_code': drf_serializers.CharField(required=False, allow_blank=True),
    'description': drf_serializers.CharField(required=False, allow_blank=True),
    'cost_price': drf_serializers.FloatField(required=False),
    'min_order_amount': drf_serializers.FloatField(required=False),
    'weight': drf_serializers.IntegerField(required=False),
    'default_lifetime_days': drf_serializers.IntegerField(required=False),
    'activation_limit': drf_serializers.IntegerField(required=False, allow_null=True),
    'available_from': drf_serializers.DateTimeField(required=False, allow_null=True),
    'available_to': drf_serializers.DateTimeField(required=False, allow_null=True),
    'branch_id': drf_serializers.IntegerField(required=False, allow_null=True),
    'available_for_rfm': drf_serializers.BooleanField(required=False),
    'is_active': drf_serializers.BooleanField(required=False),
})


# ── список и создание ────────────────────────────────────────────────────────

class RewardCatalogListCreateAPIView(APIView):
    """
    GET  /api/v1/analytics/rf/reward-catalog/ — пул наград.
    POST /api/v1/analytics/rf/reward-catalog/ — завести позицию.

    Ответ GET — надмножество старой ручки `analytics/api/views.py:1746`:
    ключ `items` и все её поля на месте, поэтому существующий веб RFM
    продолжает работать без правок.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        parameters=[
            OpenApiParameter('include_inactive', bool, description='1 — показать выключенные, без подарка, вне периода'),
            OpenApiParameter('include_archived', bool, description='1 — показать архивные'),
            OpenApiParameter('tier', str, description='G1 | G2 | G3, можно через запятую'),
            OpenApiParameter('branch_ids', str, description='внутренние id точек через запятую (сетевые позиции остаются)'),
            OpenApiParameter('limit', int, description=f'не передан — весь список; потолок {MAX_LIMIT}'),
            OpenApiParameter('offset', int),
        ],
        responses={200: _LIST_OUT, 400: _ERR}, tags=['v1'])
    def get(self, request):
        allowed = _allowed_branches(request)
        now = timezone.now()

        raw_tier = (request.query_params.get('tier') or '').strip()
        tiers = []
        if raw_tier:
            try:
                tiers = [_parse_tier(part) for part in raw_tier.split(',') if part.strip()]
            except PayloadError as exc:
                return _error(exc.code, exc.detail, http_status.HTTP_400_BAD_REQUEST, **exc.extra)

        raw_branches = (request.query_params.get('branch_ids') or '').strip()
        branch_scope = None
        if raw_branches:
            try:
                requested = [int(x) for x in raw_branches.split(',') if x.strip()]
            except (TypeError, ValueError):
                return _error('invalid_payload', 'branch_ids: список внутренних id точек через запятую',
                              http_status.HTTP_400_BAD_REQUEST)
            # requested непустой — effective_branch_ids вернёт пересечение
            # (или [-1], если точки чужие). Пустой список сюда НЕ передаём:
            # для него это означало бы «все точки».
            if requested:
                branch_scope = effective_branch_ids(request.user, current_schema_name(), requested)

        qs = _catalog_queryset(
            allowed=allowed, now=now,
            include_inactive=_flag(request, 'include_inactive'),
            include_archived=_flag(request, 'include_archived'),
            tiers=tiers, branch_scope=branch_scope,
        )
        total = qs.count()
        limit, offset = _page_params(request)
        # `limit` в ответе — это всегда размер страницы, которую вернули:
        # без параметра страница = остаток списка, и молча проглоченного
        # offset не остаётся.
        page = list(qs[offset:offset + limit]) if limit else list(qs[offset:])
        return Response({
            'items': _cards(page),
            'total': total,
            'limit': limit if limit else max(total - offset, 0),
            'offset': offset,
            'tiers': TIERS,
        })

    @extend_schema(request=_IN, responses={201: _CARD_OUT, 400: _ERR, 403: _ERR, 404: _ERR},
                   tags=['v1'])
    def post(self, request):
        if not _may_write(request.user):
            return _error('role_not_allowed',
                          'заводить награды может только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        allowed = _allowed_branches(request)
        try:
            values = _collect(request.data or {}, editable=CREATE_FIELDS)
            if 'product_id' not in values:
                raise PayloadError('product_required',
                                   'product_id: выберите подарок — без него гостю нечего '
                                   'показать в «Моих подарках»')
            if 'tier' not in values:
                raise PayloadError('tier_invalid', 'tier: ' + ' | '.join(RewardTier.values))
            _check_period(values.get('available_from'), values.get('available_to'))
        except PayloadError as exc:
            return _error(exc.code, exc.detail, http_status.HTTP_400_BAD_REQUEST, **exc.extra)

        branch = None
        branch_id = values.get('branch_id')
        if branch_id is None:
            if not _may_write_network(request.user, allowed):
                return _error('role_not_allowed',
                              'позицию на всю сеть (без точки) заводит только администратор '
                              'сети без ограничения по точкам',
                              http_status.HTTP_403_FORBIDDEN)
        else:
            branch = _branch_or_none(branch_id, allowed)
            if branch is None:
                return _error('not_found', 'Точка не найдена', http_status.HTTP_404_NOT_FOUND)

        product = _product_or_none(values['product_id'])
        if product is None:
            return _error('not_found', 'Подарок не найден или в архиве',
                          http_status.HTTP_404_NOT_FOUND)

        item = RewardCatalogItem.objects.create(
            product=product,
            branch=branch,
            tier=values['tier'],
            name=values.get('name', ''),
            internal_code=values.get('internal_code', ''),
            description=values.get('description', ''),
            cost_price=values.get('cost_price', Decimal('0')),
            min_order_amount=values.get('min_order_amount', Decimal('0')),
            weight=values.get('weight', 1),
            default_lifetime_days=values.get('default_lifetime_days', 10),
            activation_limit=values.get('activation_limit'),
            available_from=values.get('available_from'),
            available_to=values.get('available_to'),
            available_for_rfm=values.get('available_for_rfm', True),
            is_active=values.get('is_active', True),
        )
        log.info('reward catalog item created: %s item=%s tier=%s branch=%s by=%s',
                 current_schema_name(), item.pk, item.tier, branch.pk if branch else None,
                 request.user)
        return Response(_card(item), status=http_status.HTTP_201_CREATED)


# ── карточка, правка, архив ──────────────────────────────────────────────────

class RewardCatalogDetailAPIView(APIView):
    """
    GET    /api/v1/analytics/rf/reward-catalog/{id}/ — карточка позиции.
    PATCH  …/{id}/ — правка. Подарок, тир и точку нельзя менять, пока позиция
           занята (`409 in_use`); лимит нельзя опустить ниже выданного
           (`409 limit_below_issued`). Остальные поля — всегда.
    DELETE …/{id}/ — архив (`is_archived=True, is_active=False`), физического
           удаления нет.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: _CARD_OUT, 404: _ERR}, tags=['v1'])
    def get(self, request, pk: int):
        item = _get_item(pk, _allowed_branches(request))
        if item is None:
            return _error('not_found', 'Позиция каталога не найдена',
                          http_status.HTTP_404_NOT_FOUND)
        return Response(_card(item, _in_use_map([item.pk]).get(item.pk)))

    @extend_schema(request=_IN, responses={
        200: OpenApiResponse(_PATCH_OUT, description='карточка + changed[] (+warning/rules)'),
        400: _ERR, 403: _ERR, 404: _ERR, 409: _ERR}, tags=['v1'])
    def patch(self, request, pk: int):
        if not _may_write(request.user):
            return _error('role_not_allowed',
                          'править награды может только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        allowed = _allowed_branches(request)
        item = _get_item(pk, allowed)
        if item is None:
            return _error('not_found', 'Позиция каталога не найдена',
                          http_status.HTTP_404_NOT_FOUND)
        try:
            values = _collect(request.data or {}, editable=PATCH_FIELDS)
            _check_period(values.get('available_from', item.available_from),
                          values.get('available_to', item.available_to))
        except PayloadError as exc:
            return _error(exc.code, exc.detail, http_status.HTTP_400_BAD_REQUEST, **exc.extra)
        if not values:
            return _error('invalid_payload', 'нечего менять: передайте хотя бы одно поле',
                          http_status.HTTP_400_BAD_REQUEST, editable=list(PATCH_FIELDS))

        # Лимит ниже уже выданного = «выдали больше, чем разрешено»: цифра
        # выдач правде не соответствовала бы, а позиция молча умерла бы.
        limit = values.get('activation_limit', item.activation_limit)
        if limit is not None and limit < (item.issued_count or 0):
            return _error('limit_below_issued',
                          f'activation_limit: уже выдано {item.issued_count} — лимит не может '
                          'быть меньше',
                          http_status.HTTP_409_CONFLICT, issued_count=item.issued_count)

        # Что реально меняется (а не просто прислано тем же значением).
        current = {'product_id': item.product_id, 'tier': item.tier, 'branch_id': item.branch_id}
        blocked = [f for f in LOCKED_FIELDS if f in values and values[f] != current[f]]
        if blocked:
            usage = _in_use_map([item.pk]).get(item.pk) or {'campaigns': 0, 'live_gifts': 0}
            if usage['campaigns'] or usage['live_gifts']:
                return _error('in_use',
                              'Позиция занята: подарок уже у гостей либо идёт начисление '
                              'кампании. Подарок, тир и точку менять нельзя — выключите '
                              'позицию и заведите новую.',
                              http_status.HTTP_409_CONFLICT,
                              campaigns=_processing_campaigns(item.pk),
                              live_gifts=usage['live_gifts'],
                              blocked_fields=blocked)

        changed, db_fields = [], []
        if 'product_id' in values and values['product_id'] != item.product_id:
            product = _product_or_none(values['product_id'])
            if product is None:
                return _error('not_found', 'Подарок не найден или в архиве',
                              http_status.HTTP_404_NOT_FOUND)
            item.product = product
            changed.append('product_id')
            db_fields.append('product')
        if 'branch_id' in values and values['branch_id'] != item.branch_id:
            if values['branch_id'] is None:
                if not _may_write_network(request.user, allowed):
                    return _error('role_not_allowed',
                                  'сделать позицию сетевой может только администратор сети '
                                  'без ограничения по точкам',
                                  http_status.HTTP_403_FORBIDDEN)
                item.branch = None
            else:
                branch = _branch_or_none(values['branch_id'], allowed)
                if branch is None:
                    return _error('not_found', 'Точка не найдена', http_status.HTTP_404_NOT_FOUND)
                item.branch = branch
            changed.append('branch_id')
            db_fields.append('branch')

        simple = ('tier', 'name', 'internal_code', 'description', 'cost_price',
                  'min_order_amount', 'weight', 'default_lifetime_days',
                  'activation_limit', 'available_from', 'available_to',
                  'available_for_rfm', 'is_active', 'is_archived')
        for field in simple:
            if field in values and values[field] != getattr(item, field):
                setattr(item, field, values[field])
                changed.append(field)
                db_fields.append(field)

        if db_fields:
            item.save(update_fields=sorted(set(db_fields)) + ['updated_at'])
            log.info('reward catalog item patched: %s item=%s fields=%s by=%s',
                     current_schema_name(), item.pk, changed, request.user)

        payload = _card(item, _in_use_map([item.pk]).get(item.pk))
        payload['changed'] = changed
        # Позицию выключили или убрали в архив — вдруг это была последняя в тире.
        if values.get('is_active') is False or values.get('is_archived') is True:
            warning = _tier_warning(item)
            if warning:
                payload.update(warning)
        return Response(payload)

    @extend_schema(responses={200: _DELETE_OUT, 403: _ERR, 404: _ERR, 409: _ERR}, tags=['v1'])
    def delete(self, request, pk: int):
        if not _may_write(request.user):
            return _error('role_not_allowed',
                          'убирать награды в архив может только администратор сети',
                          http_status.HTTP_403_FORBIDDEN)
        item = _get_item(pk, _allowed_branches(request))
        if item is None:
            return _error('not_found', 'Позиция каталога не найдена',
                          http_status.HTTP_404_NOT_FOUND)

        # Идёт начисление — позиция прямо сейчас раздаётся: убирать нельзя.
        # Уже выданные подарки архиву не мешают: они остаются у гостей и
        # активируются как обычно (паттерн «скрытие вместо удаления»).
        usage = _in_use_map([item.pk]).get(item.pk) or {'campaigns': 0, 'live_gifts': 0}
        if usage['campaigns']:
            return _error('in_use',
                          'По позиции идёт начисление RFM-кампании — дождитесь её завершения '
                          'или отмените кампанию.',
                          http_status.HTTP_409_CONFLICT,
                          campaigns=_processing_campaigns(item.pk),
                          live_gifts=usage['live_gifts'])

        item.is_archived = True
        item.is_active = False
        item.save(update_fields=['is_archived', 'is_active', 'updated_at'])
        log.info('reward catalog item archived: %s item=%s by=%s',
                 current_schema_name(), item.pk, request.user)
        payload = {'id': item.pk, 'is_archived': True, 'is_active': False}
        warning = _tier_warning(item)
        if warning:
            payload.update(warning)
        return Response(payload)
