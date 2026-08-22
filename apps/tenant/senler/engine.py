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
    # StoryGiftEntry или InventoryItem, если событие про подарок.
    # ⚠️ Аннотация обязательна: без неё это атрибут класса, а не поле dataclass,
    # и Candidate(gift=...) в резолверах падал TypeError (латентный баг —
    # не всплывал, пока правила «подарок не забран» были выключены).
    gift: object = None


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


def _tenant_client_config():
    """
    ClientConfig текущего тенанта (public-таблица, OneToOne к Company).

    ⚠️ Фикс 22.08: под schema_context (а так работают celery-таски движка и
    shell) connection.tenant — это FakeTenant БЕЗ pk. Раньше оба читателя
    настроек возвращали 0 при отсутствии pk → частотный кэп и задержка
    напоминания МОЛЧА не работали во всех боевых прогонах движка (в
    предпросмотре из админки tenant настоящий — там всё выглядело живым).
    Теперь Company добираем по schema_name.
    """
    from django.db import connection
    from django_tenants.utils import get_tenant_model
    from apps.shared.config.models import ClientConfig

    company = getattr(connection, 'tenant', None)
    if company is not None and getattr(company, 'pk', None):
        return ClientConfig.objects.filter(company=company).first()
    schema = getattr(company, 'schema_name', None) or getattr(connection, 'schema_name', '')
    if not schema or schema == 'public':
        return None
    real = get_tenant_model().objects.filter(schema_name=schema).first()
    if real is None:
        return None
    return ClientConfig.objects.filter(company=real).first()


def _tenant_gift_reminder_days() -> int:
    """Дефолтная задержка напоминания о подарке = настройка сети."""
    try:
        cfg = _tenant_client_config()
        return int(getattr(cfg, 'story_gift_reminder_days', 0) or 0)
    except Exception:
        return 0


def _rf_gift_expiring_resolver(rule, now):
    """
    RF/RFM-подарок ждёт активации, и до сгорания (claim_expires_at) осталось
    ≤ delay_days дней. Одно напоминание на подарок (DEDUP_ENTITY); если в день
    срабатывания напоминание задержал антиспам-оркестратор, окно продолжает
    матчиться на следующих тиках — до самого сгорания.
    """
    from apps.tenant.inventory.models import AcquisitionSource, InventoryItem

    delay = rule.delay_days
    if delay is None or delay <= 0:
        delay = 2

    qs = (
        InventoryItem.objects
        .filter(
            acquired_from__in=(AcquisitionSource.RFM, AcquisitionSource.RF_AUTO),
            activated_at__isnull=True,
            used_at__isnull=True,
            claim_expires_at__gt=now,                                # ещё не сгорел
            claim_expires_at__lte=now + timedelta(days=delay),        # но скоро
            client_branch__is_employee=False,
            client_branch__client__vk_id__isnull=False,
        )
        .select_related(
            'product', 'client_branch',
            'client_branch__client', 'client_branch__branch',
        )
    )
    out = []
    for item in qs:
        cb = item.client_branch
        if not _match_audience_obj(cb, rule):
            continue
        out.append(Candidate(
            client_branch=cb,
            vk_id=cb.client.vk_id,
            entity_key=f'invgift:{item.pk}',
            gift=item,
        ))
    return out


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
            resolver=_no_visit_resolver, placeholders=('{имя}', '{адреса}', '{баланс}'),
        ),
        T.SUBSCRIBED_DAYS: EventSpec(
            label='Подписался N дней назад (welcome)', dedup=DEDUP_DAY,
            resolver=_subscribed_resolver, placeholders=('{имя}', '{адреса}', '{баланс}'),
        ),
        # ── Фаза 3 ────────────────────────────────────────────────────────────
        T.FOLLOW_UP: EventSpec(
            label='Догоняющее (не отреагировал на другое правило)', dedup=DEDUP_ENTITY,
            resolver=_follow_up_resolver, placeholders=('{имя}', '{адреса}', '{баланс}'),
        ),
        # ── Фаза 5: RF/RFM-награды ────────────────────────────────────────────
        T.RF_GIFT_EXPIRING: EventSpec(
            label='RF-подарок скоро сгорит (напоминание)', dedup=DEDUP_ENTITY,
            resolver=_rf_gift_expiring_resolver,
            placeholders=('{имя}', '{подарок}', '{дней_осталось}', '{адреса}', '{баланс}'),
            default_delay_days=2,
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


# ── Подарочный шаг правила (фаза 5, ТЗ §8 / §15.4) ───────────────────────────

# Пороги алгоритма назначения подарка (ТЗ §8).
RF_GIFT_BALANCE_CAP = 3000          # баланс 3000+ → магазин вместо подарка
RF_GIFT_BLOCK_UNREDEEMED = 2        # 2 несгоревших-неактивированных RF-подарка…
RF_GIFT_BLOCK_DAYS = 90             # …за 90 дней → блок новых подарков
RF_GIFT_BDAY_MARGIN_DAYS = 3        # ДР раньше конца срока подарка + 3 дня → без подарка
RF_GIFT_RECENT_EXCLUDE = 3          # не повторять 3 последних подарка гостя


def _rule_gift_tiers(rule) -> list:
    """Тиры подарочного шага правила: 'G1,G2' → ['G1', 'G2']. Пусто — шага нет."""
    raw = (getattr(rule, 'gift_tier', '') or '').strip()
    if not raw:
        return []
    return [t.strip().upper() for t in raw.split(',') if t.strip()]


def _next_birthday(birth_date, today):
    """Ближайший ДР (29 февраля → 1 марта в невисокосный год)."""
    def _safe(year):
        try:
            return birth_date.replace(year=year)
        except ValueError:
            return birth_date.replace(year=year, month=3, day=1)

    bd = _safe(today.year)
    if bd < today:
        bd = _safe(today.year + 1)
    return bd


def _prepare_rf_gift(rule, c, now):
    """
    Пытается назначить кандидату подарок по алгоритму ТЗ §8 / §15.4.

    Возвращает (InventoryItem, '') при успехе или (None, machine_reason),
    если подарочный шаг заблокирован гейтом — тогда правило уходит на
    запасной текст M0 (или пропускает гостя, если запасного текста нет).
    Гейты:
      • активный (несгоревший/неиспользованный) RF/RFM- или story-подарок;
      • баланс гостя 3000+ (мотивируем магазином, а не новым подарком);
      • 2 сгоревших без активации RF-подарка за 90 дней → блок;
      • нет доступной позиции каталога / исчерпан лимит;
      • ДР гостя раньше, чем конец срока подарка + 3 дня.
    """
    from django.db.models import Q as _Q
    from apps.tenant.inventory import reward_catalog
    from apps.tenant.inventory.models import AcquisitionSource, InventoryItem, StoryGiftEntry

    cb = c.client_branch
    client_id = cb.client_id

    rf_sources = (AcquisitionSource.RFM, AcquisitionSource.RF_AUTO)

    # Активный промо-подарок нашего контура уже на руках (ТЗ: не дублировать).
    # Покупки за баллы гостя не блокируют — это его собственность, не промо.
    has_active_rf = (
        InventoryItem.objects
        .filter(client_branch__client_id=client_id,
                acquired_from__in=rf_sources, used_at__isnull=True)
        .filter(
            _Q(activated_at__isnull=True)
            & (_Q(claim_expires_at__isnull=True) | _Q(claim_expires_at__gt=now))
            | _Q(activated_at__isnull=False)
            & (_Q(expires_at__isnull=True) | _Q(expires_at__gt=now))
        )
        .exists()
    )
    if has_active_rf:
        return None, 'active_gift'

    # Неактивированный story/website-подарок в силе — тоже активный промо.
    has_active_story = (
        StoryGiftEntry.objects
        .filter(client_branch__client_id=client_id,
                received_at__isnull=False, activated_at__isnull=True)
        .filter(_Q(claim_expires_at__isnull=True) | _Q(claim_expires_at__gt=now))
        .exists()
    )
    if has_active_story:
        return None, 'active_story_gift'

    if _coin_balance(cb) >= RF_GIFT_BALANCE_CAP:
        return None, 'balance_3000'

    # Два RF-подарка сгорели без активации за 90 дней → гостю подарки
    # не заходят, 90-дневный блок новых (ТЗ §8).
    unredeemed = (
        InventoryItem.objects
        .filter(client_branch__client_id=client_id,
                acquired_from__in=rf_sources,
                activated_at__isnull=True, used_at__isnull=True,
                claim_expires_at__gt=now - timedelta(days=RF_GIFT_BLOCK_DAYS),
                claim_expires_at__lte=now)
        .count()
    )
    if unredeemed >= RF_GIFT_BLOCK_UNREDEEMED:
        return None, 'gift_block_90d'

    # Не повторять три последних подарка гостя (по позициям каталога).
    recent_ids = list(
        InventoryItem.objects
        .filter(client_branch__client_id=client_id, catalog_item__isnull=False)
        .order_by('-created_at')
        .values_list('catalog_item_id', flat=True)[:RF_GIFT_RECENT_EXCLUDE]
    )

    item = reward_catalog.pick_reward(
        _rule_gift_tiers(rule), cb.branch, exclude_ids=recent_ids,
    )
    if item is None:
        return None, 'no_gift_available'

    days = rule.gift_lifetime_days or item.default_lifetime_days or 0

    # Правило ближайшего ДР (ТЗ §4): ДР раньше конца срока подарка + 3 дня —
    # подарочный шаг не запускается (иначе два промоподарка подряд).
    if cb.birth_date and days:
        today = timezone.localdate()
        if _next_birthday(cb.birth_date, today) <= today + timedelta(days=days + RF_GIFT_BDAY_MARGIN_DAYS):
            return None, 'birthday_near'

    issued = reward_catalog.issue_to_guest(
        item, cb,
        source=AcquisitionSource.RF_AUTO,
        lifetime_days=days,
        description=f'RF-правило «{rule.name}» #{rule.pk}',
    )
    if issued is None:
        return None, 'limit_exhausted'
    return issued, ''


# ── Частотный кэп (предохранитель) ───────────────────────────────────────────

def _weekly_cap() -> int:
    """
    Не больше N авто-сообщений одному гостю за 7 дней. Настройка сети
    (ClientConfig.auto_broadcast_weekly_cap). 0 = без ограничения (дефолт, чтобы
    ничего не изменилось у тех, кто уже живёт на legacy).
    """
    try:
        cfg = _tenant_client_config()
        return int(getattr(cfg, 'auto_broadcast_weekly_cap', 0) or 0)
    except Exception:
        return 0


def _apply_frequency_cap(cands: list[Candidate], now, event: str | None = None) -> list[Candidate]:
    """
    Выкидывает тех, кто за последние 7 дней уже получил cap авто-сообщений.

    Контур ДР отдельный (фикс 22.08): поздравления с днём рождения кэп
    не глушит никогда, и сами ДР-сообщения не съедают лимит остальных —
    иначе поток RF-реактиваций систематически душил бы поздравления.
    """
    from django.db.models import Count
    from apps.tenant.senler.models import AutoBroadcastLog, AutoBroadcastType as T

    birthday_events = {T.BIRTHDAY, T.BIRTHDAY_1_DAY, T.BIRTHDAY_7_DAYS}
    if event in birthday_events:
        return cands

    cap = _weekly_cap()
    if cap <= 0 or not cands:
        return cands

    vk_ids = [c.vk_id for c in cands]
    counts = dict(
        AutoBroadcastLog.objects
        .filter(vk_id__in=vk_ids, sent_at__gte=now - timedelta(days=7))
        .exclude(trigger_type__in=birthday_events)
        .values('vk_id')
        .annotate(n=Count('id'))
        .values_list('vk_id', 'n')
    )
    return [c for c in cands if counts.get(c.vk_id, 0) < cap]


# ── Оркестратор RF-коммуникаций (ТЗ авторассылок v1.1, §5) ───────────────────
# Глобальный антиспам ТОЛЬКО для RF-событий (реактивация / welcome /
# догоняющее). ДР, after-game и напоминания о подарках живут своими
# контурами — оркестратор их не трогает и они не съедают RF-лимиты.
# Включается флагом сети ClientConfig.rf_orchestrator_enabled; по умолчанию
# ВЫКЛЮЧЕН — поведение прода не меняется, пока флаг не взведён.

RF_MIN_GAP_HOURS        = 72   # минимум между двумя RF-сообщениями
RF_MAX_PER_14D          = 2    # не более 2 RF-сообщений за 14 дней
RF_MAX_PER_30D          = 3    # не более 3 за 30 дней
RF_SCAN_COOLDOWN_DAYS   = 7    # после скана/визита RF молчит 7 полных дней
RF_BIRTHDAY_FREEZE_DAYS = 7    # заморозка D-7..D+7 вокруг дня рождения


def _rf_events() -> set:
    from apps.tenant.senler.models import AutoBroadcastType as T
    return {T.NO_VISIT_DAYS, T.SUBSCRIBED_DAYS, T.FOLLOW_UP, T.RF_GIFT_EXPIRING}


def _orchestrator_enabled() -> bool:
    try:
        cfg = _tenant_client_config()
        return bool(getattr(cfg, 'rf_orchestrator_enabled', False))
    except Exception:
        return False


def _apply_rf_orchestrator(cands: list[Candidate], event: str, now) -> list[Candidate]:
    """
    Отсекает кандидатов, для которых RF-сообщение сейчас нарушило бы
    глобальный антиспам ТЗ v1.1 §5:
      • < 72 часов с прошлого RF-сообщения;
      • уже 2 RF-сообщения за 14 дней или 3 за 30;
      • визит за последние 7 дней (гость «вернулся» — реактивация не нужна,
        это же отменяет и догоняющие шаги);
      • день рождения в окне ±7 дней (там работает ДР-контур с подарком).

    Кросс-правила в одном тике решаются сами: правила идут последовательно
    по приоритету, лог пишется на каждую отправку — следующее правило уже
    видит свежие RF-сообщения и 72-часовой зазор их отсечёт.
    """
    if not cands or event not in _rf_events() or not _orchestrator_enabled():
        return cands

    from datetime import date as _date
    from apps.tenant.senler.models import AutoBroadcastLog
    from apps.tenant.branch.models import ClientBranch, ClientBranchVisit

    vk_ids = [c.vk_id for c in cands]
    today = now.astimezone(_MSK).date()

    # 72ч + лимиты 14/30 дней — по логу RF-событий
    last_sent: dict = {}
    cnt14: dict = {}
    cnt30: dict = {}
    for vk, sent_at in (AutoBroadcastLog.objects
                        .filter(vk_id__in=vk_ids, trigger_type__in=_rf_events(),
                                sent_at__gte=now - timedelta(days=30))
                        .values_list('vk_id', 'sent_at')):
        cnt30[vk] = cnt30.get(vk, 0) + 1
        if sent_at >= now - timedelta(days=14):
            cnt14[vk] = cnt14.get(vk, 0) + 1
        if vk not in last_sent or sent_at > last_sent[vk]:
            last_sent[vk] = sent_at

    # Cooldown после визита: любой визит за последние 7 дней
    recent_visit_vk = set(
        ClientBranchVisit.objects
        .filter(client__client__vk_id__in=vk_ids,
                visited_at__gte=now - timedelta(days=RF_SCAN_COOLDOWN_DAYS))
        .values_list('client__client__vk_id', flat=True)
    )

    # Заморозка вокруг ДР (D-7..D+7)
    bday_frozen: set = set()
    for vk, bd in (ClientBranch.objects
                   .filter(client__vk_id__in=vk_ids, birth_date__isnull=False)
                   .values_list('client__vk_id', 'birth_date')):
        if vk in bday_frozen:
            continue
        for yr in (today.year - 1, today.year, today.year + 1):
            try:
                occ = bd.replace(year=yr)
            except ValueError:            # 29 февраля
                occ = _date(yr, 3, 1)
            if abs((occ - today).days) <= RF_BIRTHDAY_FREEZE_DAYS:
                bday_frozen.add(vk)
                break

    min_gap = timedelta(hours=RF_MIN_GAP_HOURS)
    out: list[Candidate] = []
    for c in cands:
        vk = c.vk_id
        ls = last_sent.get(vk)
        if ls is not None and (now - ls) < min_gap:
            continue
        if cnt14.get(vk, 0) >= RF_MAX_PER_14D or cnt30.get(vk, 0) >= RF_MAX_PER_30D:
            continue
        if vk in recent_visit_vk:
            continue
        if vk in bday_frozen:
            continue
        out.append(c)
    return out


# ── Аудитория ────────────────────────────────────────────────────────────────

def _apply_audience(qs, rule):
    """
    Фильтры правила поверх queryset'а ClientBranch. Пусто = без ограничения.
    Сегменты — ТАК ЖЕ, как у обычных рассылок (services.resolve_recipients):
    rf_score — related_name от GuestRFScore.client, который указывает на
    guest.Client (НЕ ClientBranch). От ClientBranch путь идёт через client:
    client → rf_score → segment. НЕ rf_segment_id.
    """
    branch_ids = list(rule.branches.values_list('id', flat=True))
    if branch_ids:
        qs = qs.filter(branch_id__in=branch_ids)
    if rule.gender_filter and rule.gender_filter != 'all':
        qs = qs.filter(client__gender=rule.gender_filter)
    seg_ids = list(rule.rf_segments.values_list('id', flat=True))
    if seg_ids:
        qs = qs.filter(client__rf_score__segment_id__in=seg_ids)
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
        seg = getattr(getattr(cb.client, 'rf_score', None), 'segment_id', None)
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

def _coin_balance(client_branch) -> int:
    """Баланс монет гостя на его точке (баллы пер-точечные)."""
    from django.db.models import Q as _Q, Sum
    from apps.tenant.branch.models import CoinTransaction

    agg = CoinTransaction.objects.filter(client=client_branch).aggregate(
        income=Sum('amount', filter=_Q(type='income')),
        expense=Sum('amount', filter=_Q(type='expense')),
    )
    return (agg['income'] or 0) - (agg['expense'] or 0)


def render_text(rule, c: Candidate, variant=None, template_override=None) -> str:
    """
    Подстановка переменных. Недоступные для события переменные просто пустеют.
    variant — если у правила идёт A/B, берём текст варианта вместо текста правила.
    template_override — готовый шаблон вместо текста правила/варианта
    (запасной M0-текст подарочного шага, когда подарок выдать нельзя).
    """
    gift = c.gift
    days_left = getattr(gift, 'days_left_to_claim', None) if gift else None
    if template_override is not None:
        template = template_override
    else:
        template = (variant.message_text if variant else rule.message_text) or ''
    if '{баланс}' in template:
        # Лениво: баланс считается только когда переменная реально в тексте.
        template = template.replace('{баланс}', str(_coin_balance(c.client_branch)))
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

    # In-batch дедуп (фикс 22.08): гость с профилями на нескольких точках
    # приходит от резолвера несколькими кандидатами с одним vk_id — без
    # этого он получил бы столько же одинаковых сообщений за один прогон
    # (лог-дедуп ловит только СЛЕДУЮЩИЕ прогоны). Оставляем первого.
    seen_keys: set = set()
    unique: list[Candidate] = []
    for c in fresh:
        k = _dedup_key(spec, c)
        if k in seen_keys:
            continue
        seen_keys.add(k)
        unique.append(c)

    # Предохранитель: не заваливать одного гостя авторассылками.
    capped = _apply_frequency_cap(unique, now, event=rule.event)

    # RF-оркестратор (ТЗ v1.1 §5): 72ч / 2 за 14д / 3 за 30д / пауза после
    # визита / заморозка вокруг ДР. Работает только при включённом флаге сети.
    return _apply_rf_orchestrator(capped, rule.event, now)


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
    from apps.tenant.senler.services import (
        SoftTimeLimitExceeded, send_vk_message, upload_vk_photo,
    )

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

    def _finalize_sends():
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

    try:
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

        # ── Подарочный шаг (фаза 5): назначить подарок ПЕРЕД отправкой ──────
        # (ТЗ §8: подарок появляется в «Моих подарках» одновременно с
        # сообщением). Сбой отправки компенсируется отзывом (§15.6).
        gift_issued = None
        template_override = None
        if _rule_gift_tiers(rule):
            gift_issued, gift_reason = _prepare_rf_gift(rule, c, now)
            if gift_issued is not None:
                c.gift = gift_issued
            else:
                fallback = (rule.gift_fallback_text or '').strip()
                if not fallback:
                    logger.info(
                        'Auto-rule %s: подарок недоступен (%s), vk_id=%s пропущен',
                        rule.pk, gift_reason, c.vk_id,
                    )
                    continue
                template_override = fallback

        try:
            ok, err, vk_msg_id = send_vk_message(
                senler_cfg, c.vk_id,
                render_text(rule, c, variant, template_override=template_override),
                attachment,
            )
        except SoftTimeLimitExceeded:
            # Таймаут между выдачей и отправкой: подарок без сообщения
            # висеть не должен (§15.6) — отзываем и уходим в общий обработчик.
            if gift_issued is not None:
                from apps.tenant.inventory.reward_catalog import revoke_issued_item
                revoke_issued_item(gift_issued)
            raise
        if not ok and gift_issued is not None:
            # Сообщение не ушло — компенсация: подарок отозван, лимит возвращён.
            from apps.tenant.inventory.reward_catalog import revoke_issued_item
            revoke_issued_item(gift_issued)
            c.gift = None
        if ok:
            # ⚠️ trigger_type=rule.event — тот же ключ, что пишет legacy-задача.
            AutoBroadcastLog.objects.create(
                trigger_type=rule.event,
                vk_id=c.vk_id,
                entity_key=c.entity_key,
                rule=rule,
            )
            # reminder_sent_at есть только у StoryGiftEntry (общий дедуп с
            # legacy-задачей); у InventoryItem (RF/RFM-подарки) поля нет —
            # их дедуп держит entity_key-лог.
            if c.gift is not None and hasattr(c.gift, 'reminder_sent_at') \
                    and not c.gift.reminder_sent_at:
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
    except SoftTimeLimitExceeded:
        # Celery-таймаут посреди правила: всё отправленное уже в дедуп-логе,
        # следующий 15-минутный тик дошлёт остальных без дублей. Закрываем
        # счётчики (иначе запуски висят «running») и отдаём таймаут наверх.
        _finalize_sends()
        raise

    _finalize_sends()
    return {'sent': sent_count, 'failed': failed_count}
