"""
Senler broadcast services.

run_broadcast() is synchronous for now — wrap in a Celery task when the
task queue is set up:

    @shared_task
    def run_broadcast_task(send_id: int) -> None:
        send = BroadcastSend.objects.get(pk=send_id)
        run_broadcast(send)
"""
import random
import time

from django.db import transaction
from django.utils import timezone

from apps.tenant.branch.models import ClientBranch

from .models import (
    AudienceType, Broadcast, BroadcastRecipient, BroadcastSend,
    GenderFilter, RecipientStatus, SendStatus, SenlerConfig,
)


# ── Audience resolution ───────────────────────────────────────────────────────

def resolve_recipients(broadcast: Broadcast):
    """
    Returns a QuerySet[ClientBranch] that should receive this broadcast.

    Filtering rules:
      ALL → all active, non-employee guests with a vk_id
            + gender_filter (AND, when not ALL)
            + rf_segments   (AND, OR within selected segments)
      SPECIFIC → exact specific_clients list, other filters ignored
    """
    base = (
        ClientBranch.objects
        .filter(
            branch=broadcast.branch,
            is_employee=False,
            client__is_active=True,
            client__vk_id__isnull=False,
        )
        .select_related('client')
    )

    if broadcast.audience_type == AudienceType.SPECIFIC:
        return base.filter(
            pk__in=broadcast.specific_clients.values_list('pk', flat=True)
        )

    # ALL type — apply stackable filters
    qs = base

    if broadcast.gender_filter != GenderFilter.ALL:
        qs = qs.filter(client__gender=broadcast.gender_filter)

    segments = broadcast.rf_segments.all()
    if segments.exists():
        # rf_score is the related_name from GuestRFScore.client OneToOneField
        qs = qs.filter(rf_score__segment__in=segments)

    return qs.distinct()


# ── VK API ────────────────────────────────────────────────────────────────────

def _vk_call(method: str, params: dict, timeout: int = 10) -> dict:
    """Makes a VK API call, returns the parsed JSON response."""
    import requests
    resp = requests.post(
        f'https://api.vk.com/method/{method}',
        data=params,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def upload_vk_photo(config: SenlerConfig, image_field) -> tuple[str | None, str]:
    """
    Uploads an image to VK for use as a message attachment.

    VK photo upload is a 3-step process:
      1. photos.getMessagesUploadServer → upload_url
      2. POST file to upload_url        → {server, photo, hash}
      3. photos.saveMessagesPhoto       → [{owner_id, id, ...}]

    Returns (attachment_str, error).
    attachment_str format: 'photo{owner_id}_{photo_id}'

    Upload once per broadcast, reuse the string for all recipients.
    """
    try:
        import requests
    except ImportError:
        return None, 'requests library not installed'

    base_params = {'access_token': config.vk_community_token, 'v': '5.131'}

    # Step 1: get upload server URL
    try:
        data = _vk_call('photos.getMessagesUploadServer', {**base_params, 'peer_id': 0})
        if 'error' in data:
            return None, data['error'].get('error_msg', 'getMessagesUploadServer failed')
        upload_url = data['response']['upload_url']
    except Exception as exc:
        return None, f'getMessagesUploadServer: {exc}'

    # Step 2: upload the file
    try:
        filename = image_field.name.rsplit('/', 1)[-1]
        with image_field.open('rb') as fh:
            up = requests.post(upload_url, files={'photo': (filename, fh)}, timeout=30)
        up_data = up.json()   # {server, photo, hash}
    except Exception as exc:
        return None, f'photo upload: {exc}'

    # Step 3: save and get the attachment string
    try:
        data = _vk_call('photos.saveMessagesPhoto', {
            **base_params,
            'server': up_data['server'],
            'photo':  up_data['photo'],
            'hash':   up_data['hash'],
        })
        if 'error' in data:
            return None, data['error'].get('error_msg', 'saveMessagesPhoto failed')
        photo = data['response'][0]
        return f"photo{photo['owner_id']}_{photo['id']}", ''
    except Exception as exc:
        return None, f'saveMessagesPhoto: {exc}'


def send_vk_message(
    config: SenlerConfig,
    vk_user_id: int,
    message: str,
    attachment: str | None = None,
) -> tuple[bool, str, int | None]:
    """
    Sends a message from the VK community to a user via messages.send.

    Returns (success: bool, error: str, vk_message_id: int | None).
    Requires the user to have started a dialog with the community or
    subscribed to community messages.

    VK API rate limit: ≤ 20 messages/second (handle in caller via throttling).
    """
    try:
        import requests
    except ImportError:
        return False, 'requests library not installed', None

    payload: dict = {
        'user_id':      vk_user_id,
        'message':      message,
        'random_id':    random.randint(1, 2 ** 31),
        'access_token': config.vk_community_token,
        'v':            '5.131',
    }
    if attachment:
        payload['attachment'] = attachment

    try:
        resp = requests.post(
            'https://api.vk.com/method/messages.send',
            data=payload,
            timeout=10,
        )
        data = resp.json()
        if 'error' in data:
            return False, data['error'].get('error_msg', 'VK API error'), None
        # VK returns message_id as the response value
        vk_message_id = data.get('response')
        return True, '', vk_message_id
    except Exception as exc:
        return False, str(exc), None


# ── Broadcast runner ──────────────────────────────────────────────────────────

@transaction.atomic
def create_send(
    broadcast: Broadcast,
    triggered_by: str = '',
    trigger_type: str = 'manual',
) -> BroadcastSend:
    """Creates a new BroadcastSend record in PENDING state."""
    return BroadcastSend.objects.create(
        broadcast=broadcast,
        trigger_type=trigger_type,
        triggered_by=triggered_by,
    )


def run_broadcast(send: BroadcastSend) -> None:
    """
    Resolves recipients, sends VK messages, records results.

    Intended to be called from:
      - Admin "Send Now" action (synchronous, blocks the request)
      - Celery task (run_broadcast_task) — preferred for production

    Updates BroadcastSend.sent_count, failed_count, skipped_count in real time.
    Updates RFSegment.last_campaign_date for each targeted segment.
    """
    # Mark as running
    send.status = SendStatus.RUNNING
    send.started_at = timezone.now()
    send.save(update_fields=['status', 'started_at'])

    # Require VK config
    try:
        config: SenlerConfig = send.broadcast.branch.senler_config
    except SenlerConfig.DoesNotExist:
        _fail(send, 'Не настроены параметры рассылки VK (SenlerConfig отсутствует).')
        return

    if not config.is_active:
        _fail(send, 'Рассылка VK отключена в настройках.')
        return

    # Resolve audience
    recipients_qs = resolve_recipients(send.broadcast)
    recipient_list = list(recipients_qs)
    send.recipients_count = len(recipient_list)
    send.save(update_fields=['recipients_count'])

    if not recipient_list:
        send.status = SendStatus.DONE
        send.finished_at = timezone.now()
        send.save(update_fields=['status', 'finished_at'])
        return

    # Bulk-create recipient records
    BroadcastRecipient.objects.bulk_create([
        BroadcastRecipient(
            send=send,
            client_branch=cb,
            vk_id=cb.client.vk_id,
        )
        for cb in recipient_list
    ])

    # Upload image once — reuse the attachment string for every recipient
    image_attachment: str | None = None
    if send.broadcast.image:
        attachment_str, upload_err = upload_vk_photo(config, send.broadcast.image)
        if attachment_str:
            image_attachment = attachment_str
        else:
            # Non-fatal: log the error and continue without the image
            send.error_message = f'Фото не загружено: {upload_err}. Отправляется без изображения.'
            send.save(update_fields=['error_message'])

    sent = failed = skipped = 0

    for recipient in BroadcastRecipient.objects.filter(send=send, status=RecipientStatus.PENDING):
        if not recipient.vk_id:
            recipient.status = RecipientStatus.SKIPPED
            recipient.save(update_fields=['status'])
            skipped += 1
            continue

        ok, error_msg, vk_msg_id = send_vk_message(
            config,
            recipient.vk_id,
            send.broadcast.message_text,
            image_attachment,
        )

        if ok:
            recipient.status  = RecipientStatus.SENT
            recipient.sent_at = timezone.now()
            recipient.vk_message_id = vk_msg_id
            recipient.save(update_fields=['status', 'sent_at', 'vk_message_id'])
            sent += 1
        elif _is_user_unreachable(error_msg):
            recipient.status = RecipientStatus.SKIPPED
            recipient.error  = error_msg[:512]
            recipient.save(update_fields=['status', 'error'])
            skipped += 1
        else:
            recipient.status = RecipientStatus.FAILED
            recipient.error  = error_msg[:512]
            recipient.save(update_fields=['status', 'error'])
            failed += 1

        # VK rate limit: ≤ 20 messages/second
        time.sleep(0.05)

    # Finalize
    send.sent_count    = sent
    send.failed_count  = failed
    send.skipped_count = skipped
    send.status        = SendStatus.DONE
    send.finished_at   = timezone.now()
    send.save(update_fields=['sent_count', 'failed_count', 'skipped_count', 'status', 'finished_at'])

    # Update RFSegment.last_campaign_date for targeted segments
    targeted_segments = send.broadcast.rf_segments.all()
    if targeted_segments.exists():
        targeted_segments.update(last_campaign_date=timezone.now())


_UNREACHABLE_VK_ERROR_FRAGMENTS = (
    "can't send messages for users without permission",
    'access to user denied',
    'user was deleted or banned',
    'user not found',
)


def _is_user_unreachable(error_msg: str) -> bool:
    if not error_msg:
        return False
    low = error_msg.lower()
    return any(frag in low for frag in _UNREACHABLE_VK_ERROR_FRAGMENTS)


def _fail(send: BroadcastSend, message: str) -> None:
    send.status        = SendStatus.FAILED
    send.error_message = message
    send.finished_at   = timezone.now()
    send.save(update_fields=['status', 'error_message', 'finished_at'])
