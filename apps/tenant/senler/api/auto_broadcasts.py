"""
Конструктор авторассылок для внешнего кабинета CheckUp (контракт платформы,
возможность №31, разделы 3б.2 / 3б.6 / 3б.8).

ПОЧЕМУ ЭТОТ МОДУЛЬ ПЕРЕКРЫВАЕТ СТАРЫЕ ВЬЮХИ МОБИЛКИ
───────────────────────────────────────────────────
Пути ТЕ ЖЕ, что у мобильного приложения (`/api/v1/auto-broadcasts/…`), потому
что контракт требует надмножество, а не второй API. senler/api/urls.py
подключён в main/urls.py РАНЬШЕ mobile/api/urls.py (строка 32 против 39), то
есть запросы старой мобилки (список, PATCH текста/вкл-выкл, preview) приходят
сюда. Это осознанно: mobile/api/views.py:3068-3184 не тронут и остаётся путём
отката (достаточно убрать три path() из senler/api/urls.py).

Обязательства перед уже установленной мобилкой:
  1. карточка правила содержит ВСЕ старые ключи `_serialize_rule` с теми же
     значениями и типами (см. auto_broadcasts_serializers.rule_to_dict и тест
     LegacyCardKeysTest — список ключей зафиксирован);
  2. список отдаёт `{'rules': [...]}` (плюс total/limit/offset — аддитивно);
     без параметра `limit` возвращается ПОЛНЫЙ список, как раньше;
  3. PATCH принимает message_text / is_active от обычного пользователя без
     каких-либо новых обязательных полей;
  4. 404 несёт старый текст «Правило не найдено.» — теперь в поле `detail`
     новой формы ошибки {code, detail}.

ГЕЙТ ВКЛЮЧЕНИЯ (★10 ревью 18.09)
────────────────────────────────
Пользователь, созданный обменом токена CheckUp (есть CheckUpIdentity), НЕ
может включить правило через PATCH is_active=true → 400 use_activate. Из
кабинета включение только через POST …/activate/ с expected_count, как у
рассылок: включённое правило шлёт сообщения само, и цифру охвата человек
обязан увидеть до этого (ЧП 21.08 «рассылка на 1 ушла 612»).

ЧЕГО МОДУЛЬ НЕ ДЕЛАЕТ
─────────────────────
Не трогает engine.py / tasks.py: архив — это is_active=False плюс скрытие из
списка, движок смотрит только на is_active. Не удаляет правила и варианты
физически (на них история отправок). Не принимает картинки (как у рассылок).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.checkup.models import CheckUpIdentity
from apps.shared.users.access import current_schema_name, effective_branch_ids
from apps.tenant.analytics.models import RFSegment
from apps.tenant.branch.models import Branch, ClientBranch
from apps.tenant.senler.engine import (
    Candidate, get_events, pick_variant, render_text, resolve_recipients, rule_is_due, rule_stats,
)
from apps.tenant.senler.models import (
    AutoBroadcastRule, AutoBroadcastVariant, BroadcastRecipient, BroadcastSend,
    FollowUpCondition, GenderFilter, RecipientStatus,
)
from apps.tenant.senler.services import send_vk_message

from . import auto_broadcasts_schema as api_schema  # только OpenAPI, на поведение не влияет
from .auto_broadcasts_serializers import (
    DELAY_REQUIRED_EVENTS, EMPTY_STATS, GIFT_TIER_CODES, MAX_NAME_LEN, MAX_TEXT_LEN, MAX_VARIANT_NAME_LEN,
    event_to_dict, gift_tiers, log_row_to_dict, rule_to_dict, variant_to_dict,
)
from .guard import audience_changed

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
# ★37: тест-отправка — не чаще раза в минуту на правило.
TEST_SEND_TTL = 60

NOT_FOUND_DETAIL = 'Правило не найдено.'   # текст мобилки, менять нельзя


# ── Общие помощники ───────────────────────────────────────────────────────────

def _error(code: str, detail: str, status_code: int, **extra):
    """Единая форма ошибки кабинета: {'code': ..., 'detail': ...}."""
    payload = {'code': code, 'detail': detail}
    payload.update(extra)
    return Response(payload, status=status_code)


def _not_found():
    return _error('not_found', NOT_FOUND_DETAIL, http_status.HTTP_404_NOT_FOUND)


def _allowed_branches(request):
    """
    None — доступны все точки; [-1] — доступа нет; иначе список PK.

    ⚠️ requested обязан быть None: с пустым списком effective_branch_ids вернёт
    [] у неограниченного пользователя, и мы отфильтруем вообще всё.
    """
    return effective_branch_ids(request.user, current_schema_name(), None)


def _page_params(request):
    """limit/offset с потолком. limit_given=False → полный список (как раньше)."""
    raw_limit = request.query_params.get('limit')
    try:
        limit = int(raw_limit) if raw_limit not in (None, '') else DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        offset = int(request.query_params.get('offset') or 0)
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    return limit, offset, raw_limit not in (None, '')


def _confirmed(data) -> bool:
    value = data.get('confirm')
    if value is True:
        return True
    return str(value).strip().lower() in ('true', '1', 'yes')


def _is_checkup_user(user) -> bool:
    """
    Пользователь пришёл из обмена токена CheckUp (★10).

    Личность живёт в общей схеме (apps/shared/checkup). Если таблица почему-то
    недоступна — фолбэк на username checkup-<id>-<schema>, который выдаёт
    обмен: гейт лучше пережать, чем пропустить включение без expected_count.
    """
    try:
        return CheckUpIdentity.objects.filter(user=user).exists()
    except Exception:
        return str(getattr(user, 'username', '') or '').startswith('checkup-')


def _events() -> dict:
    try:
        return get_events()
    except Exception:
        return {}


def _rule_branch_ids(rule) -> list[int]:
    return [int(b.pk) for b in rule.branches.all()]


def _rule_visible(rule, allowed) -> bool:
    """
    Правило без точек — сетевое: его видит только пользователь без ограничений.
    Ограниченный видит правило, чьи точки целиком внутри его доступа.
    """
    if allowed is None:
        return True
    ids = set(_rule_branch_ids(rule))
    return bool(ids) and ids.issubset({int(x) for x in allowed})


def _rules_qs():
    return (
        AutoBroadcastRule.objects
        .all()
        .select_related('parent_rule')
        .prefetch_related('branches', 'rf_segments', 'variants')
        .order_by('event', '-priority', 'pk')
    )


def _load_rule(pk, allowed):
    """Правило с учётом RBAC. None — нет или невидимо (отвечаем 404)."""
    rule = _rules_qs().filter(pk=pk).first()
    if rule is None or not _rule_visible(rule, allowed):
        return None
    return rule


def _atomic():
    """
    Обёртка над transaction.atomic() отдельной функцией — чтобы тесты на моках
    (SimpleTestCase, БД запрещена) подменяли её пустым контекстом.
    """
    return transaction.atomic()


def _full_stats(rule) -> dict:
    """engine.rule_stats + sent_30d + last_run_at. Никогда не бросает."""
    data = dict(EMPTY_STATS)
    try:
        data.update(rule_stats(rule) or {})
    except Exception:
        pass
    try:
        since = timezone.now() - timedelta(days=30)
        data['sent_30d'] = int(BroadcastRecipient.objects.filter(
            send__auto_broadcast_rule=rule,
            status=RecipientStatus.SENT,
            sent_at__gte=since,
        ).count())
    except Exception:
        data['sent_30d'] = 0
    try:
        last = BroadcastSend.objects.filter(auto_broadcast_rule=rule).order_by('-created_at').first()
        data['last_run_at'] = getattr(last, 'created_at', None) if last is not None else None
    except Exception:
        data['last_run_at'] = None
    return data


def _card(rule, events: dict | None = None) -> dict:
    return rule_to_dict(rule, stats=_full_stats(rule), events=events if events is not None else _events())


# ── Разбор тела запроса ───────────────────────────────────────────────────────

class PayloadError(ValueError):
    """Некорректное тело — превращается в 400 с кодом (по умолчанию invalid_payload)."""

    def __init__(self, detail: str, code: str = 'invalid_payload'):
        super().__init__(detail)
        self.code = code
        self.detail = detail


_UNSET = object()


def _parse_bool(raw, field: str) -> bool:
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in ('true', '1', 'yes'):
        return True
    if value in ('false', '0', 'no'):
        return False
    raise PayloadError(f'{field}: ожидается true или false')


def _parse_int(raw, field: str, *, minimum=None, maximum=None, allow_none=True,
               code: str = 'invalid_payload'):
    if raw in (None, ''):
        if allow_none:
            return None
        raise PayloadError(f'{field}: обязательное число', code)
    if isinstance(raw, bool):
        raise PayloadError(f'{field}: ожидается число', code)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise PayloadError(f'{field}: ожидается число', code)
    if minimum is not None and value < minimum:
        raise PayloadError(f'{field}: минимум {minimum}', code)
    if maximum is not None and value > maximum:
        raise PayloadError(f'{field}: максимум {maximum}', code)
    return value


def _parse_date(raw, field: str):
    if raw in (None, ''):
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        raise PayloadError(f'{field}: ожидается дата в формате ГГГГ-ММ-ДД')


def _parse_text(raw, field: str, max_len: int, *, required: bool, code: str = 'invalid_payload') -> str:
    text = str(raw or '').strip()
    if not text and required:
        raise PayloadError(f'{field}: не может быть пустым', code)
    if len(text) > max_len:
        raise PayloadError(f'{field}: превышен лимит {max_len} символов', code)
    return text


def _parse_id_list(raw, field: str) -> list[int]:
    if raw in (None, ''):
        return []
    if isinstance(raw, str):
        items = [x for x in raw.split(',') if x.strip()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise PayloadError(f'{field}: ожидается список id')
    try:
        return [int(x) for x in items]
    except (TypeError, ValueError):
        raise PayloadError(f'{field}: ожидается список целых id')


def _parse_variants(raw) -> list[dict]:
    """Варианты A/B целиком (замена). Ошибки — variant_weights_invalid."""
    if raw in (None, '', [], ()):
        return []
    if not isinstance(raw, (list, tuple)):
        raise PayloadError('variants: ожидается список', 'variant_weights_invalid')
    out: list[dict] = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise PayloadError(f'variants[{i}]: ожидается объект', 'variant_weights_invalid')
        out.append({
            'name': _parse_text(item.get('name'), f'variants[{i}].name', MAX_VARIANT_NAME_LEN,
                                required=True, code='variant_weights_invalid'),
            'message_text': _parse_text(item.get('message_text'), f'variants[{i}].message_text',
                                        MAX_TEXT_LEN, required=True, code='variant_weights_invalid'),
            'weight': _parse_int(item.get('weight', 1), f'variants[{i}].weight', minimum=1,
                                 allow_none=False, code='variant_weights_invalid'),
            'is_active': _parse_bool(item.get('is_active', True), f'variants[{i}].is_active'),
        })
    return out


def _parse_reward(raw) -> dict:
    if raw in (None, ''):
        return {'gift_tier': '', 'gift_lifetime_days': 0, 'gift_fallback_text': ''}
    if not isinstance(raw, dict):
        raise PayloadError('reward: ожидается объект', 'reward_invalid')
    tier = str(raw.get('gift_tier') or '').strip()
    if tier not in GIFT_TIER_CODES:
        raise PayloadError(
            'reward.gift_tier: допустимы «» (без подарка), G1, G1,G2', 'reward_invalid')
    days = _parse_int(raw.get('gift_lifetime_days', 0), 'reward.gift_lifetime_days',
                      minimum=0, allow_none=True, code='reward_invalid') or 0
    fallback = _parse_text(raw.get('gift_fallback_text'), 'reward.gift_fallback_text',
                           MAX_TEXT_LEN, required=False, code='reward_invalid')
    return {'gift_tier': tier, 'gift_lifetime_days': days, 'gift_fallback_text': fallback}


def _parse_follow_up(raw):
    """None — очистить связку; иначе {'parent_rule_id', 'condition'}."""
    if raw in (None, '', {}):
        return None
    if not isinstance(raw, dict):
        raise PayloadError('follow_up: ожидается объект {parent_rule_id, condition}')
    parent_id = _parse_int(raw.get('parent_rule_id'), 'follow_up.parent_rule_id',
                           minimum=1, allow_none=False)
    condition = str(raw.get('condition') or FollowUpCondition.NOT_READ).strip()
    if condition not in {c for c, _ in FollowUpCondition.choices}:
        raise PayloadError('follow_up.condition: допустимы not_read и not_visited')
    return {'parent_rule_id': parent_id, 'condition': condition}


def _parse_audience(data) -> dict | None:
    """
    audience целиком (замена). Плоский gender_filter принимается для
    совместимости, но только если объекта audience в теле нет.
    """
    if 'audience' in data:
        raw = data.get('audience') or {}
        if not isinstance(raw, dict):
            raise PayloadError('audience: ожидается объект')
        out: dict = {}
        if 'branch_ids' in raw:
            out['branch_ids'] = _parse_id_list(raw.get('branch_ids'), 'audience.branch_ids')
        if 'rf_segment_ids' in raw:
            out['rf_segment_ids'] = _parse_id_list(raw.get('rf_segment_ids'), 'audience.rf_segment_ids')
        if 'gender_filter' in raw:
            out['gender_filter'] = _parse_gender(raw.get('gender_filter'))
        return out
    out = {}
    if 'branch_ids' in data:
        out['branch_ids'] = _parse_id_list(data.get('branch_ids'), 'branch_ids')
    if 'rf_segment_ids' in data:
        out['rf_segment_ids'] = _parse_id_list(data.get('rf_segment_ids'), 'rf_segment_ids')
    if 'gender_filter' in data:
        out['gender_filter'] = _parse_gender(data.get('gender_filter'))
    return out or None


def _parse_gender(raw) -> str:
    value = str(raw or GenderFilter.ALL).strip()
    if value not in {c for c, _ in GenderFilter.choices}:
        raise PayloadError('gender_filter: допустимы all, m, f')
    return value


def _parse_rule_payload(data, *, create: bool) -> dict:
    """
    Разбирает только ПЕРЕДАННЫЕ ключи (годится и для POST, и для PATCH).

    Возвращает {'fields': {...поля модели...}, 'audience': {...}|None,
    'reward': {...}|None, 'follow_up': {...}|None|'clear', 'variants': [...]|None}.
    """
    if not isinstance(data, dict):
        raise PayloadError('ожидается объект JSON')

    fields: dict = {}
    if create or 'name' in data:
        fields['name'] = _parse_text(data.get('name'), 'name', MAX_NAME_LEN, required=create)
    if create or 'event' in data:
        event = str(data.get('event') or '').strip()
        if not event:
            raise PayloadError('event: обязательное поле')
        if event not in _events():
            raise PayloadError(f'event: неизвестное событие «{event}»', 'event_unknown')
        fields['event'] = event
    if create or 'message_text' in data:
        fields['message_text'] = _parse_text(data.get('message_text'), 'message_text',
                                             MAX_TEXT_LEN, required=create)
    if 'delay_days' in data:
        fields['delay_days'] = _parse_int(data.get('delay_days'), 'delay_days', minimum=0)
    if 'send_hour_start' in data:
        fields['send_hour_start'] = _parse_int(data.get('send_hour_start'), 'send_hour_start',
                                               minimum=0, maximum=23, allow_none=False)
    if 'send_hour_end' in data:
        fields['send_hour_end'] = _parse_int(data.get('send_hour_end'), 'send_hour_end',
                                             minimum=0, maximum=23, allow_none=False)
    if 'active_from' in data:
        fields['active_from'] = _parse_date(data.get('active_from'), 'active_from')
    if 'active_to' in data:
        fields['active_to'] = _parse_date(data.get('active_to'), 'active_to')
    if 'priority' in data:
        fields['priority'] = _parse_int(data.get('priority'), 'priority', minimum=0, allow_none=False)
    if 'is_active' in data:
        fields['is_active'] = _parse_bool(data.get('is_active'), 'is_active')

    follow_up = _UNSET
    if 'follow_up' in data:
        follow_up = _parse_follow_up(data.get('follow_up'))

    return {
        'fields':    fields,
        'audience':  _parse_audience(data),
        'reward':    _parse_reward(data.get('reward')) if 'reward' in data else None,
        'follow_up': follow_up,
        'variants':  _parse_variants(data.get('variants')) if 'variants' in data else None,
    }


def _check_hours_and_period(rule, fields: dict):
    """Часы и период сверяются с ИТОГОВЫМИ значениями (учитывая то, что уже в правиле)."""
    def value(name, default):
        if name in fields:
            return fields[name]
        return getattr(rule, name, default) if rule is not None else default

    start = value('send_hour_start', 9)
    end = value('send_hour_end', 21)
    if start is not None and end is not None and int(start) >= int(end):
        raise PayloadError('send_hour_start должен быть меньше send_hour_end')
    active_from = value('active_from', None)
    active_to = value('active_to', None)
    if active_from and active_to and active_from > active_to:
        raise PayloadError('active_from не может быть позже active_to')


def _check_delay(rule, fields: dict, events: dict):
    """
    delay_days обязателен только у событий DELAY_REQUIRED_EVENTS (движок берёт
    N из правила и без него ничего не выберет) — 400 delay_required. У дней
    рождения / игры задержка не читается, у остальных есть значение по умолчанию.

    ⚠️ На PATCH проверка вызывается ТОЛЬКО когда в теле есть event или
    delay_days: иначе старая мобилка, которая шлёт один message_text, получала
    бы 400 на давно живущих правилах.
    """
    event = fields.get('event') or (getattr(rule, 'event', None) if rule is not None else None)
    if event not in DELAY_REQUIRED_EVENTS:
        return
    delay = fields['delay_days'] if 'delay_days' in fields else (
        getattr(rule, 'delay_days', None) if rule is not None else None)
    if delay is None:
        raise PayloadError(
            f'delay_days: для события «{event}» задержка обязательна', 'delay_required')


# ── Список и создание ─────────────────────────────────────────────────────────

class AutoBroadcastEventsAPIView(APIView):
    """GET /api/v1/auto-broadcasts/events/ — справочник событий и словарей."""
    permission_classes = [IsAuthenticated]

    @api_schema.events
    def get(self, request):
        events = _events()
        return Response({
            'events': [event_to_dict(code, spec) for code, spec in events.items()],
            'gender_filters':       [{'code': c, 'label': l} for c, l in GenderFilter.choices],
            'follow_up_conditions': [{'code': c, 'label': l} for c, l in FollowUpCondition.choices],
            'gift_tiers':           gift_tiers(),
        })


class AutoBroadcastRuleListCreateAPIView(APIView):
    """
    GET  /api/v1/auto-broadcasts/  — список правил.
    POST /api/v1/auto-broadcasts/  — создать правило (ВСЕГДА выключенным).

    Ключ списка — `rules` (так его читает мобилка); total/limit/offset
    добавлены аддитивно. Без параметра limit отдаётся полный список.
    Архивные скрыты, пока не передан include_archived=1.
    """
    permission_classes = [IsAuthenticated]

    @api_schema.rule_list
    def get(self, request):
        allowed = _allowed_branches(request)
        limit, offset, limit_given = _page_params(request)
        qs = _rules_qs()

        event = request.query_params.get('event')
        if event:
            qs = qs.filter(event=event)
        is_active = request.query_params.get('is_active')
        if is_active not in (None, ''):
            try:
                qs = qs.filter(is_active=_parse_bool(is_active, 'is_active'))
            except PayloadError as exc:
                return _error(exc.code, exc.detail, http_status.HTTP_400_BAD_REQUEST)
        search = (request.query_params.get('q') or '').strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(message_text__icontains=search))
        if str(request.query_params.get('include_archived') or '').strip().lower() not in ('1', 'true', 'yes'):
            qs = qs.filter(is_archived=False)

        if allowed is None:
            total = qs.count()
            rows = list(qs[offset:offset + limit]) if limit_given else list(qs)
        else:
            # Точки правила — M2M, «подмножество» в SQL не выразить: правил
            # десятки, фильтруем в Python (как у черновиков рассылок).
            visible = [r for r in qs if _rule_visible(r, allowed)]
            total = len(visible)
            rows = visible[offset:offset + limit] if limit_given else visible
        if not limit_given:
            limit, offset = total, 0

        events = _events()
        return Response({
            'rules':  [_card(r, events) for r in rows],
            'total':  total,
            'limit':  limit,
            'offset': offset,
        })

    @api_schema.rule_create
    def post(self, request):
        allowed = _allowed_branches(request)
        events = _events()
        try:
            parsed = _parse_rule_payload(request.data or {}, create=True)
            fields = parsed['fields']
            _check_hours_and_period(None, fields)
            _check_delay(None, fields, events)
        except PayloadError as exc:
            return _error(exc.code, exc.detail, http_status.HTTP_400_BAD_REQUEST)

        audience = parsed['audience'] or {}
        branch_ids = audience.get('branch_ids', [])
        error = _audience_access_error(branch_ids, allowed, required=True)
        if error is not None:
            return error

        segment_ids = audience.get('rf_segment_ids', [])
        if segment_ids and not _segments_exist(segment_ids):
            return _error('segment_not_found', 'RF-сегмент не найден',
                          http_status.HTTP_404_NOT_FOUND)

        follow_up = parsed['follow_up'] if parsed['follow_up'] is not _UNSET else None
        if fields.get('event') == 'follow_up' and not follow_up:
            return _error('invalid_payload',
                          'follow_up.parent_rule_id обязателен для события «Догоняющее»',
                          http_status.HTTP_400_BAD_REQUEST)
        parent = None
        if follow_up:
            parent = _load_rule(follow_up['parent_rule_id'], allowed)
            if parent is None:
                return _not_found()

        reward = parsed['reward'] or {}
        with _atomic():
            rule = AutoBroadcastRule.objects.create(
                name=fields['name'],
                event=fields['event'],
                message_text=fields['message_text'],
                delay_days=fields.get('delay_days'),
                send_hour_start=fields.get('send_hour_start', 9),
                send_hour_end=fields.get('send_hour_end', 21),
                active_from=fields.get('active_from'),
                active_to=fields.get('active_to'),
                priority=fields.get('priority', 0),
                gender_filter=audience.get('gender_filter', GenderFilter.ALL),
                gift_tier=reward.get('gift_tier', ''),
                gift_lifetime_days=reward.get('gift_lifetime_days', 0),
                gift_fallback_text=reward.get('gift_fallback_text', ''),
                parent_rule=parent,
                follow_up_condition=(follow_up or {}).get('condition', FollowUpCondition.NOT_READ),
                # Контракт 3б.2: правило создаётся ВЫКЛЮЧЕННЫМ. Включение —
                # только через activate/ с expected_count.
                is_active=False,
                is_archived=False,
            )
            if branch_ids:
                rule.branches.set(branch_ids)
            if segment_ids:
                rule.rf_segments.set(segment_ids)
            for variant in (parsed['variants'] or []):
                AutoBroadcastVariant.objects.create(rule=rule, **variant)
        return Response(_card(rule, events), status=http_status.HTTP_201_CREATED)


def _segments_exist(segment_ids) -> bool:
    try:
        found = RFSegment.objects.filter(pk__in=list(segment_ids)).values_list('pk', flat=True)
        return set(int(x) for x in found) == set(int(x) for x in segment_ids)
    except Exception:
        return False


def _audience_access_error(branch_ids, allowed, *, required: bool):
    """
    ★9: роль с ограничением по точкам обязана прислать непустой branch_ids ⊆
    своих точек. Недоступная или несуществующая точка — 404 (существование не
    раскрываем). Возвращает Response с ошибкой или None.
    """
    if allowed is not None:
        if required and not branch_ids:
            return _error(
                'invalid_payload',
                'branch_ids обязательны для роли с ограничением по точкам',
                http_status.HTTP_400_BAD_REQUEST,
            )
        if branch_ids and set(int(x) for x in branch_ids) - {int(x) for x in allowed}:
            return _error('not_found', 'Точка не найдена', http_status.HTTP_404_NOT_FOUND)
    if branch_ids:
        try:
            found = Branch.objects.filter(pk__in=list(branch_ids)).values_list('pk', flat=True)
            missing = set(int(x) for x in branch_ids) - set(int(x) for x in found)
        except Exception:
            missing = set()
        if missing:
            return _error('not_found', 'Точка не найдена', http_status.HTTP_404_NOT_FOUND)
    return None


# ── Карточка: чтение, правка, архив ───────────────────────────────────────────

class AutoBroadcastRuleDetailAPIView(APIView):
    """
    GET    /api/v1/auto-broadcasts/{id}/  — карточка.
    PATCH  /api/v1/auto-broadcasts/{id}/  — любые поля карточки.
    DELETE /api/v1/auto-broadcasts/{id}/  — АРХИВ (204), физически не удаляем.

    PATCH: audience / reward / follow_up / variants меняются ЦЕЛИКОМ (замена);
    плоские message_text, is_active, name, delay_days, send_hour_*, active_*,
    priority принимаются ради совместимости с мобилкой.
    """
    permission_classes = [IsAuthenticated]

    @api_schema.rule_get
    def get(self, request, pk):
        rule = _load_rule(pk, _allowed_branches(request))
        if rule is None:
            return _not_found()
        return Response(_card(rule))

    @api_schema.rule_patch
    def patch(self, request, pk):
        allowed = _allowed_branches(request)
        rule = _load_rule(pk, allowed)
        if rule is None:
            return _not_found()
        if getattr(rule, 'is_archived', False):
            return _error('archived', 'Правило в архиве — восстановите его перед правкой',
                          http_status.HTTP_409_CONFLICT)

        data = request.data or {}
        events = _events()
        try:
            parsed = _parse_rule_payload(data, create=False)
            fields = parsed['fields']
            _check_hours_and_period(rule, fields)
            if 'event' in fields or 'delay_days' in fields:
                _check_delay(rule, fields, events)
        except PayloadError as exc:
            return _error(exc.code, exc.detail, http_status.HTTP_400_BAD_REQUEST)

        # ★10: кабинет CheckUp включает правило только через activate/.
        if fields.get('is_active') is True and _is_checkup_user(request.user):
            return _error(
                'use_activate',
                'Включать правило можно только через POST …/activate/ с expected_count',
                http_status.HTTP_400_BAD_REQUEST,
            )

        audience = parsed['audience']
        if audience is not None and 'branch_ids' in audience:
            error = _audience_access_error(audience['branch_ids'], allowed, required=True)
            if error is not None:
                return error
        segment_ids = (audience or {}).get('rf_segment_ids')
        if segment_ids and not _segments_exist(segment_ids):
            return _error('segment_not_found', 'RF-сегмент не найден',
                          http_status.HTTP_404_NOT_FOUND)

        follow_up = parsed['follow_up']
        parent = None
        if follow_up is not _UNSET and follow_up:
            if int(follow_up['parent_rule_id']) == int(rule.pk):
                return _error('invalid_payload', 'follow_up.parent_rule_id: правило не может догонять само себя',
                              http_status.HTTP_400_BAD_REQUEST)
            parent = _load_rule(follow_up['parent_rule_id'], allowed)
            if parent is None:
                return _not_found()

        update_fields: list[str] = []
        for name, value in fields.items():
            setattr(rule, name, value)
            update_fields.append(name)
        if audience is not None and 'gender_filter' in audience:
            rule.gender_filter = audience['gender_filter']
            update_fields.append('gender_filter')
        if parsed['reward'] is not None:
            for name, value in parsed['reward'].items():
                setattr(rule, name, value)
                update_fields.append(name)
        if follow_up is not _UNSET:
            rule.parent_rule = parent
            update_fields.append('parent_rule')
            if follow_up:
                rule.follow_up_condition = follow_up['condition']
                update_fields.append('follow_up_condition')

        with _atomic():
            if update_fields:
                rule.save(update_fields=sorted(set(update_fields)) + ['updated_at'])
            if audience is not None and 'branch_ids' in audience:
                rule.branches.set(audience['branch_ids'])
            if segment_ids is not None and audience is not None and 'rf_segment_ids' in audience:
                rule.rf_segments.set(audience['rf_segment_ids'])
            if parsed['variants'] is not None:
                # Замена целиком: варианты с отправками не удаляем физически —
                # на них висит история (BroadcastSend.auto_broadcast_variant),
                # такие просто выключаются.
                _replace_variants(rule, parsed['variants'])
        # Правило пришло из queryset с prefetch_related: после .set() кэш
        # связей устарел бы и карточка показала старые точки/сегменты.
        try:
            rule.refresh_from_db()
        except Exception:
            pass
        return Response(_card(rule, events))

    @api_schema.rule_delete
    def delete(self, request, pk):
        rule = _load_rule(pk, _allowed_branches(request))
        if rule is None:
            return _not_found()
        rule.is_archived = True
        rule.is_active = False
        rule.save(update_fields=['is_archived', 'is_active', 'updated_at'])
        logger.info('auto-broadcast rule %s archived by %s', pk, getattr(request.user, 'username', ''))
        return Response(status=http_status.HTTP_204_NO_CONTENT)


def _replace_variants(rule, variants: list[dict]):
    """Варианты правила целиком: с отправками — выключаем, без отправок — удаляем."""
    for old in list(rule.variants.all()):
        if BroadcastSend.objects.filter(auto_broadcast_variant=old).exists():
            if old.is_active:
                old.is_active = False
                old.save(update_fields=['is_active'])
        else:
            old.delete()
    for variant in variants:
        AutoBroadcastVariant.objects.create(rule=rule, **variant)


# ── Предпросмотр ──────────────────────────────────────────────────────────────

class AutoBroadcastRulePreviewAPIView(APIView):
    """
    GET /api/v1/auto-broadcasts/{id}/preview/

    Старые поля мобилки (recipients, due_now, reason, sample_text,
    sample_names) собираются ТАК ЖЕ, как engine.preview_rule (тот же
    resolve_recipients + rule_is_due + render_text), но одним проходом:
    из того же списка кандидатов считаются count/by_branch/sample_texts.
    Резолв аудитории тяжёлый (сегменты, дедуп, кэп) — дважды его не гоняем.

    count — та цифра, которую кабинет обязан вернуть в expected_count при
    activate/. Расчёт упал → 409 preview_failed (включать тоже нельзя, ★11).
    """
    permission_classes = [IsAuthenticated]

    @api_schema.preview
    def get(self, request, pk):
        rule = _load_rule(pk, _allowed_branches(request))
        if rule is None:
            return _not_found()
        try:
            now = timezone.now()
            candidates = resolve_recipients(rule, now)
            due, reason = rule_is_due(rule, now)
            sample = candidates[0] if candidates else None
            data = {
                'recipients':   len(candidates),
                'due_now':      due,
                'reason':       reason,
                'sample_text':  (render_text(rule, sample) if sample is not None
                                 else (rule.message_text or '')),
                'sample_names': [_candidate_name(c) for c in candidates[:5]],
                'count':        len(candidates),
                'by_branch':    _by_branch(candidates),
                'sample_texts': _sample_texts(rule, candidates),
            }
        except Exception as exc:
            logger.warning('auto-broadcast preview failed rule=%s: %s', pk, exc)
            return _error('preview_failed', f'Не удалось посчитать: {exc}',
                          http_status.HTTP_409_CONFLICT)
        return Response(data)


def _candidate_name(candidate) -> str:
    """Имя гостя для sample_names — как в engine.preview_rule."""
    client = getattr(candidate.client_branch, 'client', None)
    return getattr(client, 'first_name', '') or f'vk{candidate.vk_id}'


def _by_branch(candidates) -> list[dict]:
    """Кандидаты по точкам (внутренний Branch.id), по возрастанию id."""
    rows: dict[int, dict] = {}
    for c in candidates:
        branch = getattr(c.client_branch, 'branch', None)
        if branch is None:
            continue
        row = rows.setdefault(int(branch.pk), {
            'branch_id': int(branch.pk), 'name': branch.name or '', 'count': 0,
        })
        row['count'] += 1
    return [rows[key] for key in sorted(rows)]


def _sample_texts(rule, candidates) -> list[dict]:
    """
    Пример текста по каждому активному варианту A/B (как их выбирает run_rule
    через pick_variant); без вариантов — один «Основной текст».
    Нет кандидатов — показываем шаблон как есть, без подстановок.
    """
    sample = candidates[0] if candidates else None
    variants = [v for v in rule.variants.all() if v.is_active and v.weight > 0]
    if not variants:
        text = render_text(rule, sample) if sample is not None else (rule.message_text or '')
        return [{'variant_id': None, 'name': 'Основной текст', 'text': text}]
    out = []
    for variant in sorted(variants, key=lambda v: v.pk):
        text = (render_text(rule, sample, variant) if sample is not None
                else (variant.message_text or ''))
        out.append({'variant_id': variant.pk, 'name': variant.name or '', 'text': text})
    return out


# ── Включение / выключение ────────────────────────────────────────────────────

class AutoBroadcastRuleActivateAPIView(APIView):
    """
    POST /api/v1/auto-broadcasts/{id}/activate/  {expected_count, confirm: true}

    Единственный путь включения из кабинета. Порядок проверок:
      1) RBAC → 404;
      2) состояние правила (архив / уже включено) → 409;
      3) expected_count и confirm обязательны → 400;
      4) аудитория пересчитывается СВЕЖО (тем же кодом, что и предпросмотр);
      5) пусто → 400 audience_empty, разошлось → 409 audience_changed.
    """
    permission_classes = [IsAuthenticated]

    @api_schema.activate
    def post(self, request, pk):
        rule = _load_rule(pk, _allowed_branches(request))
        if rule is None:
            return _not_found()
        if getattr(rule, 'is_archived', False):
            return _error('archived', 'Правило в архиве — включить нельзя',
                          http_status.HTTP_409_CONFLICT)
        if rule.is_active:
            return _error('already_active', 'Правило уже включено',
                          http_status.HTTP_409_CONFLICT)

        data = request.data or {}
        if data.get('expected_count') in (None, ''):
            return _error('expected_count_required',
                          'Нужно передать expected_count — число получателей из предпросмотра',
                          http_status.HTTP_400_BAD_REQUEST)
        try:
            expected = int(data.get('expected_count'))
        except (TypeError, ValueError):
            return _error('invalid_payload', 'expected_count: ожидается число',
                          http_status.HTTP_400_BAD_REQUEST)
        if not _confirmed(data):
            return _error('confirm_required', 'Нужно подтверждение: confirm=true',
                          http_status.HTTP_400_BAD_REQUEST)

        try:
            actual = len(resolve_recipients(rule))
        except Exception as exc:
            logger.warning('auto-broadcast activate preview failed rule=%s: %s', pk, exc)
            return _error('preview_failed', f'Не удалось посчитать: {exc}',
                          http_status.HTTP_409_CONFLICT)

        if actual == 0:
            return _error('audience_empty', 'Сейчас правило никому не отправит',
                          http_status.HTTP_400_BAD_REQUEST)
        if audience_changed(expected, actual):
            return _error('audience_changed', 'Аудитория изменилась — обновите предпросмотр',
                          http_status.HTTP_409_CONFLICT, expected=expected, actual=actual)

        rule.is_active = True
        rule.save(update_fields=['is_active', 'updated_at'])
        logger.info('auto-broadcast rule %s activated by %s (expected=%s actual=%s)',
                    pk, getattr(request.user, 'username', ''), expected, actual)
        return Response(_card(rule))


class AutoBroadcastRuleDeactivateAPIView(APIView):
    """POST /api/v1/auto-broadcasts/{id}/deactivate/ — выключить (всегда можно)."""
    permission_classes = [IsAuthenticated]

    @api_schema.deactivate
    def post(self, request, pk):
        rule = _load_rule(pk, _allowed_branches(request))
        if rule is None:
            return _not_found()
        if rule.is_active:
            rule.is_active = False
            rule.save(update_fields=['is_active', 'updated_at'])
            logger.info('auto-broadcast rule %s deactivated by %s',
                        pk, getattr(request.user, 'username', ''))
        return Response(_card(rule))


# ── Лог и статистика ──────────────────────────────────────────────────────────

class AutoBroadcastRuleLogAPIView(APIView):
    """
    GET /api/v1/auto-broadcasts/{id}/log/?limit&offset&status

    Получатели всех запусков правила (BroadcastRecipient всех BroadcastSend с
    auto_broadcast_rule=rule). Отсева дедупом/кэпом/окном здесь нет: он
    происходит ДО создания получателей.
    """
    permission_classes = [IsAuthenticated]

    @api_schema.log
    def get(self, request, pk):
        rule = _load_rule(pk, _allowed_branches(request))
        if rule is None:
            return _not_found()
        limit, offset, _ = _page_params(request)
        qs = (
            BroadcastRecipient.objects
            .filter(send__auto_broadcast_rule=rule)
            .select_related('send', 'send__auto_broadcast_variant', 'client_branch', 'client_branch__client')
            .order_by('-sent_at', '-id')
        )
        status_filter = (request.query_params.get('status') or '').strip()
        if status_filter:
            qs = qs.filter(status=status_filter)
        total = qs.count()
        rows = list(qs[offset:offset + limit])
        return Response({
            'total':   total,
            'limit':   limit,
            'offset':  offset,
            'results': [log_row_to_dict(r) for r in rows],
        })


class AutoBroadcastRuleStatsAPIView(APIView):
    """GET /api/v1/auto-broadcasts/{id}/stats/ — сводка правила и вариантов."""
    permission_classes = [IsAuthenticated]

    @api_schema.stats
    def get(self, request, pk):
        rule = _load_rule(pk, _allowed_branches(request))
        if rule is None:
            return _not_found()
        card = _card(rule)
        stats = dict(card['stats'])
        stats['variants'] = card['variants']
        return Response(stats)


# ── Варианты A/B ──────────────────────────────────────────────────────────────

class AutoBroadcastVariantCreateAPIView(APIView):
    """POST /api/v1/auto-broadcasts/{id}/variants/ — добавить вариант текста."""
    permission_classes = [IsAuthenticated]

    @api_schema.variant_create
    def post(self, request, pk):
        rule = _load_rule(pk, _allowed_branches(request))
        if rule is None:
            return _not_found()
        if getattr(rule, 'is_archived', False):
            return _error('archived', 'Правило в архиве', http_status.HTTP_409_CONFLICT)
        try:
            values = _parse_variants([request.data or {}])[0]
        except PayloadError as exc:
            return _error(exc.code, exc.detail, http_status.HTTP_400_BAD_REQUEST)
        variant = AutoBroadcastVariant.objects.create(rule=rule, **values)
        return Response(variant_to_dict(variant), status=http_status.HTTP_201_CREATED)


class AutoBroadcastVariantDetailAPIView(APIView):
    """
    PATCH/DELETE /api/v1/auto-broadcasts/{id}/variants/{vid}/

    DELETE варианта, у которого есть отправки, — 409 has_sends: на нём висит
    статистика и прочтения (BroadcastSend.auto_broadcast_variant). Такой
    вариант выключают (is_active=false), а не удаляют.
    """
    permission_classes = [IsAuthenticated]

    def _load(self, request, pk, vid):
        rule = _load_rule(pk, _allowed_branches(request))
        if rule is None:
            return None, None
        variant = AutoBroadcastVariant.objects.filter(pk=vid, rule=rule).first()
        return rule, variant

    @api_schema.variant_patch
    def patch(self, request, pk, vid):
        rule, variant = self._load(request, pk, vid)
        if rule is None or variant is None:
            return _not_found()
        data = request.data or {}
        merged = {
            'name':         data.get('name', variant.name),
            'message_text': data.get('message_text', variant.message_text),
            'weight':       data.get('weight', variant.weight),
            'is_active':    data.get('is_active', variant.is_active),
        }
        try:
            values = _parse_variants([merged])[0]
        except PayloadError as exc:
            return _error(exc.code, exc.detail, http_status.HTTP_400_BAD_REQUEST)
        for name, value in values.items():
            setattr(variant, name, value)
        variant.save(update_fields=['name', 'message_text', 'weight', 'is_active', 'updated_at'])
        return Response(variant_to_dict(variant))

    @api_schema.variant_delete
    def delete(self, request, pk, vid):
        rule, variant = self._load(request, pk, vid)
        if rule is None or variant is None:
            return _not_found()
        if BroadcastSend.objects.filter(auto_broadcast_variant=variant).exists():
            return _error('has_sends',
                          'У варианта есть отправки — его нельзя удалить, выключите его (is_active=false)',
                          http_status.HTTP_409_CONFLICT)
        variant.delete()
        return Response(status=http_status.HTTP_204_NO_CONTENT)


# ── Тест-отправка ─────────────────────────────────────────────────────────────

class AutoBroadcastRuleTestSendAPIView(APIView):
    """
    POST /api/v1/auto-broadcasts/{id}/test-send/  {vk_id}

    Отправляет ОДНО сообщение тем же путём, что и движок (send_vk_message от
    имени сообщества точки гостя), но НИЧЕГО не пишет: ни AutoBroadcastLog
    (общий дедуп — испортим боевую отправку этому гостю), ни BroadcastSend /
    BroadcastRecipient (статистика правила осталась бы кривой). Факт — только
    в серверный лог (★12).

    Троттл ★37: не чаще раза в минуту на правило (django cache).
    """
    permission_classes = [IsAuthenticated]

    @api_schema.test_send
    def post(self, request, pk):
        allowed = _allowed_branches(request)
        rule = _load_rule(pk, allowed)
        if rule is None:
            return _not_found()

        data = request.data or {}
        try:
            vk_id = _parse_int(data.get('vk_id'), 'vk_id', minimum=1, allow_none=False)
        except PayloadError as exc:
            return _error(exc.code, exc.detail, http_status.HTTP_400_BAD_REQUEST)

        cache_key = f'ab-test-send:{current_schema_name()}:{rule.pk}'
        if cache.get(cache_key):
            return _error('rate_limited', 'Тестовую отправку можно делать не чаще раза в минуту',
                          http_status.HTTP_409_CONFLICT)

        client_branch = _find_guest(rule, vk_id, allowed)
        if client_branch is None:
            return _error('guest_not_found', 'Гость с таким VK ID не найден в доступных точках',
                          http_status.HTTP_400_BAD_REQUEST)
        config = getattr(client_branch.branch, 'senler_config', None)
        if config is None or not config.is_active:
            return _error('no_vk_token', 'У точки гостя нет подключённого сообщества ВК',
                          http_status.HTTP_409_CONFLICT)

        candidate = Candidate(client_branch=client_branch, vk_id=vk_id)
        variant = pick_variant(rule, vk_id)
        text = render_text(rule, candidate, variant)

        cache.set(cache_key, 1, TEST_SEND_TTL)
        ok, err, message_id = send_vk_message(config, vk_id, text, None)
        if not ok:
            if '901' in str(err or ''):
                return _error('not_subscribed', 'Гость не разрешил сообщения от сообщества',
                              http_status.HTTP_400_BAD_REQUEST)
            return _error('vk_error', str(err or 'ВК не принял сообщение'),
                          http_status.HTTP_502_BAD_GATEWAY)
        logger.info('auto-broadcast test-send rule=%s vk_id=%s by=%s message_id=%s',
                    rule.pk, vk_id, getattr(request.user, 'username', ''), message_id)
        return Response({'ok': True, 'message_id': message_id})


def _find_guest(rule, vk_id: int, allowed):
    """
    Профиль гостя для тест-отправки: сначала в точках правила, потом в любой
    доступной пользователю точке (гость сети может быть привязан к нескольким).
    """
    qs = ClientBranch.objects.filter(client__vk_id=vk_id).select_related('client', 'branch')
    if allowed is not None:
        qs = qs.filter(branch_id__in=[int(x) for x in allowed])
    rule_branches = _rule_branch_ids(rule)
    if rule_branches:
        found = qs.filter(branch_id__in=rule_branches).first()
        if found is not None:
            return found
    return qs.first()
