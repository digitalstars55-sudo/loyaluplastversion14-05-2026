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

def poll_branch_messages(branch_id: int) -> dict:
    """
    Fetch recent unread conversations from VK group for the given branch
    and save new messages to TestimonialConversation/TestimonialMessage.

    Returns: {'new_messages': int, 'errors': list[str]}
    """
    from apps.tenant.branch.api.services import handle_vk_incoming_message
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

    try:
        # Get recent conversations (up to 20)
        convs_resp = _vk_call(
            'messages.getConversations',
            token,
            group_id=group_id,
            filter='unread',
            count=20,
        )
    except RuntimeError as e:
        return {'new_messages': 0, 'errors': [str(e)]}

    items = convs_resp.get('items', [])

    for item in items:
        conv_data = item.get('conversation', {})
        peer      = conv_data.get('peer', {})
        peer_id   = peer.get('id')

        if not peer_id or peer_id < 0:
            # Skip group chats and service chats, only 1-on-1 DMs
            continue

        try:
            # Fetch last 5 messages from this conversation.
            # mark_as_read=0 keeps messages unread in VK so the guest
            # doesn't see them disappear from unread before we actually reply.
            hist = _vk_call(
                'messages.getHistory',
                token,
                peer_id=peer_id,
                group_id=group_id,
                count=5,
                rev=0,          # newest first
                mark_as_read=0,
            )
        except RuntimeError as e:
            errors.append(f'peer {peer_id}: {e}')
            continue

        for msg in hist.get('items', []):
            # Skip outgoing messages (from_id == -group_id)
            if msg.get('from_id', 0) < 0:
                continue
            text = (msg.get('text') or '').strip()
            if not text:
                continue

            saved = handle_vk_incoming_message(
                group_id=group_id,
                from_id=msg['from_id'],
                message_id=msg['id'],
                text=text,
            )
            if saved is not None:
                new_count += 1

    return {'new_messages': new_count, 'errors': errors}


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


@shared_task(name='apps.tenant.branch.tasks.generate_daily_codes_task')
def generate_daily_codes_task() -> dict:
    """
    Celery Beat task: generate 5-digit DailyCodes for every active branch
    in every tenant for today (game, quest, birthday purposes).
    Runs daily at 03:00 Moscow time (configured in main/celery.py).
    """
    import random
    from django.utils import timezone
    from django_tenants.utils import get_tenant_model, schema_context

    TenantModel = get_tenant_model()
    today       = timezone.localdate()  # дата по TIME_ZONE Django (Europe/Moscow)
    created_total = 0
    skipped_total = 0

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        with schema_context(tenant.schema_name):
            from apps.tenant.branch.models import Branch, DailyCode, DailyCodePurpose
            branches  = Branch.objects.filter(is_active=True)
            purposes  = [p.value for p in DailyCodePurpose]
            for branch in branches:
                for purpose in purposes:
                    code = f'{random.randint(0, 99999):05d}'
                    _, created = DailyCode.objects.get_or_create(
                        branch=branch,
                        purpose=purpose,
                        valid_date=today,
                        defaults={'code': code},
                    )
                    if created:
                        created_total += 1
                    else:
                        skipped_total += 1

    logger.info(
        'generate_daily_codes: created=%d already_existed=%d date=%s',
        created_total, skipped_total, today,
    )
    return {'created': created_total, 'skipped': skipped_total, 'date': str(today)}


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


# ── VK membership catchup via Long Poll ───────────────────────────────────────

_MEMBERSHIP_EVENTS = frozenset({'group_join', 'group_leave', 'message_allow', 'message_deny'})


def longpoll_catchup_branch(branch_id: int) -> dict:
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
        vk_bulk_membership_sync_task.delay(branch_id)
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
                result = longpoll_catchup_branch(cfg.branch_id)
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
def vk_bulk_membership_sync_task(self, branch_id: int) -> dict:
    """
    Fallback: если Long Poll ts протух (пропуск > лимита VK), делаем полную
    синхронизацию статуса подписки через groups.isMember для всех гостей ветки.
    Повторяет логику management-команды sync_vk_status для одной точки.
    """
    from apps.tenant.senler.models import SenlerConfig
    from apps.tenant.branch.models import ClientBranch, ClientVKStatus
    from django.utils import timezone

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

    logger.info('VK bulk membership sync branch=%s: synced=%d', branch_id, synced)
    return {'synced': synced, 'errors': errors}
