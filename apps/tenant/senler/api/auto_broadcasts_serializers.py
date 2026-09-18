"""
Карточка правила авторассылки для внешнего кабинета CheckUp (контракт №31).

Зачем отдельный модуль: форма карточки — НАДМНОЖЕСТВО того, что отдавала
мобилке apps/tenant/mobile/api/views.py:_serialize_rule. Старые плоские ключи
(`branches_count`, `segments_count`, `sent_total`, `sent`, `read`, `failed`,
`open_rate`, `gender_filter`, `variants`, `parent_rule_name`) обязаны остаться
С ТЕМИ ЖЕ значениями и типами — иначе сломается уже установленная мобилка,
чьи запросы после подключения этого модуля приходят сюда (senler/api/urls.py
включён в main/urls.py раньше mobile/api/urls.py).

Сериализация обычными функциями (как в senler/api/serializers.py): модели
тенантные, тесты работают на моках и в ORM не ходят.

Инварианты:
  • все даты/датавремя — ISO-строка или null;
  • `image` всегда null (картинки правил в v1.5 не поддержаны, как у рассылок);
  • статистика никогда не роняет карточку: расчёт обёрнут в try/except и в
    худшем случае даёт нули (так же вела себя мобилка).
"""
from __future__ import annotations

from .serializers import iso, segment_to_dict

# Лимит VK на длину сообщения.
MAX_TEXT_LEN = 4096
# Длина названия правила (AutoBroadcastRule.name) и названия варианта.
MAX_NAME_LEN = 120
MAX_VARIANT_NAME_LEN = 60

# Подарочный шаг: реально используемые сочетания тиров (models.AutoBroadcastRule
# .gift_tier — свободная строка, но кабинет выбирает из трёх вариантов).
GIFT_TIERS = (
    ('',      'Без подарка'),
    ('G1',    'G1 — обычный подарок'),
    ('G1,G2', 'G1 или G2 — обычный либо средний'),
)
GIFT_TIER_CODES = tuple(code for code, _ in GIFT_TIERS)

# Полный список подстановок, которые умеет engine.render_text. Используется,
# когда у события в EventSpec.placeholders пусто (истина по подстановкам —
# сам render_text, а не каталог событий).
COMMON_PLACEHOLDERS = (
    '{имя}', '{баланс}', '{награда}', '{подарок}', '{дней_осталось}', '{адреса}',
)

# Человеческие пояснения к событиям для кабинета: в EventSpec есть только label.
EVENT_DESCRIPTIONS = {
    'birthday_7d':      'Уходит за 7 дней до дня рождения гостя. Раз в год на гостя.',
    'birthday_1d':      'Уходит за 1 день до дня рождения гостя. Раз в год на гостя.',
    'birthday':         'Уходит в день рождения гостя. Раз в год на гостя.',
    'after_game_3h':    'Через 3 часа после игры (вечерние игры добираются утром). Раз в сутки на гостя.',
    'gift_not_claimed': 'Подарок из сториз/сайта не забран N дней. Одно напоминание на подарок.',
    'no_visit_days':    'Гость не приходил N дней — реактивация. Задержка обязательна.',
    'subscribed_days':  'Гость подписался N дней назад — welcome. Задержка обязательна.',
    'follow_up':        'Догоняющее: кому отправило другое правило и кто не отреагировал. Нужен parent_rule_id.',
    'rf_gift_expiring': 'RF-подарок сгорает через N дней. Одно напоминание на подарок.',
    'phone_request':    'Просьба поделиться номером через N дней после визита. Одно напоминание на гостя.',
}

EMPTY_STATS = {
    'sent': 0, 'read': 0, 'failed': 0, 'open_rate': 0.0,
    'variants': [], 'sent_30d': 0, 'last_run_at': None,
}

GENDER_LABELS = {'all': 'все', 'm': 'мужчины', 'f': 'женщины'}


# ── Мелкие помощники ─────────────────────────────────────────────────────────

def safe_int(func, default: int = 0) -> int:
    """
    Счётчик из ORM, который не имеет права уронить карточку.

    На моках (тесты) и при недоступной таблице возвращает default — ровно так
    же вела себя мобилка, оборачивая rule_stats в try/except.
    """
    try:
        return int(func())
    except Exception:
        return default


def events_catalog() -> dict:
    """Каталог событий движка; пустой словарь, если движок недоступен."""
    try:
        from apps.tenant.senler.engine import get_events
        return get_events()
    except Exception:
        return {}


def event_spec(code, events: dict | None = None):
    catalog = events if events is not None else events_catalog()
    try:
        return catalog.get(code)
    except Exception:
        return None


def safe_rule_stats(rule) -> dict:
    """rule_stats движка с нулями вместо исключения (как в мобилке)."""
    try:
        from apps.tenant.senler.engine import rule_stats
        data = dict(EMPTY_STATS)
        data.update(rule_stats(rule) or {})
        return data
    except Exception:
        return dict(EMPTY_STATS)


# ── Справочник событий ───────────────────────────────────────────────────────

# События, у которых резолвер движка читает rule.delay_days БЕЗ значения по
# умолчанию (engine.py: _no_visit_resolver и _subscribed_resolver — «ровно N
# дней назад»). У дней рождения и «через 3 ч после игры» задержка не читается
# вовсе, у остальных есть default_delay_days — там поле необязательно.
DELAY_REQUIRED_EVENTS = frozenset({'no_visit_days', 'subscribed_days'})


def event_to_dict(code, spec) -> dict:
    """
    Строка справочника GET auto-broadcasts/events/.

    delay_required = событие из DELAY_REQUIRED_EVENTS: движок берёт N только из
    delay_days (no_visit_days / subscribed_days); остальным поле не нужно.
    placeholders — из EventSpec; если там пусто, отдаём общий список
    render_text, чтобы кабинет не показывал пустую подсказку.
    """
    placeholders = [str(p) for p in (getattr(spec, 'placeholders', None) or ())]
    code = str(code)
    default_delay = getattr(spec, 'default_delay_days', None)
    return {
        'code':               code,
        'label':              getattr(spec, 'label', '') or code,
        'description':        EVENT_DESCRIPTIONS.get(code, getattr(spec, 'label', '') or code),
        'dedup':              getattr(spec, 'dedup', ''),
        'delay_unit':         'days',
        'default_delay_days': default_delay,
        'delay_required':     code in DELAY_REQUIRED_EVENTS,
        'placeholders':       placeholders or list(COMMON_PLACEHOLDERS),
    }


def gift_tiers() -> list[dict]:
    return [{'code': code, 'label': label} for code, label in GIFT_TIERS]


# ── Сводки для карточки ──────────────────────────────────────────────────────

def audience_summary(branch_ids, gender_filter, segments) -> str:
    """Человеческая строка «кому» — кабинет показывает её в списке правил."""
    parts = ['все точки' if not branch_ids else f'точки: {len(branch_ids)}']
    gender = GENDER_LABELS.get(gender_filter or 'all', gender_filter or 'все')
    if (gender_filter or 'all') != 'all':
        parts.append(gender)
    if segments:
        names = ', '.join((s.get('name') or s.get('code') or '') for s in segments)
        parts.append(f'сегменты: {names}')
    return ' · '.join(p for p in parts if p)


def reward_summary(gift_tier: str, gift_lifetime_days, gift_fallback_text: str) -> str:
    """Человеческая строка подарочного шага."""
    tier = (gift_tier or '').strip()
    if not tier:
        return 'без подарка'
    days = int(gift_lifetime_days or 0)
    parts = [f'подарок {tier}']
    parts.append(f'сгорает через {days} дн.' if days > 0 else 'срок — как в каталоге')
    if (gift_fallback_text or '').strip():
        parts.append('есть запасной текст')
    return ', '.join(parts)


# ── Варианты A/B ─────────────────────────────────────────────────────────────

def variant_to_dict(variant, stats: dict | None = None) -> dict:
    """
    Вариант текста + его статистика.

    stats приходит из rule_stats (там варианты уже посчитаны одним запросом);
    без него отдаём нули — карточка не должна плодить N запросов.
    """
    stats = stats or {}
    return {
        'id':           variant.pk,
        'name':         variant.name or '',
        'message_text': variant.message_text or '',
        'weight':       variant.weight,
        'is_active':    bool(variant.is_active),
        'sent':         stats.get('sent', 0),
        'read':         stats.get('read', 0),
        'failed':       stats.get('failed', 0),
        'open_rate':    stats.get('open_rate', 0.0),
    }


# ── Карточка правила ─────────────────────────────────────────────────────────

def rule_to_dict(rule, stats: dict | None = None, events: dict | None = None) -> dict:
    """
    Карточка правила: новая вложенная форма (audience/reward/follow_up/stats)
    ПЛЮС старые плоские ключи мобилки. Ни один старый ключ удалять нельзя —
    на них живёт установленное мобильное приложение.

    stats — результат engine.rule_stats + sent_30d/last_run_at (считает вьюха
    одним проходом); events — каталог get_events() (в списке правил он общий на
    все строки, чтобы не пересобирать его на каждую карточку).
    """
    spec = event_spec(rule.event, events)
    st = dict(EMPTY_STATS)
    st.update(stats if stats is not None else safe_rule_stats(rule))

    branch_ids = [b.pk for b in rule.branches.all()]
    segments = [segment_to_dict(s) for s in rule.rf_segments.all()]
    variant_stats = {v.get('id'): v for v in (st.get('variants') or []) if isinstance(v, dict)}
    variants = [variant_to_dict(v, variant_stats.get(v.pk)) for v in rule.variants.all()]
    gender = rule.gender_filter or 'all'

    follow_up = None
    if getattr(rule, 'parent_rule_id', None):
        parent = rule.parent_rule
        follow_up = {
            'parent_rule_id':   rule.parent_rule_id,
            'parent_rule_name': (getattr(parent, 'name', '') or ''),
            'condition':        rule.follow_up_condition or '',
        }

    try:
        event_label = rule.get_event_display()
    except Exception:
        event_label = getattr(spec, 'label', '') or str(rule.event)

    gift_tier = rule.gift_tier or ''
    gift_lifetime = rule.gift_lifetime_days or 0
    gift_fallback = rule.gift_fallback_text or ''

    return {
        # ── новая форма (контракт 3б.2) ───────────────────────────────────────
        'id':                 rule.pk,
        'name':               rule.name or '',
        'event':              str(rule.event),
        'event_label':        event_label,
        'is_active':          rule.is_active,
        'is_archived':        bool(getattr(rule, 'is_archived', False)),
        'priority':           rule.priority,
        'delay_days':         rule.delay_days,
        'default_delay_days': getattr(spec, 'default_delay_days', None),
        'send_hour_start':    rule.send_hour_start,
        'send_hour_end':      rule.send_hour_end,
        'active_from':        iso(rule.active_from),
        'active_to':          iso(rule.active_to),
        'audience': {
            'branch_ids':    branch_ids,     # ВНУТРЕННИЕ Branch.id; [] = все точки сети
            'gender_filter': gender,
            'rf_segments':   segments,
        },
        'audience_summary':   audience_summary(branch_ids, gender, segments),
        'message_text':       rule.message_text or '',
        'image':              None,          # картинки правил в v1.5 не поддержаны
        'reward': {
            'gift_tier':          gift_tier,
            'gift_lifetime_days': gift_lifetime,
            'gift_fallback_text': gift_fallback,
        },
        'reward_summary':     reward_summary(gift_tier, gift_lifetime, gift_fallback),
        'follow_up':          follow_up,
        'variants':           variants,
        'stats': {
            'sent':        st['sent'],
            'read':        st['read'],
            'failed':      st['failed'],
            'open_rate':   st['open_rate'],
            'sent_30d':    st['sent_30d'],
            'last_run_at': iso(st['last_run_at']),
        },
        'created_at':         iso(getattr(rule, 'created_at', None)),
        'updated_at':         iso(getattr(rule, 'updated_at', None)),

        # ── старые плоские ключи мобилки (_serialize_rule) — НЕ УДАЛЯТЬ ───────
        'branches_count':     len(branch_ids),      # 0 = все точки
        'gender_filter':      gender,
        'segments_count':     len(segments),
        'sent_total':         safe_int(rule.logs.count),
        'sent':               st['sent'],
        'read':               st['read'],
        'failed':             st['failed'],
        'open_rate':          st['open_rate'],
        'parent_rule_name':   follow_up['parent_rule_name'] if follow_up else None,
    }


# ── Лог получателей ──────────────────────────────────────────────────────────

def log_row_to_dict(recipient) -> dict:
    """
    Строка GET auto-broadcasts/{id}/log/.

    vk_id — СТРОКОЙ (решение ★ ревью CheckUp: числа за 2^53 в JS теряют
    точность). Отсев дедупом/кэпом/окном сюда не попадает: получатели
    создаются уже после него.
    """
    send = recipient.send if getattr(recipient, 'send_id', None) else None
    variant = getattr(send, 'auto_broadcast_variant', None) if send is not None else None
    client_branch = recipient.client_branch if getattr(recipient, 'client_branch_id', None) else None
    client = getattr(client_branch, 'client', None)
    return {
        'sent_at': iso(recipient.sent_at),
        'vk_id':   str(recipient.vk_id or ''),
        'name':    (getattr(client, 'first_name', '') or ''),
        'variant': {'id': variant.pk, 'name': variant.name or ''} if variant is not None else None,
        'status':  recipient.status,
        'read_at': iso(recipient.read_at),
        'error':   recipient.error or '',
    }
