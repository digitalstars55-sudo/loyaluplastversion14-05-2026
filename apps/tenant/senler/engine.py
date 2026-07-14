"""
Движок авторассылок («конструктор»).

Считает, КОМУ и ЧТО отправить по правилу (AutoBroadcastRule), и умеет работать
в режиме предпросмотра (dry-run) — без единого сообщения в VK.

═══════════════════════════════════════════════════════════════════════════════
ПОЧЕМУ ЭТО БЕЗОПАСНО РЯДОМ СО СТАРЫМИ ЗАДАЧАМИ
═══════════════════════════════════════════════════════════════════════════════
Старые задачи (send_birthday_broadcasts_task, send_after_game_broadcast_task,
send_gift_reminder_broadcasts_task) НЕ ТРОГАЮТСЯ и остаются путём отката.

Движок пишет и читает ТОТ ЖЕ AutoBroadcastLog с ТЕМИ ЖЕ trigger_type, что и они.
На проде накоплено ~13k записей лога — именно они не дают повторно написать тем,
кого уже поздравили. Поэтому:
  • при переезде на движок старые записи продолжают работать → дублей нет;
  • даже если по ошибке одновременно отработают и старая задача, и движок,
    второй отправки не будет — оба спрашивают один лог.
Это осознанно делает дедуп общим ресурсом, а не «у каждого свой».

Резолверы ниже ПОВТОРЯЮТ выборки старых задач один-в-один (включая защиту
«дата ДР установлена ≥30 дней назад»). Любое расхождение = гость получит или
не получит сообщение не тогда, когда раньше, — поэтому менять их нельзя без
сверки с legacy-задачей.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

import pytz
from django.utils import timezone

logger = logging.getLogger(__name__)

_MSK = pytz.timezone('Europe/Moscow')

# Как дедуплицируется событие:
#   'year'   — раз в календарный год на гостя (ДР)
#   'day'    — раз в сутки на гостя (после игры)
#   'entity' — раз на КОНКРЕТНЫЙ объект (подарок): гость может получить несколько
#              подарков, по каждому положено своё напоминание
DEDUP_YEAR   = 'year'
DEDUP_DAY    = 'day'
DEDUP_ENTITY = 'entity'


@dataclass
class Candidate:
    """Один получатель + контекст для подстановки переменных."""
    client_branch: object
    vk_id: int
    entity_key: str = ''          # 'gift:123' для дедупа по объекту
    gift = None                   # StoryGiftEntry, если событие про подарок


@dataclass
class EventSpec:
    label: str
    dedup: str
    resolver: Callable            # (rule, now) -> list[Candidate]
    placeholders: tuple           # какие переменные доступны в тексте
    default_delay_days: int | None = None


# ── Резолверы (ПОВТОРЯЮТ legacy-задачи, см. шапку) ───────────────────────────

def _birthday_resolver(offset_days: int):
    """
    ДР: гости, у кого день/месяц рождения = сегодня + offset_days.
    1-в-1 с send_birthday_broadcasts_task: is_employee=False, client активен,
    есть vk_id, и birth_date_set_at не новее 30 дней (защита от накрутки).
    """
    def _resolve(rule, now):
        from apps.tenant.branch.models import ClientBranch

        today = now.astimezone(_MSK).date()
        target = today + timedelta(days=offset_days)
        qs = (
            ClientBranch.objects
            .filter(
                is_employee=False,
                client__is_active=True,
                client__vk_id__isnull=False,
                birth_date__month=target.month,
                birth_date__day=target.day,
                birth_date_set_at__lte=today - timedelta(days=30),
            )
            .select_related('client', 'branch')
        )
        qs = _apply_audience(qs, rule)
        return [Candidate(client_branch=cb, vk_id=cb.client.vk_id) for cb in qs]
    return _resolve


def _after_game_resolver(rule, now):
    """
    Через 3 часа после игры. 1-в-1 с send_after_game_broadcast_task:
    окно (now-3h-20min .. now-3h), 20 мин нахлёста чтобы не терять игроков.

    ВЕЧЕРНИЙ РЕЖИМ (Фаза 2): у legacy для этого отдельный запуск в 09:00 с
    process_evening=True — он добирает вчерашние игры 18:01–23:59, чей «+3 часа»
    пришёлся на ночь. Движок делает то же: если сейчас 9-й час по МСК, к обычному
    окну добавляются вчерашние вечерние игры. Движок крутится каждые 15 мин, т.е.
    в 9:00/9:15/9:30/9:45 они попадут 4 раза — но дедуп «раз в сутки на гостя»
    (DEDUP_DAY, общий лог с legacy) не даст отправить повторно.
    """
    from apps.tenant.game.models import ClientAttempt

    local = now.astimezone(_MSK)
    windows = [(now - timedelta(hours=3, minutes=20), now - timedelta(hours=3))]

    if local.hour == 9:
        yesterday = local.date() - timedelta(days=1)
        windows.append((
            _MSK.localize(datetime(yesterday.year, yesterday.month, yesterday.day, 18, 1, 0)),
            _MSK.localize(datetime(yesterday.year, yesterday.month, yesterday.day, 23, 59, 59)),
        ))

    out, seen = [], set()
    for w_start, w_end in windows:
        qs = (
            ClientAttempt.objects
            .filter(
                created_at__gte=w_start,
                created_at__lte=w_end,
                client__is_employee=False,
                client__client__vk_id__isnull=False,
            )
            .select_related('client', 'client__client', 'client__branch')
            .distinct()
        )
        for att in qs:
            cb = att.client
            if cb.pk in seen:
                continue
            seen.add(cb.pk)
            if not _match_audience_obj(cb, rule):
                continue
            out.append(Candidate(client_branch=cb, vk_id=cb.client.vk_id))
    return out


def _no_visit_resolver(rule, now):
    """
    «Не приходил N дней» — реактивация. Берём гостей, чей ПОСЛЕДНИЙ визит был
    ровно N дней назад (по дате). Так правило срабатывает один раз — в день, когда
    гость пересёк порог, а не каждый день после него.

    N = rule.delay_days (обязателен). Одно событие → сколько угодно правил:
    «не приходил 30 дней», «не приходил 60 дней» — не мешают друг другу, потому
    что один гость в один день пересекает только один порог.
    """
    from django.db.models import Max
    from apps.tenant.branch.models import ClientBranch

    n = rule.delay_days
    if not n or n <= 0:
        return []

    target = now.astimezone(_MSK).date() - timedelta(days=n)
    qs = (
        ClientBranch.objects
        .filter(
            is_employee=False,
            client__is_active=True,
            client__vk_id__isnull=False,
        )
        .annotate(last_visit=Max('visits__visited_at'))
        .filter(last_visit__date=target)      # последний визит ровно N дней назад
        .select_related('client', 'branch')
    )
    qs = _apply_audience(qs, rule)
    return [Candidate(client_branch=cb, vk_id=cb.client.vk_id) for cb in qs]


def _subscribed_resolver(rule, now):
    """
    «Подписался N дней назад» — welcome-серия. Гости, вступившие в сообщество
    ровно N дней назад (по дате вступления). N = rule.delay_days.
    """
    from apps.tenant.branch.models import ClientVKStatus

    n = rule.delay_days
    if n is None or n < 0:
        return []

    target = now.astimezone(_MSK).date() - timedelta(days=n)
    qs = (
        ClientVKStatus.objects
        .filter(
            community_joined_at__date=target,
            client__is_employee=False,
            client__client__is_active=True,
            client__client__vk_id__isnull=False,
        )
        .select_related('client', 'client__client', 'client__branch')
    )
    out = []
    for st in qs:
        cb = st.client
        if not _match_audience_obj(cb, rule):
            continue
        out.append(Candidate(client_branch=cb, vk_id=cb.client.vk_id))
    return out


def _gift_not_claimed_resolver(rule, now):
    """
    Подарок из сториз/сайта получен, но не активирован в кафе.
    1-в-1 с send_gift_reminder_broadcasts_task: получен ≥ delay дней назад,
    НЕ активирован, срок забора ещё НЕ вышел (сгоревшим писать бессмысленно).
    Дедуп — по конкретному подарку (entity_key), а не по периоду.
    """
    from apps.tenant.inventory.models import StoryGiftEntry

    delay = rule.delay_days
    if delay is None:
        delay = _tenant_gift_reminder_days()
    if not delay or delay <= 0:
        return []

    qs = (
        StoryGiftEntry.objects
        .filter(
            received_at__isnull=False,
            received_at__lte=now - timedelta(days=delay),
            activated_at__isnull=True,
            claim_expires_at__isnull=False,   # бессрочным не напоминаем
            claim_expires_at__gt=now,          # сгоревшим — тоже
            # ⚠️ КРИТИЧНО: legacy-задача (send_gift_reminder_broadcasts_task) метит
            # отправку именно этим полем, а НЕ entity_key-логом. Без этого фильтра
            # движок написал бы второй раз тем, кому legacy уже написала. Дедуп
            # движка (entity_key) legacy-отправок не видит — значит смотрим поле.
            reminder_sent_at__isnull=True,
            client_branch__is_employee=False,
            client_branch__client__vk_id__isnull=False,
        )
        .select_related(
            'product', 'client_branch',
            'client_branch__client', 'client_branch__branch',
        )
    )
    out = []
    for e in qs:
        cb = e.client_branch
        if not _match_audience_obj(cb, rule):
            continue
        out.append(Candidate(
            client_branch=cb,
            vk_id=cb.client.vk_id,
            entity_key=f'gift:{e.pk}',
            gift=e,
        ))
    return out


def _follow_up_resolver(rule, now):
    """
    Догоняющее письмо. Берём тех, кому РОДИТЕЛЬСКОЕ правило отправило сообщение
    ≥ N дней назад (N = rule.delay_days) и кто не отреагировал:

      not_read    — сообщение так и не прочитано (BroadcastRecipient.read_at пуст;
                    прочтения проставляет существующая check_read_status_task);
      not_visited — прочитал или нет, но в кафе после этого не приходил.

    Дедуп по объекту: один догон на (правило, гость) — entity_key 'fu:<rule>:<vk_id>'.
    """
    from apps.tenant.branch.models import ClientBranchVisit
    from apps.tenant.senler.models import BroadcastRecipient, RecipientStatus

    parent = rule.parent_rule
    if not parent:
        return []
    n = rule.delay_days or 0
    cutoff = now - timedelta(days=n)

    qs = (
        BroadcastRecipient.objects
        .filter(
            send__auto_broadcast_rule=parent,
            status=RecipientStatus.SENT,
            sent_at__isnull=False,
            sent_at__lte=cutoff,
            client_branch__isnull=False,
        )
        .select_related('client_branch', 'client_branch__client', 'client_branch__branch')
    )
    if rule.follow_up_condition == 'not_read':
        qs = qs.filter(read_at__isnull=True)

    out, seen = [], set()
    for rec in qs:
        cb = rec.client_branch
        if cb.pk in seen:
            continue
        seen.add(cb.pk)

        if rule.follow_up_condition == 'not_visited':
            came = ClientBranchVisit.objects.filter(
                client=cb, visited_at__gte=rec.sent_at,
            ).exists()
            if came:
                continue

        if not _match_audience_obj(cb, rule):
            continue
        out.append(Candidate(
            client_branch=cb,
            vk_id=cb.client.vk_id,
            entity_key=f'fu:{rule.pk}:{cb.client.vk_id}',
        ))
    return out


def _tenant_gift_reminder_days() -> int:
    """
    Дефолтная задержка напоминания о подарке = настройка сети.
    ClientConfig лежит в public и привязан к Company — берём через connection.tenant.
    """
    from django.db import connection
    from apps.shared.config.models import ClientConfig
    try:
        company = getattr(connection, 'tenant', None)
        if company is None or not getattr(company, 'pk', None):
            return 0
        cfg = ClientConfig.objects.filter(company=company).first()
        return int(getattr(cfg, 'story_gift_reminder_days', 0) or 0)
    except Exception:
        return 0


# ── Каталог событий ──────────────────────────────────────────────────────────

def get_events() -> dict:
    """Каталог событий. Отдельной функцией — чтобы не импортировать модели на старте."""
    from apps.tenant.senler.models import AutoBroadcastType as T

    return {
        T.BIRTHDAY_7_DAYS: EventSpec(
            label='За 7 дней до дня рождения', dedup=DEDUP_YEAR,
            resolver=_birthday_resolver(7), placeholders=('{имя}',),
        ),
        T.BIRTHDAY_1_DAY: EventSpec(
            label='За 1 день до дня рождения', dedup=DEDUP_YEAR,
            resolver=_birthday_resolver(1), placeholders=('{имя}',),
        ),
        T.BIRTHDAY: EventSpec(
            label='День рождения', dedup=DEDUP_YEAR,
            resolver=_birthday_resolver(0), placeholders=('{имя}',),
        ),
        T.AFTER_GAME_3H: EventSpec(
            label='Через 3 часа после игры', dedup=DEDUP_DAY,
            resolver=_after_game_resolver, placeholders=('{имя}', '{адреса}'),
        ),
        T.GIFT_NOT_CLAIMED: EventSpec(
            label='Подарок из сториз/сайта не забран', dedup=DEDUP_ENTITY,
            resolver=_gift_not_claimed_resolver,
            placeholders=('{имя}', '{подарок}', '{дней_осталось}', '{адреса}'),
            default_delay_days=10,
        ),
        # ── Фаза 2 ────────────────────────────────────────────────────────────
        T.NO_VISIT_DAYS: EventSpec(
            label='Не приходил N дней (реактивация)', dedup=DEDUP_DAY,
            resolver=_no_visit_resolver, placeholders=('{имя}', '{адреса}'),
        ),
        T.SUBSCRIBED_DAYS: EventSpec(
            label='Подписался N дней назад (welcome)', dedup=DEDUP_DAY,
            resolver=_subscribed_resolver, placeholders=('{имя}', '{адреса}'),
        ),
        # ── Фаза 3 ────────────────────────────────────────────────────────────
        T.FOLLOW_UP: EventSpec(
            label='Догоняющее (не отреагировал на другое правило)', dedup=DEDUP_ENTITY,
            resolver=_follow_up_resolver, placeholders=('{имя}', '{адреса}'),
        ),
    }


# ── A/B-варианты ─────────────────────────────────────────────────────────────

def pick_variant(rule, vk_id: int):
    """
    Какой вариант текста достанется этому гостю. Выбор ДЕТЕРМИНИРОВАННЫЙ (по vk_id
    и id правила): один и тот же человек всегда попадает в одну и ту же ветку.
    Иначе он в разные дни получал бы разные варианты, и A/B было бы грязным.

    Нет активных вариантов → None (шлём rule.message_text, как раньше).
    """
    variants = [v for v in rule.variants.all() if v.is_active and v.weight > 0]
    if not variants:
        return None
    variants.sort(key=lambda v: v.pk)          # стабильный порядок
    total = sum(v.weight for v in variants)
    # md5 вместо random и вместо hash(): встроенный hash() рандомизируется между
    # процессами (PYTHONHASHSEED), т.е. гость мог бы попасть в разные ветки в
    # разных воркерах. md5 даёт один и тот же бакет всегда и везде.
    digest = hashlib.md5(f'{rule.pk}:{int(vk_id)}'.encode()).hexdigest()
    bucket = int(digest, 16) % total
    acc = 0
    for v in variants:
        acc += v.weight
        if bucket < acc:
            return v
    return variants[-1]


# ── Статистика по правилу / вариантам ────────────────────────────────────────

def rule_stats(rule) -> dict:
    """
    Отправлено / прочитано / % открытий — по правилу и по каждому A/B-варианту.
    Прочтения берутся из BroadcastRecipient.read_at, который проставляет
    существующая check_read_status_task (она выбирает получателей обобщённо и
    резолвит VK-конфиг через client_branch.branch — отправки движка подхватываются
    сама собой, править её не пришлось).
    """
    from apps.tenant.senler.models import BroadcastRecipient, RecipientStatus

    def _agg(qs):
        sent = qs.filter(status=RecipientStatus.SENT).count()
        read = qs.filter(status=RecipientStatus.SENT, read_at__isnull=False).count()
        failed = qs.filter(status=RecipientStatus.FAILED).count()
        return {
            'sent': sent,
            'read': read,
            'failed': failed,
            'open_rate': round(read / sent * 100, 1) if sent else 0.0,
        }

    base = BroadcastRecipient.objects.filter(send__auto_broadcast_rule=rule)
    out = _agg(base)
    out['variants'] = [
        {
            'id': v.pk,
            'name': v.name,
            'weight': v.weight,
            'is_active': v.is_active,
            **_agg(base.filter(send__auto_broadcast_variant=v)),
        }
        for v in rule.variants.all().order_by('pk')
    ]
    return out


# ── Частотный кэп (предохранитель) ───────────────────────────────────────────

def _weekly_cap() -> int:
    """
    Не больше N авто-сообщений одному гостю за 7 дней. Настройка сети
    (ClientConfig.auto_broadcast_weekly_cap). 0 = без ограничения (дефолт, чтобы
    ничего не изменилось у тех, кто уже живёт на legacy).
    """
    from django.db import connection
    from apps.shared.config.models import ClientConfig
    try:
        company = getattr(connection, 'tenant', None)
        if company is None or not getattr(company, 'pk', None):
            return 0
        cfg = ClientConfig.objects.filter(company=company).first()
        return int(getattr(cfg, 'auto_broadcast_weekly_cap', 0) or 0)
    except Exception:
        return 0


def _apply_frequency_cap(cands: list[Candidate], now) -> list[Candidate]:
    """Выкидывает тех, кто за последние 7 дней уже получил cap авто-сообщений."""
    from django.db.models import Count
    from apps.tenant.senler.models import AutoBroadcastLog

    cap = _weekly_cap()
    if cap <= 0 or not cands:
        return cands

    vk_ids = [c.vk_id for c in cands]
    counts = dict(
        AutoBroadcastLog.objects
        .filter(vk_id__in=vk_ids, sent_at__gte=now - timedelta(days=7))
        .values('vk_id')
        .annotate(n=Count('id'))
        .values_list('vk_id', 'n')
    )
    return [c for c in cands if counts.get(c.vk_id, 0) < cap]


# ── Аудитория ────────────────────────────────────────────────────────────────

def _apply_audience(qs, rule):
    """
    Фильтры правила поверх queryset'а ClientBranch. Пусто = без ограничения.
    Сегменты — ТАК ЖЕ, как у обычных рассылок (services.resolve_audience):
    через related_name rf_score (GuestRFScore) → поле segment. НЕ rf_segment_id.
    """
    branch_ids = list(rule.branches.values_list('id', flat=True))
    if branch_ids:
        qs = qs.filter(branch_id__in=branch_ids)
    if rule.gender_filter and rule.gender_filter != 'all':
        qs = qs.filter(client__gender=rule.gender_filter)
    seg_ids = list(rule.rf_segments.values_list('id', flat=True))
    if seg_ids:
        qs = qs.filter(rf_score__segment_id__in=seg_ids)
    return qs.distinct()


def _match_audience_obj(cb, rule) -> bool:
    """То же, но для уже загруженного ClientBranch (резолверы, идущие от события)."""
    branch_ids = set(rule.branches.values_list('id', flat=True))
    if branch_ids and cb.branch_id not in branch_ids:
        return False
    if rule.gender_filter and rule.gender_filter != 'all':
        if getattr(cb.client, 'gender', '') != rule.gender_filter:
            return False
    seg_ids = set(rule.rf_segments.values_list('id', flat=True))
    if seg_ids:
        seg = getattr(getattr(cb, 'rf_score', None), 'segment_id', None)
        if seg not in seg_ids:
            return False
    return True


# ── Дедуп (ТОТ ЖЕ лог, что у legacy — см. шапку) ─────────────────────────────

def _already_sent(spec: EventSpec, event: str, cands: list[Candidate], now) -> set:
    """
    Возвращает множество «кому уже отправляли» — vk_id (для year/day) или
    entity_key (для entity). Читает legacy-лог с тем же trigger_type.
    """
    from apps.tenant.senler.models import AutoBroadcastLog

    local_today = now.astimezone(_MSK).date()
    qs = AutoBroadcastLog.objects.filter(trigger_type=event)

    if spec.dedup == DEDUP_YEAR:
        return set(qs.filter(sent_at__year=local_today.year).values_list('vk_id', flat=True))
    if spec.dedup == DEDUP_DAY:
        return set(qs.filter(sent_at__date=local_today).values_list('vk_id', flat=True))
    # entity
    keys = [c.entity_key for c in cands if c.entity_key]
    if not keys:
        return set()
    return set(qs.filter(entity_key__in=keys).values_list('entity_key', flat=True))


def _dedup_key(spec: EventSpec, c: Candidate):
    return c.entity_key if spec.dedup == DEDUP_ENTITY else c.vk_id


# ── Окно отправки / период действия ──────────────────────────────────────────

def rule_is_due(rule, now) -> tuple[bool, str]:
    """Можно ли слать по этому правилу прямо сейчас. (можно, причина_если_нет)"""
    if not rule.is_active:
        return False, 'inactive'
    local = now.astimezone(_MSK)
    if rule.active_from and local.date() < rule.active_from:
        return False, 'not_started'
    if rule.active_to and local.date() > rule.active_to:
        return False, 'finished'
    if not (rule.send_hour_start <= local.hour < rule.send_hour_end):
        return False, 'outside_send_window'
    return True, ''


# ── Текст ────────────────────────────────────────────────────────────────────

def render_text(rule, c: Candidate, variant=None) -> str:
    """
    Подстановка переменных. Недоступные для события переменные просто пустеют.
    variant — если у правила идёт A/B, берём текст варианта вместо текста правила.
    """
    gift = c.gift
    days_left = getattr(gift, 'days_left_to_claim', None) if gift else None
    template = (variant.message_text if variant else rule.message_text) or ''
    return (
        template
        .replace('{имя}', getattr(c.client_branch.client, 'first_name', '') or '')
        .replace('{подарок}', (gift.product.name if gift and gift.product else '') or 'подарок')
        .replace('{дней_осталось}', str(days_left) if days_left is not None else '')
        .replace('{адреса}', tenant_addresses())
    )


def tenant_addresses(limit: int = 10) -> str:
    """Адреса активных точек — подарок сетевой, конкретное кафе не называем."""
    from apps.tenant.branch.models import Branch

    parts = []
    for b in Branch.objects.filter(is_active=True).select_related('config')[:limit]:
        addr = (getattr(getattr(b, 'config', None), 'address', '') or '').strip()
        parts.append(addr or b.name)
    return ', '.join(p for p in parts if p)


# ── Главное: подобрать получателей / отправить ───────────────────────────────

def resolve_recipients(rule, now=None) -> list[Candidate]:
    """
    Кому уйдёт сообщение по этому правилу ПРЯМО СЕЙЧАС (уже с учётом дедупа,
    аудитории и того, что человек не получал это раньше). Ничего не отправляет.
    Используется и предпросмотром, и реальной отправкой — то есть предпросмотр
    показывает ровно то, что и уйдёт.
    """
    now = now or timezone.now()
    events = get_events()
    spec = events.get(rule.event)
    if not spec:
        return []

    cands = spec.resolver(rule, now)
    if not cands:
        return []

    sent = _already_sent(spec, rule.event, cands, now)
    fresh = [c for c in cands if _dedup_key(spec, c) not in sent]
    # Предохранитель: не заваливать одного гостя авторассылками.
    return _apply_frequency_cap(fresh, now)


def preview_rule(rule, sample: int = 5, now=None) -> dict:
    """
    Предпросмотр перед включением: сколько человек получит и пример текста.
    НИЧЕГО НЕ ОТПРАВЛЯЕТ. Игнорирует is_active и окно отправки — показывает
    «кому ушло бы», иначе выключенное правило всегда показывало бы 0.
    """
    now = now or timezone.now()
    cands = resolve_recipients(rule, now)
    due, reason = rule_is_due(rule, now)
    return {
        'recipients': len(cands),
        'due_now': due,
        'reason': reason,
        'sample_text': render_text(rule, cands[0]) if cands else rule.message_text,
        'sample_names': [
            (getattr(c.client_branch.client, 'first_name', '') or f'vk{c.vk_id}')
            for c in cands[:sample]
        ],
    }


def run_rule(rule, now=None, dry_run: bool = False) -> dict:
    """
    Отправка по правилу. dry_run=True — считает получателей и НЕ шлёт (для E2E).

    Порядок ровно как в legacy: BroadcastSend на запуск, картинка грузится один
    раз на VK-сообщество, sleep 0.05 между сообщениями (лимит VK ≤20/сек),
    запись в AutoBroadcastLog + BroadcastRecipient.
    """
    import time

    from apps.tenant.senler.models import (
        AutoBroadcastLog, BroadcastRecipient, BroadcastSend,
        RecipientStatus, SendStatus, TriggerType,
    )
    from apps.tenant.senler.services import send_vk_message, upload_vk_photo

    now = now or timezone.now()
    due, reason = rule_is_due(rule, now)
    if not due and not dry_run:
        return {'sent': 0, 'reason': reason}

    spec = get_events().get(rule.event)
    cands = resolve_recipients(rule, now)
    if not cands:
        return {'sent': 0, 'reason': 'no_recipients'}

    if dry_run:
        return {
            'sent': 0, 'would_send': len(cands), 'dry_run': True,
            'reason': reason or 'ok',
        }

    # A/B: у каждого варианта СВОЙ BroadcastSend — тогда прочтения и статистика
    # считаются по вариантам сами собой, через уже существующие механизмы.
    sends: dict = {}          # variant_pk (или None) → BroadcastSend
    counters: dict = {}       # variant_pk (или None) → [sent, failed]

    def _send_for(variant):
        key = variant.pk if variant else None
        if key not in sends:
            sends[key] = BroadcastSend.objects.create(
                status=SendStatus.RUNNING,
                trigger_type=TriggerType.AUTO,
                triggered_by=rule.event,
                auto_broadcast_rule=rule,
                auto_broadcast_variant=variant,
                started_at=timezone.now(),
            )
            counters[key] = [0, 0]
        return sends[key], counters[key]

    attachment_cache: dict[tuple, str | None] = {}
    sent_count = failed_count = 0

    for c in cands:
        cb = c.client_branch
        try:
            senler_cfg = cb.branch.senler_config
        except Exception:
            continue
        if not senler_cfg.is_active:
            continue

        variant = pick_variant(rule, c.vk_id)
        bs, cnt = _send_for(variant)

        image = (variant.image if variant and variant.image else rule.image)
        if image:
            # Кэш на (VK-сообщество, вариант): фото, загруженное токеном одного
            # сообщества, нельзя отправить от имени другого.
            ck = (senler_cfg.pk, variant.pk if variant else None)
            if ck not in attachment_cache:
                att, _ = upload_vk_photo(senler_cfg, image)
                attachment_cache[ck] = att
            attachment = attachment_cache[ck]
        else:
            attachment = None

        ok, err, vk_msg_id = send_vk_message(
            senler_cfg, c.vk_id, render_text(rule, c, variant), attachment,
        )
        if ok:
            # ⚠️ trigger_type=rule.event — тот же ключ, что пишет legacy-задача.
            AutoBroadcastLog.objects.create(
                trigger_type=rule.event,
                vk_id=c.vk_id,
                entity_key=c.entity_key,
                rule=rule,
            )
            if c.gift is not None and not c.gift.reminder_sent_at:
                c.gift.reminder_sent_at = timezone.now()
                c.gift.save(update_fields=['reminder_sent_at'])
            BroadcastRecipient.objects.create(
                send=bs, client_branch=cb, vk_id=c.vk_id,
                status=RecipientStatus.SENT, sent_at=timezone.now(),
                vk_message_id=vk_msg_id,
            )
            cnt[0] += 1
            sent_count += 1
        else:
            BroadcastRecipient.objects.create(
                send=bs, client_branch=cb, vk_id=c.vk_id,
                status=RecipientStatus.FAILED, error=(err or '')[:512],
            )
            cnt[1] += 1
            failed_count += 1
            logger.warning('Auto-rule %s failed vk_id=%s: %s', rule.pk, c.vk_id, err)
        time.sleep(0.05)  # VK rate limit: ≤ 20 messages/second

    for key, bs in sends.items():
        s, f = counters[key]
        bs.status = SendStatus.DONE
        bs.sent_count = s
        bs.failed_count = f
        bs.recipients_count = s + f
        bs.finished_at = timezone.now()
        bs.save(update_fields=[
            'status', 'sent_count', 'failed_count', 'recipients_count', 'finished_at',
        ])
    return {'sent': sent_count, 'failed': failed_count}
