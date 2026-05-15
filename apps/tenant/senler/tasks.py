"""
Celery tasks for automatic VK broadcasts.

Schedule (main/celery.py beat_schedule):
  send-birthday-broadcasts  — daily at 10:00 Moscow time
  send-after-game-broadcast — every 15 minutes (09:00–21:00 window enforced in task)
  send-after-game-morning   — daily at 09:00, handles yesterday-evening games
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import pytz
from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

_MSK = pytz.timezone('Europe/Moscow')


# ── Birthday broadcasts ───────────────────────────────────────────────────────

@shared_task(name='apps.tenant.senler.tasks.send_birthday_broadcasts_task')
def send_birthday_broadcasts_task() -> dict:
    """
    Daily task at 10:00: sends birthday VK messages for 3 triggers per tenant.

    Trigger  → send date relative to today
    birthday_7d → birthday is in 7 days
    birthday_1d → birthday is in 1 day
    birthday    → birthday is today

    Deduplication: each (trigger_type, vk_id) is sent at most once per calendar year.
    """
    from django_tenants.utils import get_tenant_model, schema_context
    from apps.tenant.branch.models import ClientBranch
    from apps.tenant.senler.models import (
        AutoBroadcastLog, AutoBroadcastTemplate, AutoBroadcastType,
        BroadcastRecipient, BroadcastSend, RecipientStatus, SendStatus, TriggerType,
    )
    from apps.tenant.senler.services import send_vk_message, upload_vk_photo

    today = timezone.now().astimezone(_MSK).date()

    triggers = [
        (AutoBroadcastType.BIRTHDAY_7_DAYS, today + timedelta(days=7)),
        (AutoBroadcastType.BIRTHDAY_1_DAY,  today + timedelta(days=1)),
        (AutoBroadcastType.BIRTHDAY,        today),
    ]

    TenantModel = get_tenant_model()
    total_sent = 0

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                # Load active templates for this tenant
                templates = {
                    t.type: t
                    for t in AutoBroadcastTemplate.objects.filter(
                        type__in=[tt for tt, _ in triggers],
                        is_active=True,
                    )
                }
                if not templates:
                    continue

                for trigger_type, target_date in triggers:
                    template = templates.get(trigger_type)
                    if not template:
                        continue

                    # Guests whose birthday month/day matches target_date
                    # birth_date_set_at__lte: дата ДР должна быть установлена
                    # не менее 30 дней назад (защита от злоупотреблений)
                    candidates = (
                        ClientBranch.objects
                        .filter(
                            is_employee=False,
                            client__is_active=True,
                            client__vk_id__isnull=False,
                            birth_date__month=target_date.month,
                            birth_date__day=target_date.day,
                            birth_date_set_at__lte=today - timedelta(days=30),
                        )
                        .select_related('client', 'branch')
                    )

                    if not candidates.exists():
                        continue

                    # Already sent this trigger this calendar year
                    already_sent = set(
                        AutoBroadcastLog.objects
                        .filter(trigger_type=trigger_type, sent_at__year=today.year)
                        .values_list('vk_id', flat=True)
                    )

                    # Create BroadcastSend record for this run
                    bs = BroadcastSend.objects.create(
                        auto_broadcast_template=template,
                        status=SendStatus.RUNNING,
                        trigger_type=TriggerType.AUTO,
                        triggered_by=trigger_type,
                        started_at=timezone.now(),
                    )

                    # Cache image uploads per VK community (senler_cfg.pk).
                    # Each community stores photos in its own album —
                    # an attachment uploaded via community A cannot be sent
                    # from community B's token.
                    attachment_cache: dict[int, str | None] = {}
                    sent_count = 0
                    failed_count = 0

                    for cb in candidates:
                        vk_id = cb.client.vk_id
                        if vk_id in already_sent:
                            continue

                        try:
                            senler_cfg = cb.branch.senler_config
                        except Exception:
                            continue

                        if not senler_cfg.is_active:
                            continue

                        if template.image:
                            if senler_cfg.pk not in attachment_cache:
                                att, _ = upload_vk_photo(senler_cfg, template.image)
                                attachment_cache[senler_cfg.pk] = att
                            attachment = attachment_cache[senler_cfg.pk]
                        else:
                            attachment = None

                        ok, err, vk_msg_id = send_vk_message(
                            senler_cfg, vk_id, template.message_text, attachment
                        )
                        if ok:
                            AutoBroadcastLog.objects.create(
                                trigger_type=trigger_type, vk_id=vk_id
                            )
                            # Запись в историю рассылок: участвует в подсчётах
                            # «% открываемости» и в ежечасном опросе read_at.
                            BroadcastRecipient.objects.create(
                                send=bs,
                                client_branch=cb,
                                vk_id=vk_id,
                                status=RecipientStatus.SENT,
                                sent_at=timezone.now(),
                                vk_message_id=vk_msg_id,
                            )
                            already_sent.add(vk_id)
                            total_sent += 1
                            sent_count += 1
                        else:
                            BroadcastRecipient.objects.create(
                                send=bs,
                                client_branch=cb,
                                vk_id=vk_id,
                                status=RecipientStatus.FAILED,
                                error=(err or '')[:512],
                            )
                            failed_count += 1
                            logger.warning(
                                'Birthday broadcast failed vk_id=%s trigger=%s: %s',
                                vk_id, trigger_type, err,
                            )
                        time.sleep(0.05)  # VK rate limit: ≤ 20 messages/second

                    bs.status = SendStatus.DONE
                    bs.sent_count = sent_count
                    bs.failed_count = failed_count
                    bs.recipients_count = sent_count + failed_count
                    bs.finished_at = timezone.now()
                    bs.save(update_fields=[
                        'status', 'sent_count', 'failed_count',
                        'recipients_count', 'finished_at',
                    ])

        except Exception as exc:
            logger.exception(
                'send_birthday_broadcasts_task failed tenant=%s: %s',
                tenant.schema_name, exc,
            )

    return {'sent': total_sent}


# ── After-game (3h) broadcast ─────────────────────────────────────────────────

@shared_task(name='apps.tenant.senler.tasks.send_after_game_broadcast_task')
def send_after_game_broadcast_task(process_evening: bool = False) -> dict:
    """
    Sends 'after_game_3h' message to guests who played ~3 hours ago.

    Normal mode (process_evening=False, runs every 15 min):
      - Window: games played between (now-3h-20min) and (now-3h)
      - Only executes if current Moscow time is 09:00–21:00
      - Games where 3h mark falls after 21:00 are skipped (morning task handles them)

    Evening mode (process_evening=True, runs at 09:00):
      - Window: yesterday's games played between 18:01 and 23:59 Moscow time
        (their 3h mark was 21:01–26:59, i.e., after 21:00 or next-day morning)
      - Sends now (at 09:00)

    Deduplication: at most one send per (vk_id) per calendar day.
    """
    from django_tenants.utils import get_tenant_model, schema_context
    from apps.tenant.game.models import ClientAttempt
    from apps.tenant.senler.models import (
        AutoBroadcastLog, AutoBroadcastTemplate, AutoBroadcastType,
        BroadcastRecipient, BroadcastSend, RecipientStatus, SendStatus, TriggerType,
    )
    from apps.tenant.senler.services import send_vk_message, upload_vk_photo

    now = timezone.now()
    now_local = now.astimezone(_MSK)
    today = now_local.date()

    if process_evening:
        # Yesterday's games 18:01–23:59 whose 3h mark was after 21:00
        yesterday = today - timedelta(days=1)
        window_start = _MSK.localize(
            datetime(yesterday.year, yesterday.month, yesterday.day, 18, 1, 0)
        )
        window_end = _MSK.localize(
            datetime(yesterday.year, yesterday.month, yesterday.day, 23, 59, 59)
        )
    else:
        # Only send between 09:00 and 21:00 Moscow time
        if not (9 <= now_local.hour < 21):
            return {'sent': 0, 'reason': 'outside_send_window'}

        # Games played ~3h ago (20-min overlap prevents missed windows)
        window_end   = now - timedelta(hours=3)
        window_start = now - timedelta(hours=3, minutes=20)

    TenantModel = get_tenant_model()
    total_sent = 0

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                try:
                    template = AutoBroadcastTemplate.objects.get(
                        type=AutoBroadcastType.AFTER_GAME_3H,
                        is_active=True,
                    )
                except AutoBroadcastTemplate.DoesNotExist:
                    continue

                # Already sent today
                already_sent_today = set(
                    AutoBroadcastLog.objects
                    .filter(
                        trigger_type=AutoBroadcastType.AFTER_GAME_3H,
                        sent_at__date=today,
                    )
                    .values_list('vk_id', flat=True)
                )

                attempts = (
                    ClientAttempt.objects
                    .filter(
                        created_at__gte=window_start,
                        created_at__lte=window_end,
                        client__is_employee=False,
                        client__client__vk_id__isnull=False,
                    )
                    .select_related('client', 'client__client', 'client__branch')
                    .distinct()
                )

                if not attempts.exists():
                    continue

                # Create BroadcastSend record for this run
                bs = BroadcastSend.objects.create(
                    auto_broadcast_template=template,
                    status=SendStatus.RUNNING,
                    trigger_type=TriggerType.AUTO,
                    triggered_by=AutoBroadcastType.AFTER_GAME_3H,
                    started_at=timezone.now(),
                )

                attachment_cache: dict[int, str | None] = {}
                sent_count = 0
                failed_count = 0

                for attempt in attempts:
                    cb = attempt.client
                    vk_id = cb.client.vk_id

                    if vk_id in already_sent_today:
                        continue

                    try:
                        senler_cfg = cb.branch.senler_config
                    except Exception:
                        continue

                    if not senler_cfg.is_active:
                        continue

                    if template.image:
                        if senler_cfg.pk not in attachment_cache:
                            att, _ = upload_vk_photo(senler_cfg, template.image)
                            attachment_cache[senler_cfg.pk] = att
                        attachment = attachment_cache[senler_cfg.pk]
                    else:
                        attachment = None

                    ok, err, vk_msg_id = send_vk_message(
                        senler_cfg, vk_id, template.message_text, attachment
                    )
                    if ok:
                        AutoBroadcastLog.objects.create(
                            trigger_type=AutoBroadcastType.AFTER_GAME_3H,
                            vk_id=vk_id,
                        )
                        BroadcastRecipient.objects.create(
                            send=bs,
                            client_branch=cb,
                            vk_id=vk_id,
                            status=RecipientStatus.SENT,
                            sent_at=timezone.now(),
                            vk_message_id=vk_msg_id,
                        )
                        already_sent_today.add(vk_id)
                        total_sent += 1
                        sent_count += 1
                    else:
                        BroadcastRecipient.objects.create(
                            send=bs,
                            client_branch=cb,
                            vk_id=vk_id,
                            status=RecipientStatus.FAILED,
                            error=(err or '')[:512],
                        )
                        failed_count += 1
                        logger.warning(
                            'After-game broadcast failed vk_id=%s: %s', vk_id, err
                        )
                    time.sleep(0.05)  # VK rate limit: ≤ 20 messages/second

                bs.status = SendStatus.DONE
                bs.sent_count = sent_count
                bs.failed_count = failed_count
                bs.recipients_count = sent_count + failed_count
                bs.finished_at = timezone.now()
                bs.save(update_fields=[
                    'status', 'sent_count', 'failed_count',
                    'recipients_count', 'finished_at',
                ])

        except Exception as exc:
            logger.exception(
                'send_after_game_broadcast_task failed tenant=%s: %s',
                tenant.schema_name, exc,
            )

    return {'sent': total_sent}


# ── Read-status polling ──────────────────────────────────────────────────────

def _check_read_for_config(cfg, items_with_vk_id, now, tenant_schema):
    """
    Given a SenlerConfig and a list of (vk_id, vk_message_id_int, save_fn) tuples,
    polls VK API and calls save_fn(item) for each message that was read.
    Returns count of newly marked items.
    """
    from apps.tenant.senler.services import _vk_call

    marked = 0
    for i in range(0, len(items_with_vk_id), 100):
        batch = items_with_vk_id[i:i + 100]
        peer_ids = ','.join(str(vk_id) for vk_id, _, _ in batch)
        try:
            data = _vk_call('messages.getConversationsById', {
                'peer_ids': peer_ids,
                'access_token': cfg.vk_community_token,
                'v': '5.131',
            })
            if 'error' in data:
                logger.warning(
                    'VK getConversationsById error tenant=%s cfg=%s: %s',
                    tenant_schema, cfg.pk,
                    data['error'].get('error_msg', ''),
                )
                continue

            read_map: dict[int, int] = {}
            for conv in data.get('response', {}).get('items', []):
                peer_id = conv.get('peer', {}).get('id')
                out_read = conv.get('out_read', 0)
                if peer_id:
                    read_map[peer_id] = out_read

            for vk_id, msg_id_int, save_fn in batch:
                last_read = read_map.get(vk_id, 0)
                if msg_id_int and last_read >= msg_id_int:
                    save_fn(now)
                    marked += 1

        except Exception as exc:
            logger.warning(
                'check_read_status batch error tenant=%s: %s',
                tenant_schema, exc,
            )
            continue

        time.sleep(0.5)  # VK rate limit

    return marked


@shared_task(name='apps.tenant.senler.tasks.check_read_status_task')
def check_read_status_task() -> dict:
    """
    Hourly task: polls VK API to check which sent messages have been read.

    Checks two types of outgoing messages:
    1. BroadcastRecipient — рассылки и авторассылки
    2. TestimonialMessage(source=ADMIN_REPLY) — ответы на отзывы

    Uses VK API `messages.getConversationsById` to compare `in_read` (ID of the
    last message read by the user) with the stored `vk_message_id`. If
    `in_read >= vk_message_id`, the message was read.

    Schedule: every hour via Celery Beat.
    """
    from collections import defaultdict

    from django_tenants.utils import get_tenant_model, schema_context

    from apps.tenant.senler.models import (
        BroadcastRecipient, RecipientStatus, SenlerConfig,
    )

    TenantModel = get_tenant_model()
    tenants = TenantModel.objects.exclude(schema_name='public')

    total_marked = 0
    # 35 дней (чуть больше стандартного 30-дневного окна дашборда), чтобы
    # открываемость на длинных периодах не занижалась: VK может проставить
    # «прочитано» через несколько недель, если гость зашёл в чат позже.
    # Раньше было 7 дней — этого хватало пока BR-записи были свежими, но
    # с накоплением истории создавало «слепое пятно».
    cutoff = timezone.now() - timedelta(days=35)

    for tenant in tenants:
        try:
            with schema_context(tenant.schema_name):
                # ── 1. BroadcastRecipient (рассылки и авторассылки) ──────────
                # Авто-рассылки (ДР, after-game) идут с send.broadcast=None,
                # поэтому senler_config берётся через client_branch.branch.
                # Для ручных рассылок client_branch может быть SET_NULL —
                # тогда fallback на send.broadcast.branch.
                unread_broadcasts = list(
                    BroadcastRecipient.objects.filter(
                        status=RecipientStatus.SENT,
                        vk_message_id__isnull=False,
                        read_at__isnull=True,
                        sent_at__gte=cutoff,
                    ).select_related(
                        'client_branch__branch__senler_config',
                        'send__broadcast__branch__senler_config',
                    )
                )

                by_config: dict[int, list] = defaultdict(list)
                for r in unread_broadcasts:
                    cfg = None
                    try:
                        if r.client_branch_id:
                            cfg = r.client_branch.branch.senler_config
                        elif r.send.broadcast_id:
                            cfg = r.send.broadcast.branch.senler_config
                    except (SenlerConfig.DoesNotExist, AttributeError):
                        cfg = None
                    if not cfg or not cfg.is_active:
                        continue

                    def _save_broadcast(now, _r=r):
                        _r.read_at = now
                        _r.save(update_fields=['read_at'])
                    by_config[cfg.pk].append(
                        (r.vk_id, r.vk_message_id, _save_broadcast)
                    )

                # ── 2. TestimonialMessage ADMIN_REPLY (ответы на отзывы) ─────
                from apps.tenant.branch.models import TestimonialMessage

                unread_replies = list(
                    TestimonialMessage.objects.filter(
                        source=TestimonialMessage.Source.ADMIN_REPLY,
                        vk_message_id__gt='',
                        read_at__isnull=True,
                        created_at__gte=cutoff,
                    ).select_related(
                        'conversation__branch__senler_config',
                    )
                )

                for msg in unread_replies:
                    try:
                        cfg = msg.conversation.branch.senler_config
                        vk_id = msg.conversation.vk_sender_id
                        if not cfg.is_active or not vk_id:
                            continue
                        try:
                            msg_id_int = int(msg.vk_message_id)
                        except (ValueError, TypeError):
                            continue

                        def _save_reply(now, _msg=msg):
                            _msg.read_at = now
                            _msg.save(update_fields=['read_at'])

                        by_config[cfg.pk].append(
                            (int(vk_id), msg_id_int, _save_reply)
                        )
                    except (SenlerConfig.DoesNotExist, AttributeError):
                        continue

                # ── Poll VK API per config ────────────────────────────────────
                now = timezone.now()
                for cfg_pk, items in by_config.items():
                    try:
                        cfg = SenlerConfig.objects.get(pk=cfg_pk)
                    except SenlerConfig.DoesNotExist:
                        continue

                    total_marked += _check_read_for_config(
                        cfg, items, now, tenant.schema_name,
                    )

        except Exception as exc:
            logger.exception(
                'check_read_status_task failed tenant=%s: %s',
                tenant.schema_name, exc,
            )

    return {'marked_read': total_marked}


# ── Manual broadcast runner (RF / сегментные рассылки) ────────────────────────

@shared_task(name='apps.tenant.senler.tasks.run_broadcast_task', bind=True)
def run_broadcast_task(self, schema_name: str, send_ids: list[int]) -> dict:
    """
    Запускает run_broadcast для списка BroadcastSend ПОСЛЕДОВАТЕЛЬНО,
    одним таском на весь запрос рассылки.

    Почему последовательно (НЕ дробить по точкам на отдельные таски):
    внутри run_broadcast есть time.sleep(0.05) — лимит VK ≤20 msg/s,
    иначе сообщество банят. Celery worker запущен с --concurrency=2:
    параллельные таски на один vk_community_token суммарно превысят
    20 msg/s. Один серийный таск на запрос исключает это.

    Вызывать с корректным schema_name — django-tenants: celery-воркер
    стартует в public-схеме, тенанта надо выставить явно.
    """
    from django_tenants.utils import schema_context
    from apps.tenant.senler.models import BroadcastSend
    from apps.tenant.senler.services import run_broadcast

    processed = 0
    with schema_context(schema_name):
        for sid in send_ids:
            try:
                send = BroadcastSend.objects.get(pk=sid)
            except BroadcastSend.DoesNotExist:
                logger.warning(
                    'run_broadcast_task: BroadcastSend %s not found (schema=%s)',
                    sid, schema_name,
                )
                continue
            try:
                run_broadcast(send)
                processed += 1
            except Exception:
                logger.exception(
                    'run_broadcast_task: run_broadcast failed send=%s schema=%s',
                    sid, schema_name,
                )
    return {'processed': processed, 'total': len(send_ids), 'schema': schema_name}
