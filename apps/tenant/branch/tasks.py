"""
VK polling task for group messages.

Usage:
  — With Celery (recommended):
      from apps.tenant.branch.tasks import poll_vk_messages_task
      poll_vk_messages_task.delay(schema_name='levone', branch_id=1)

  — Without Celery (management command):
      python manage.py poll_vk_messages

  — Register in Celery Beat (celery.py):
      app.conf.beat_schedule = {
          'poll-vk-messages': {
              'task': 'apps.tenant.branch.tasks.poll_all_vk_messages_task',
              'schedule': 30.0,   # every 30 seconds
          },
      }

VK Polling uses messages.getConversations + messages.getHistory API:
  https://dev.vk.com/ru/method/messages.getConversations
  https://dev.vk.com/ru/method/messages.getHistory

Alternatively, configure Callback API in VK group settings
and point it at POST /api/v1/vk/callback/ — then no polling is needed.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# ── VK API helpers ────────────────────────────────────────────────────────────

VK_API_VERSION = '5.131'
VK_API_BASE    = 'https://api.vk.com/method/'


def _vk_call(method: str, token: str, **params) -> dict:
    """Make a synchronous VK API call. Raises RuntimeError on API errors."""
    params['access_token'] = token
    params['v']            = VK_API_VERSION
    url = VK_API_BASE + method + '?' + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f'Network error calling VK {method}: {e}') from e

    if 'error' in data:
        err = data['error']
        raise RuntimeError(f'VK API {method} error {err.get("error_code")}: {err.get("error_msg")}')

    return data.get('response', {})


# ── Per-branch polling ────────────────────────────────────────────────────────

# Регулярный поллинг: сколько страниц истории (по 200) максимум тянуть на ОДИН
# conv за тик при инкрементальном догоне. Реальные сообщения сосредоточены
# вверху, до курсора доходим обычно за 1 страницу. 10 — запас.
MAX_PAGES_INCREMENTAL = 10

# Полная реконсиляция (backfill / weekly self-heal): глубже, т.к. реальное
# сообщение гостя может быть погребено под сотнями рассылок. 40*200 = 8000
# сообщений на conv — покрывает практически любого гостя.
MAX_PAGES_FULL = 40

# Сколько страниц getConversations (по 200) максимум за тик. 2000 диалогов
# хватает любому текущему тенанту; защита от runaway-цикла.
MAX_CONV_PAGES = 4

# Окно свежести backstop-поллинга (LU-42): poll поднимает только
# неотвеченные диалоги с сообщением не старше этого срока. Древний
# неотвеченный бэклог не всплывает (real-time даёт Callback).
POLL_RECENT_WINDOW_SEC = 30 * 86400


def _is_outgoing(msg: dict) -> bool:
    """out=1 / from_id<0 / admin_author_id → сообщение от сообщества (не гость)."""
    return (
        msg.get('out') == 1
        or msg.get('from_id', 0) < 0
        or bool(msg.get('admin_author_id'))
    )


def _context_from(prev_msg: dict | None) -> tuple[str, int | None]:
    """
    Из предыдущего (более старого) сообщения в треде достаём контекст «на что
    ответил гость»: текст + дата. Берём ЛЮБОЕ предыдущее исходящее (авто-опрос,
    промо, ответ менеджера). Если предыдущее — тоже гость, контекст не нужен
    (он уже виден как сообщение выше). LU-40.
    """
    if not prev_msg:
        return '', None
    if not _is_outgoing(prev_msg):
        return '', None  # предыдущее — тоже гость, контекст не нужен
    txt = (prev_msg.get('text') or '').strip()
    if not txt:
        # текст пустой (например фото-промо без подписи) — берём пометку по вложению
        atts = prev_msg.get('attachments') or []
        if atts:
            txt = f'[{atts[0].get("type", "вложение")}]'
    return txt, prev_msg.get('date')


def _save_vk_message(group_id, msg, handle_incoming, handle_admin_reply, prev_msg=None) -> int | None:
    """
    Сохраняем одно VK-сообщение через подходящий handler. Возвращает saved.pk или None.
    prev_msg — предыдущее (более старое) сообщение в треде, для контекста «на что
    ответил гость» (LU-40). handlers переданы параметрами чтобы не импортить лениво.
    """
    text = (msg.get('text') or '').strip()
    attachments = msg.get('attachments') or []
    # Пропускаем только если нет НИ текста, НИ вложений (фото-только — оставляем).
    if not text and not attachments:
        return None

    from_id = msg.get('from_id', 0)
    if _is_outgoing(msg):
        # LU-35: ОТФИЛЬТРОВАТЬ РАССЫЛКИ. Рассылки идут через messages.send
        # как outgoing (out=1), но БЕЗ admin_author_id. Реальный ответ
        # менеджера всегда имеет admin_author_id (id конкретного админа).
        if not msg.get('admin_author_id'):
            return None
        saved = handle_admin_reply(
            group_id=group_id,
            peer_id=msg.get('peer_id') or msg.get('from_id') or 0,
            message_id=msg['id'],
            text=text,
            vk_date=msg.get('date'),
            attachments=attachments,
        )
    else:
        # Гость — добавляем контекст «на что ответил» из предыдущего исходящего.
        reply_to_text, reply_to_date = _context_from(prev_msg)
        saved = handle_incoming(
            group_id=group_id,
            from_id=from_id,
            message_id=msg['id'],
            text=text,
            vk_date=msg.get('date'),
            attachments=attachments,
            reply_to_text=reply_to_text,
            reply_to_date=reply_to_date,
        )
    return getattr(saved, 'pk', None) if saved is not None else None


def _sync_one_conversation(
    *, peer_id, vk_sender_id, last_polled, token, group_id,
    max_pages, handle_incoming, handle_admin_reply, errors,
    first_seen_pages=None,
):
    """
    Догоняет историю ОДНОГО диалога. Пагинация getHistory от новых к старым
    (rev=0 + offset), сохраняет реальные сообщения с id > last_polled через
    _save_vk_message (рассылки фильтруются там же, дедуп по vk_message_id).

    Возвращает (new_count, new_cursor, complete):
      complete=True  — дошли до last_polled ИЛИ до дна истории (вся НОВАЯ
                       часть отсканирована полностью);
      complete=False — упёрлись в max_pages, остался необработанный «хвост».

    КРИТИЧНО: курсор (new_cursor) продвигаем до fetched_max ТОЛЬКО при
    complete=True. Иначе оставляем last_polled. Это чинит баг LU-35: раньше
    курсор уезжал на свежую рассылку (max id), хотя реальные сообщения НИЖЕ
    ещё не были сохранены → инкремент их больше никогда не забирал.
    Курсор = «всё с id <= курсора уже в БД» — инвариант, который мы держим.
    """
    new_count = 0
    fetched_max = last_polled
    offset = 0
    complete = False
    # LU-42: в инкрементальном poll НЕ выкачиваем всю историю first-seen (cursor=0)
    # диалога — у гостя могут быть тысячи старых рассылок, poll зацикливается
    # (re-fetch верхних страниц каждый цикл, курсор не двигается → SoftTimeLimit,
    # воркеры голодают). Берём first_seen_pages верхних страниц и форсим курсор.
    # reconcile (deep backfill) first_seen_pages не передаёт → пагинирует полностью.
    _capped = bool(first_seen_pages) and last_polled == 0
    for _page in range(first_seen_pages if _capped else max_pages):
        try:
            hist = _vk_call(
                'messages.getHistory', token,
                peer_id=peer_id, group_id=group_id,
                count=200, offset=offset, rev=0, mark_as_read=0,
            )
        except RuntimeError as e:
            errors.append(f'peer {peer_id}: {e}')
            break

        items = hist.get('items', [])
        if not items:
            complete = True  # дошли до дна
            break

        page_max = max((int(m.get('id') or 0) for m in items), default=0)
        fetched_max = max(fetched_max, page_max)

        reached_cursor = False
        for i, msg in enumerate(items):
            mid = int(msg.get('id') or 0)
            if mid <= last_polled:
                # offset-paging DESC: эта и все следующие уже в БД
                reached_cursor = True
                break
            # items идут от новых к старым → предыдущее (более старое) сообщение
            # в треде = следующий элемент списка. Для контекста «на что ответил».
            prev_msg = items[i + 1] if i + 1 < len(items) else None
            if _save_vk_message(group_id, msg, handle_incoming, handle_admin_reply, prev_msg=prev_msg):
                new_count += 1

        if reached_cursor:
            complete = True
            break
        if len(items) < 200:
            complete = True  # последняя страница истории
            break
        offset += 200

    if _capped:
        complete = True  # курсор → fetched_max, не зацикливаемся на старье (LU-42)
    new_cursor = fetched_max if complete else last_polled
    return new_count, new_cursor, complete


def poll_branch_messages(branch_id: int) -> dict:
    """
    Регулярный инкрементальный поллинг VK-сообщений для точки.

    Модель (LU-35 v2, надёжная — 2026-06):
      1) getConversations(filter='all', count=200) С ПАГИНАЦИЕЙ (offset) —
         проходим ВСЕ диалоги, а не топ-50. Иначе рассылки вытесняют реальные
         диалоги ниже топ-50 и они не опрашиваются.
      2) Для каждого conv сравниваем VK last_message.id с локальным курсором
         (max last_polled_vk_msg_id по всем conv этого vk_sender_id). Если
         ничего новее курсора — пропускаем (0 запросов getHistory).
      3) Иначе _sync_one_conversation догоняет новые реальные сообщения и,
         ТОЛЬКО при полном скане, продвигает курсор.

    Сетка надёжности дополнена еженедельной reconcile-таской (self-heal,
    см. reconcile_all_vk_messages_task) — она ловит всё что инкремент мог
    пропустить (битые курсоры, обрывы, гэпы).

    Returns: {'new_messages': int, 'errors': list[str]}
    """
    from apps.tenant.branch.api.services import (
        handle_vk_incoming_message,
        handle_vk_admin_reply_from_poll,
    )
    from apps.tenant.branch.models import TestimonialConversation
    from apps.tenant.senler.models import SenlerConfig

    try:
        config = SenlerConfig.objects.select_related('branch').get(branch_id=branch_id)
    except SenlerConfig.DoesNotExist:
        return {'new_messages': 0, 'errors': [f'SenlerConfig not found for branch {branch_id}']}

    if not config.vk_community_token:
        return {'new_messages': 0, 'errors': ['vk_community_token not set']}

    token    = config.vk_community_token
    group_id = config.vk_group_id
    errors: list[str] = []
    new_count = 0
    conv_offset = 0
    import time as _time
    _now_ts = int(_time.time())  # LU-42 recency gate

    for _conv_page in range(MAX_CONV_PAGES):
        try:
            # filter='unanswered' — только диалоги, где ГОСТЬ написал последним
            # и ответа нет. Рассылки и отвеченные (исходящее последним) сюда НЕ
            # попадают → poll больше не перебирает тысячи всплывших рассылкой
            # диалогов (это убивало его по SoftTimeLimit, LU-42). Ответы
            # менеджера ловит Callback message_reply (раньше был filter='all'
            # ради LU-08, но теперь real-time Callback здоров и покрывает это).
            convs_resp = _vk_call(
                'messages.getConversations', token,
                group_id=group_id, filter='unanswered', count=200, offset=conv_offset,
            )
        except RuntimeError as e:
            errors.append(str(e))
            break

        items = convs_resp.get('items', [])
        if not items:
            break

        for item in items:
            peer    = (item.get('conversation') or {}).get('peer', {})
            peer_id = peer.get('id')
            if not peer_id or peer_id < 0:
                continue  # group/service chats — только 1-on-1

            vk_last_id = int((item.get('last_message') or {}).get('id') or 0)
            vk_sender_id = str(peer_id)

            # Backstop поднимает только СВЕЖИЕ неотвеченные (real-time даёт
            # Callback). Древний неотвеченный бэклог НЕ всплывает (LU-42) —
            # дешёвый date-гейт ДО запроса курсора/getHistory.
            _md = int((item.get('last_message') or {}).get('date') or 0)
            if _md and _md < _now_ts - POLL_RECENT_WINDOW_SEC:
                continue

            # Курсор = max last_polled по всем conv этого vk_sender_id (могут быть
            # legacy branch=X + новый branch=None — двигаем синхронно).
            last_polled = max(
                (int(c.last_polled_vk_msg_id or 0)
                 for c in TestimonialConversation.objects.filter(vk_sender_id=vk_sender_id)),
                default=0,
            )

            # Нет ничего новее курсора → пропуск (0 getHistory).
            if vk_last_id and vk_last_id <= last_polled:
                continue

            n, new_cursor, _complete = _sync_one_conversation(
                peer_id=peer_id, vk_sender_id=vk_sender_id, last_polled=last_polled,
                token=token, group_id=group_id, max_pages=MAX_PAGES_INCREMENTAL,
                handle_incoming=handle_vk_incoming_message,
                handle_admin_reply=handle_vk_admin_reply_from_poll,
                errors=errors, first_seen_pages=2,
            )
            new_count += n
            if new_cursor > last_polled:
                TestimonialConversation.objects.filter(
                    vk_sender_id=vk_sender_id,
                ).update(last_polled_vk_msg_id=new_cursor)

        if len(items) < 200:
            break
        conv_offset += 200

    return {'new_messages': new_count, 'errors': errors}


def reconcile_branch_messages(branch_id: int, max_pages: int = MAX_PAGES_FULL) -> dict:
    """
    ПОЛНАЯ реконсиляция (self-heal) одной точки: для КАЖДОГО диалога сканирует
    историю до дна (игнорируя текущий курсор!), сохраняет все пропущенные
    реальные сообщения, затем выставляет курсор в истинный max.

    Чинит «отравленные» курсоры (LU-35 баг): когда курсор стоял на свежей
    рассылке, а реальные сообщения гостя НИЖЕ него не были сохранены.

    Тяжелее обычного poll'а (сканирует всех), поэтому запускается:
      - вручную через manage.py backfill_vk_messages
      - еженедельно через reconcile_all_vk_messages_task (beat)

    Returns: {'new_messages': int, 'reconciled_convs': int, 'errors': [...]}
    """
    from apps.tenant.branch.api.services import (
        handle_vk_incoming_message,
        handle_vk_admin_reply_from_poll,
    )
    from apps.tenant.branch.models import TestimonialConversation
    from apps.tenant.senler.models import SenlerConfig

    try:
        config = SenlerConfig.objects.select_related('branch').get(branch_id=branch_id)
    except SenlerConfig.DoesNotExist:
        return {'new_messages': 0, 'reconciled_convs': 0, 'errors': [f'no SenlerConfig branch {branch_id}']}
    if not config.vk_community_token:
        return {'new_messages': 0, 'reconciled_convs': 0, 'errors': ['no token']}

    token, group_id = config.vk_community_token, config.vk_group_id
    errors: list[str] = []
    new_count = 0
    reconciled = 0

    # Берём ВСЕ vk_sender_id из БД (а не из getConversations) — так чиним даже те
    # диалоги, что давно ушли из топа активности и не вернутся.
    sender_ids = list(
        TestimonialConversation.objects
        .exclude(vk_sender_id='')
        .values_list('vk_sender_id', flat=True)
        .distinct()
    )
    for vk_sender_id in sender_ids:
        try:
            peer_id = int(vk_sender_id)
        except (TypeError, ValueError):
            continue
        if peer_id <= 0:
            continue
        # last_polled=0 → полный скан до дна, ловим всё что ниже отравленного курсора
        n, new_cursor, complete = _sync_one_conversation(
            peer_id=peer_id, vk_sender_id=vk_sender_id, last_polled=0,
            token=token, group_id=group_id, max_pages=max_pages,
            handle_incoming=handle_vk_incoming_message,
            handle_admin_reply=handle_vk_admin_reply_from_poll,
            errors=errors,
        )
        new_count += n
        reconciled += 1
        # Курсор ставим в истинный max только при complete (полный скан).
        if complete and new_cursor > 0:
            TestimonialConversation.objects.filter(
                vk_sender_id=vk_sender_id,
            ).update(last_polled_vk_msg_id=new_cursor)
        elif not complete:
            errors.append(f'peer {peer_id}: incomplete (>{max_pages} страниц)')

    return {'new_messages': new_count, 'reconciled_convs': reconciled, 'errors': errors}


# ── Celery tasks ──────────────────────────────────────────────────────────────

from celery import shared_task


@shared_task(
    name='apps.tenant.branch.tasks.poll_vk_messages_task',
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def poll_vk_messages_task(self, schema_name: str, branch_id: int) -> dict:
    """
    Celery task: poll VK messages for one branch in a specific tenant schema.
    Must be called with the correct schema_name for django-tenants.
    """
    from django_tenants.utils import schema_context
    try:
        with schema_context(schema_name):
            result = poll_branch_messages(branch_id)
            if result['errors']:
                logger.warning(
                    'VK poll branch=%s schema=%s errors=%s',
                    branch_id, schema_name, result['errors'],
                )
            return result
    except Exception as exc:
        logger.exception('VK poll failed schema=%s branch=%s', schema_name, branch_id)
        raise self.retry(exc=exc)


def ensure_today_daily_codes() -> int:
    """
    Идемпотентно создаёт коды дня (game/quest/birthday) для всех активных точек
    ТЕКУЩЕГО тенанта на сегодня. get_or_create → повторный вызов ничего не дублирует.
    Возвращает число созданных кодов. Должна вызываться внутри schema_context тенанта.

    Используется и beat-таском (03:00), и лениво при открытии экрана кодов —
    чтобы коды появлялись даже если beat пропустил тик (напр. Redis был read-only).
    """
    import random
    from apps.tenant.branch.models import Branch, DailyCode, DailyCodePurpose, current_code_date

    today = current_code_date()  # кодовые сутки начинаются в 03:00 MSK
    purposes = [p.value for p in DailyCodePurpose]
    created = 0
    for branch in Branch.objects.filter(is_active=True):
        for purpose in purposes:
            _, was_created = DailyCode.objects.get_or_create(
                branch=branch,
                purpose=purpose,
                valid_date=today,
                defaults={'code': f'{random.randint(0, 99999):05d}'},
            )
            if was_created:
                created += 1
    return created


@shared_task(name='apps.tenant.branch.tasks.generate_daily_codes_task')
def generate_daily_codes_task() -> dict:
    """
    Celery Beat task: generate 5-digit DailyCodes for every active branch
    in every tenant for today (game, quest, birthday purposes).
    Runs daily at 03:00 Moscow time (configured in main/celery.py).
    """
    from django_tenants.utils import get_tenant_model, schema_context
    from apps.tenant.branch.models import current_code_date

    TenantModel = get_tenant_model()
    today = current_code_date()
    created_total = 0

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        with schema_context(tenant.schema_name):
            created_total += ensure_today_daily_codes()

    logger.info('generate_daily_codes: created=%d date=%s', created_total, today)
    return {'created': created_total, 'date': str(today)}


@shared_task(name='apps.tenant.branch.tasks.push_daily_codes_task')
def push_daily_codes_task() -> dict:
    """
    Celery Beat task: утром (08:00 MSK) шлёт админам каждого тенанта push
    со сводкой кодов дня. Коды генерируются в 03:00, к 08:00 уже готовы.
    """
    from django_tenants.utils import get_tenant_model, schema_context
    from apps.tenant.branch.models import current_code_date
    from apps.tenant.analytics.auto_reply import push_daily_codes

    PURPOSE_LABEL = {'BIRTHDAY': 'ДР', 'SUPERPRIZE': 'Игра', 'OTHER': 'Квест'}

    TenantModel = get_tenant_model()
    today = current_code_date()
    sent_total = 0

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        body = ''
        with schema_context(tenant.schema_name):
            from apps.tenant.branch.models import DailyCode
            codes = list(
                DailyCode.objects.filter(valid_date=today, branch__is_active=True)
                .select_related('branch')
                .order_by('branch__name')
            )
            if not codes:
                continue
            by_branch: dict[str, list[str]] = {}
            for c in codes:
                name = c.branch.name if c.branch_id else '—'
                by_branch.setdefault(name, []).append(f'{PURPOSE_LABEL.get(c.purpose, c.purpose)} {c.code}')
            parts = [f'{name}: {" · ".join(items)}' for name, items in by_branch.items()]
            body = ' | '.join(parts)

        if body:
            try:
                res = push_daily_codes(tenant.schema_name, getattr(tenant, 'name', tenant.schema_name), body)
                sent_total += res.get('sent', 0)
            except Exception as e:
                logger.warning('push_daily_codes failed for %s: %s', tenant.schema_name, e)

    logger.info('push_daily_codes: sent=%d date=%s', sent_total, today)
    return {'sent': sent_total, 'date': str(today)}


@shared_task(name='apps.tenant.branch.tasks.poll_all_vk_messages_task')
def poll_all_vk_messages_task() -> dict:
    """
    Celery Beat task: iterate ALL active tenants and poll VK messages for each
    branch that has SenlerConfig configured.
    Runs every 30 seconds (configured in main/celery.py beat_schedule).
    """
    from django_tenants.utils import get_tenant_model, schema_context

    TenantModel = get_tenant_model()
    total_new   = 0
    total_err: list[str] = []

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        with schema_context(tenant.schema_name):
            from apps.tenant.senler.models import SenlerConfig
            seen_groups: set[int] = set()
            for cfg in SenlerConfig.objects.filter(is_active=True).select_related('branch'):
                if cfg.vk_group_id in seen_groups:
                    continue
                seen_groups.add(cfg.vk_group_id)
                result = poll_branch_messages(cfg.branch_id)
                total_new += result['new_messages']
                total_err.extend(
                    f'[{tenant.schema_name}/branch={cfg.branch_id}] {e}'
                    for e in result['errors']
                )

    if total_err:
        logger.warning('VK poll all errors: %s', total_err)

    return {'new_messages': total_new, 'errors': total_err}


@shared_task(name='apps.tenant.branch.tasks.reconcile_all_vk_messages_task')
def reconcile_all_vk_messages_task() -> dict:
    """
    Celery Beat task (self-heal): раз в неделю проходит ВСЕ тенанты и делает
    полную реконсиляцию VK-историй — ловит всё, что инкрементальный poll мог
    пропустить (битые/отравленные курсоры, обрывы, гэпы).

    Это «страховочная сетка» гарантии «не пропускаем ничего»: даже если
    онлайн-poll где-то ошибётся, недельный reconcile это исправит.

    Запуск (main/celery.py beat_schedule): раз в неделю ночью.
    """
    from django_tenants.utils import get_tenant_model, schema_context

    TenantModel = get_tenant_model()
    total_new = 0
    total_convs = 0
    total_err: list[str] = []

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        with schema_context(tenant.schema_name):
            from apps.tenant.senler.models import SenlerConfig
            seen_groups: set[int] = set()
            for cfg in SenlerConfig.objects.filter(is_active=True).select_related('branch'):
                if cfg.vk_group_id in seen_groups:
                    continue
                seen_groups.add(cfg.vk_group_id)
                result = reconcile_branch_messages(cfg.branch_id)
                total_new += result['new_messages']
                total_convs += result.get('reconciled_convs', 0)
                total_err.extend(
                    f'[{tenant.schema_name}] {e}' for e in result['errors']
                )

    logger.info(
        'VK reconcile all: new=%d convs=%d errors=%d',
        total_new, total_convs, len(total_err),
    )
    if total_err:
        logger.warning('VK reconcile errors (first 20): %s', total_err[:20])

    return {'new_messages': total_new, 'reconciled_convs': total_convs, 'errors': total_err[:50]}


# ── VK вложения (фото): скачивание в media + очистка по сроку хранения ─────────

VK_ATTACHMENT_RETENTION_DAYS = 90  # скользящее окно хранения фото в нашей базе
_VK_PHOTO_MAX_BYTES = 15 * 1024 * 1024  # 15 МБ на файл — защита от мусора


def _download_one_vk_photo(schema_name: str, msg_pk: int, idx: int, url: str) -> str | None:
    """Скачивает одно фото в MEDIA_ROOT/vk_attachments/<schema>/<pk>_<idx>.jpg.
    Возвращает media-относительный путь или None при ошибке."""
    import os
    import urllib.request
    from django.conf import settings

    rel_dir  = os.path.join('vk_attachments', schema_name)
    abs_dir  = os.path.join(settings.MEDIA_ROOT, rel_dir)
    rel_path = os.path.join(rel_dir, f'{msg_pk}_{idx}.jpg')
    abs_path = os.path.join(settings.MEDIA_ROOT, rel_path)
    try:
        os.makedirs(abs_dir, exist_ok=True)
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read(_VK_PHOTO_MAX_BYTES + 1)
        if not data or len(data) > _VK_PHOTO_MAX_BYTES:
            logger.warning('VK photo skip (empty/too big) msg=%s idx=%s', msg_pk, idx)
            return None
        with open(abs_path, 'wb') as f:
            f.write(data)
        return rel_path.replace('\\', '/')
    except Exception:
        logger.warning('VK photo download failed msg=%s idx=%s', msg_pk, idx, exc_info=True)
        return None


@shared_task(name='apps.tenant.branch.tasks.download_vk_attachments_task')
def download_vk_attachments_task(schema_name: str, message_pk: int) -> dict:
    """
    Скачивает фото-вложения сообщения (по сохранённым VK-url'ам) в наш media
    и заменяет {'src':url} → {'file':media-path}. Идемпотентно: уже скачанные
    (downloaded/file) и очищенные (purged) — пропускает.
    Вызывается отложенно из VK-обработчиков, чтобы не тормозить VK Callback.
    """
    from django_tenants.utils import schema_context
    with schema_context(schema_name):
        from apps.tenant.branch.models import TestimonialMessage
        try:
            msg = TestimonialMessage.objects.get(pk=message_pk)
        except TestimonialMessage.DoesNotExist:
            return {'skipped': 'not_found'}

        atts = list(msg.attachments or [])
        downloaded = 0
        for i, a in enumerate(atts):
            if a.get('file') or a.get('purged') or not a.get('src'):
                continue
            local = _download_one_vk_photo(schema_name, message_pk, i, a['src'])
            if local:
                atts[i] = {
                    'type': a.get('type', 'photo'),
                    'file': local,
                    'width': a.get('width'),
                    'height': a.get('height'),
                }
                downloaded += 1
        if downloaded:
            TestimonialMessage.objects.filter(pk=message_pk).update(attachments=atts)
        return {'downloaded': downloaded}


@shared_task(name='apps.tenant.branch.tasks.purge_old_vk_attachments_task')
def purge_old_vk_attachments_task() -> dict:
    """
    Скользящая очистка (90 дней): удаляет ФАЙЛЫ фото старше окна из media по всем
    тенантам. Текст сообщения остаётся; вложение помечается {'purged':True}, в UI
    показывается «фото удалено по сроку хранения». Запускается beat'ом раз в сутки.
    """
    import os
    from datetime import timedelta
    from django.conf import settings
    from django.utils import timezone
    from django_tenants.utils import get_tenant_model, schema_context

    cutoff = timezone.now() - timedelta(days=VK_ATTACHMENT_RETENTION_DAYS)
    TenantModel = get_tenant_model()
    purged_files = 0

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        with schema_context(tenant.schema_name):
            from apps.tenant.branch.models import TestimonialMessage
            qs = (
                TestimonialMessage.objects
                .filter(created_at__lt=cutoff)
                .exclude(attachments=[])
            )
            for msg in qs.iterator():
                atts = list(msg.attachments or [])
                changed = False
                for i, a in enumerate(atts):
                    if a.get('purged'):
                        continue
                    f = a.get('file')
                    if f:
                        try:
                            os.remove(os.path.join(settings.MEDIA_ROOT, f))
                        except FileNotFoundError:
                            pass
                        except Exception:
                            logger.warning('purge: не удалось удалить %s', f, exc_info=True)
                        purged_files += 1
                    atts[i] = {'type': a.get('type', 'photo'), 'purged': True}
                    changed = True
                if changed:
                    TestimonialMessage.objects.filter(pk=msg.pk).update(attachments=atts)

    logger.info('purge_old_vk_attachments: удалено файлов=%d (>%d дн)',
                purged_files, VK_ATTACHMENT_RETENTION_DAYS)
    return {'purged': purged_files}


# ── VK membership catchup via Long Poll ───────────────────────────────────────

_MEMBERSHIP_EVENTS = frozenset({'group_join', 'group_leave', 'message_allow', 'message_deny'})


def longpoll_catchup_branch(schema_name: str, branch_id: int) -> dict:
    """
    Получает пропущенные события подписки/отписки через VK Group Long Poll API.

    Алгоритм:
      1. Запрашивает свежий Long Poll-сервер (groups.getLongPollServer).
      2. Если сохранённый ts пуст — только сохраняет текущий ts (первый запуск).
      3. Если ts совпадает — новых событий нет.
      4. Если ts расходится — делает запрос к Long Poll с сохранённым ts:
           • Успех      → обрабатывает membership-события, обновляет ts.
           • failed=1   → ts устарел (слишком большой пропуск). Падаем на
                          bulk-sync через groups.isMember — запускает management-
                          command sync_vk_status асинхронно.
           • failed=2/3 → ключ протух, обновляем ts без catchup.

    Returns: {'events_processed': int, 'ts_updated': bool, 'errors': list[str]}
    """
    from apps.tenant.senler.models import SenlerConfig
    from apps.tenant.branch.api.services import apply_vk_membership_event

    try:
        config = SenlerConfig.objects.get(branch_id=branch_id, is_active=True)
    except SenlerConfig.DoesNotExist:
        return {'events_processed': 0, 'ts_updated': False, 'errors': ['SenlerConfig not found']}

    if not config.vk_community_token:
        return {'events_processed': 0, 'ts_updated': False, 'errors': ['vk_community_token not set']}

    token    = config.vk_community_token
    group_id = config.vk_group_id
    errors: list[str] = []

    # Step 1 — свежий Long Poll сервер
    try:
        lp = _vk_call('groups.getLongPollServer', token, group_id=group_id)
    except RuntimeError as e:
        return {'events_processed': 0, 'ts_updated': False, 'errors': [str(e)]}

    server    = lp['server']
    key       = lp['key']
    ts_fresh  = str(lp['ts'])
    ts_stored = config.longpoll_ts or ''

    # Step 2 — первый запуск: просто запоминаем ts
    if not ts_stored:
        config.longpoll_ts = ts_fresh
        config.save(update_fields=['longpoll_ts'])
        logger.info('VK LongPoll first run: saved ts=%s group=%s', ts_fresh, group_id)
        return {'events_processed': 0, 'ts_updated': True, 'errors': []}

    # Step 3 — ts не изменился: новых событий нет
    if ts_stored == ts_fresh:
        return {'events_processed': 0, 'ts_updated': False, 'errors': []}

    # Step 4 — запрашиваем события с момента ts_stored
    lp_url = f'{server}?act=a_check&key={key}&ts={ts_stored}&wait=1'
    try:
        with urllib.request.urlopen(lp_url, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        errors.append(f'LongPoll request error: {e}')
        config.longpoll_ts = ts_fresh
        config.save(update_fields=['longpoll_ts'])
        return {'events_processed': 0, 'ts_updated': True, 'errors': errors}

    failed = data.get('failed')

    if failed == 1:
        # ts слишком старый — события потеряны, нужен bulk-sync
        new_ts = str(data.get('ts', ts_fresh))
        config.longpoll_ts = new_ts
        config.save(update_fields=['longpoll_ts'])
        logger.warning(
            'VK LongPoll ts too old (gap too large) for group %s — '
            'falling back to bulk membership sync', group_id,
        )
        # Запускаем bulk sync асинхронно чтобы не блокировать beat
        vk_bulk_membership_sync_task.delay(schema_name=schema_name, branch_id=branch_id)
        return {
            'events_processed': 0, 'ts_updated': True,
            'errors': [f'LongPoll ts too old for group {group_id}, bulk sync scheduled'],
        }

    if failed in (2, 3):
        # Ключ протух — обновляем ts, при следующем запуске всё будет нормально
        config.longpoll_ts = ts_fresh
        config.save(update_fields=['longpoll_ts'])
        return {'events_processed': 0, 'ts_updated': True, 'errors': [f'LongPoll key expired (failed={failed})']}

    # Step 5 — обрабатываем пойманные события
    events_processed = 0
    for update in data.get('updates', []):
        event_type = update.get('type')
        if event_type not in _MEMBERSHIP_EVENTS:
            continue
        obj        = update.get('object', {})
        vk_user_id = obj.get('user_id')
        if not vk_user_id:
            continue
        try:
            updated = apply_vk_membership_event(
                group_id=group_id,
                vk_user_id=vk_user_id,
                event_type=event_type,
            )
            if updated:
                events_processed += 1
        except Exception as e:
            errors.append(f'{event_type} uid={vk_user_id}: {e}')

    new_ts = str(data.get('ts', ts_fresh))
    config.longpoll_ts = new_ts
    config.save(update_fields=['longpoll_ts'])

    if events_processed:
        logger.info(
            'VK LongPoll catchup group=%s: %d membership events processed',
            group_id, events_processed,
        )

    return {'events_processed': events_processed, 'ts_updated': True, 'errors': errors}


@shared_task(name='apps.tenant.branch.tasks.vk_membership_catchup_task')
def vk_membership_catchup_task() -> dict:
    """
    Celery Beat task: catchup пропущенных membership-событий VK для всех тенантов.
    Запускается каждые 5 минут.

    В штатном режиме (Callback работает) — находит 0 событий, просто обновляет ts.
    После простоя — забирает group_join/group_leave/message_allow/message_deny
    которые пришли пока сервер был недоступен.
    """
    from django_tenants.utils import get_tenant_model, schema_context

    TenantModel      = get_tenant_model()
    total_events     = 0
    total_errors: list[str] = []

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        with schema_context(tenant.schema_name):
            from apps.tenant.senler.models import SenlerConfig
            seen_groups: set[int] = set()
            for cfg in SenlerConfig.objects.filter(is_active=True):
                if cfg.vk_group_id in seen_groups:
                    continue
                seen_groups.add(cfg.vk_group_id)
                result = longpoll_catchup_branch(tenant.schema_name, cfg.branch_id)
                total_events += result['events_processed']
                total_errors.extend(
                    f'[{tenant.schema_name}/branch={cfg.branch_id}] {e}'
                    for e in result['errors']
                )

    if total_errors:
        logger.warning('VK membership catchup errors: %s', total_errors)

    return {'events_processed': total_events, 'errors': total_errors}


@shared_task(
    name='apps.tenant.branch.tasks.vk_bulk_membership_sync_task',
    bind=True,
    max_retries=2,
    default_retry_delay=120,
)
def vk_bulk_membership_sync_task(self, schema_name: str, branch_id: int) -> dict:
    """
    Fallback: если Long Poll ts протух (пропуск > лимита VK), делаем полную
    синхронизацию статуса подписки через groups.isMember для всех гостей ветки.
    Повторяет логику management-команды sync_vk_status для одной точки.
    """
    from django_tenants.utils import schema_context
    from apps.tenant.senler.models import SenlerConfig
    from apps.tenant.branch.models import ClientBranch, ClientVKStatus
    from django.utils import timezone

    with schema_context(schema_name):
        try:
            config = SenlerConfig.objects.select_related('branch').get(branch_id=branch_id)
        except SenlerConfig.DoesNotExist:
            return {'synced': 0, 'errors': ['SenlerConfig not found']}

        token    = config.vk_community_token
        group_id = config.vk_group_id

        if not token:
            return {'synced': 0, 'errors': ['vk_community_token not set']}

        # Все пары (vk_id, cb_id) по всем Branch с тем же vk_group_id
        all_pairs = list(
            ClientBranch.objects
            .filter(branch__senler_config__vk_group_id=group_id)
            .exclude(client__vk_id__isnull=True)
            .values_list('client__vk_id', 'id')
        )

        if not all_pairs:
            return {'synced': 0, 'errors': []}

        errors: list[str] = []
        now    = timezone.now()
        BATCH  = 500

        # Шаг 1: запрашиваем VK API с дедуплицированными vk_id
        unique_vk_ids = list(dict.fromkeys(uid for uid, _ in all_pairs))
        member_set:  set[int] = set()
        checked_ids: set[int] = set()  # только те vk_id, по которым API ответил успешно

        for i in range(0, len(unique_vk_ids), BATCH):
            batch_ids    = unique_vk_ids[i:i + BATCH]
            user_ids_str = ','.join(str(uid) for uid in batch_ids)
            try:
                resp = _vk_call('groups.isMember', token, group_id=group_id, user_ids=user_ids_str, extended=0)
                for item in (resp if isinstance(resp, list) else []):
                    uid = item['user_id']
                    checked_ids.add(uid)
                    if item.get('member'):
                        member_set.add(uid)
            except RuntimeError as e:
                errors.append(f'batch {i}: {e}')
                # vk_id этого батча не попадут в checked_ids → DB не тронем

        # Шаг 2: обновляем ClientVKStatus только для тех, по кому пришёл ответ VK
        synced = 0
        for vk_id, cb_id in all_pairs:
            if vk_id not in checked_ids:
                continue  # API упал для этого батча — не трогаем, чтобы не затереть данные
            is_member = vk_id in member_set
            try:
                vk_status, created = ClientVKStatus.objects.get_or_create(
                    client_id=cb_id,
                    defaults={
                        'is_community_member':  is_member,
                        'community_joined_at':  now if is_member else None,
                        'community_via_app':    False if is_member else None,
                    },
                )
                if not created:
                    update_fields: list[str] = []
                    if is_member and not vk_status.is_community_member:
                        vk_status.is_community_member = True
                        vk_status.community_joined_at = now
                        update_fields += ['is_community_member', 'community_joined_at']
                    elif not is_member and vk_status.is_community_member:
                        vk_status.is_community_member = False
                        vk_status.community_joined_at = None
                        vk_status.community_via_app   = None
                        update_fields += ['is_community_member', 'community_joined_at', 'community_via_app']
                    if update_fields:
                        vk_status.save(update_fields=update_fields)
                        synced += 1
            except Exception as e:
                errors.append(f'vk_id={vk_id}: {e}')

        logger.info('VK bulk membership sync schema=%s branch=%s: synced=%d', schema_name, branch_id, synced)
        return {'synced': synced, 'errors': errors}
