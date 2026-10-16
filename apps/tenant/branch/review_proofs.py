"""
Очередь скриншотов отзывов на картах (волна 3 ТЗ маркетолога, 14.10.2026).

Чистые действия без HTTP: прислать (гость), подтвердить / отклонить (менеджер).
Начисление — одно, под блокировкой строки: двойной клик или два менеджера сразу
второй раз баллы не дадут.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import ClientBranch, CoinTransaction, ReviewProof, TransactionSource, TransactionType

MAX_IMAGE_BYTES = 10 * 1024 * 1024


class ProofError(Exception):
    def __init__(self, code: str, detail: str, http_status: int = 400):
        super().__init__(detail)
        self.code, self.detail, self.http_status = code, detail, http_status


def submit(client, branch_public_id, platform: str, image):
    """Гость прислал скриншот. Возвращает ReviewProof (PENDING)."""
    if platform not in ReviewProof.Platform.values:
        raise ProofError('invalid_payload', 'platform: yandex | 2gis')
    if image is None:
        raise ProofError('invalid_payload', 'image: скриншот отзыва')
    if image.size > MAX_IMAGE_BYTES:
        raise ProofError('invalid_payload', 'Скриншот больше 10 МБ')
    if (getattr(image, 'content_type', '') or '').split('/')[0] != 'image':
        raise ProofError('invalid_payload', 'Нужна картинка (скриншот)')
    cb = ClientBranch.objects.filter(client=client, branch__branch_id=branch_public_id).first()
    if cb is None:
        raise ProofError('not_found', 'Сначала зайдите в эту точку в приложении', 404)
    since = timezone.now() - timedelta(days=ReviewProof.COOLDOWN_DAYS)
    if ReviewProof.objects.filter(client=cb, platform=platform, created_at__gte=since).exclude(
            status=ReviewProof.Status.REJECTED).exists():
        raise ProofError('already_sent', 'Скриншот с этой площадки уже прислан — дождитесь проверки', 409)
    return ReviewProof.objects.create(client=cb, platform=platform, image=image)


def approve(proof_id: int, coins: int, who: str):
    """Подтвердить: баллы один раз. Повтор по уже подтверждённому — без второго начисления."""
    with transaction.atomic():
        proof = ReviewProof.objects.select_for_update().select_related('client').filter(pk=proof_id).first()
        if proof is None:
            raise ProofError('not_found', 'Скриншот не найден', 404)
        if proof.status == ReviewProof.Status.APPROVED:
            return proof, False
        if proof.status != ReviewProof.Status.PENDING:
            raise ProofError('not_pending', 'Скриншот уже отклонён', 409)
        if coins > 0:
            proof.transaction = CoinTransaction.objects.create_transfer(
                proof.client, coins, TransactionType.INCOME, TransactionSource.REVIEW,
                description=f'Отзыв на {proof.get_platform_display()} · скриншот {proof.pk}',
            )
        proof.coins = max(coins, 0)
        proof.status = ReviewProof.Status.APPROVED
        proof.reviewed_by, proof.reviewed_at = who[:150], timezone.now()
        proof.save(update_fields=['transaction', 'coins', 'status', 'reviewed_by', 'reviewed_at'])
    # Сообщение — только после успешного начисления (транзакция уже закрыта).
    notify_guest(proof)
    return proof, True


def reject(proof_id: int, reason: str, who: str, *, notify: bool = True):
    with transaction.atomic():
        proof = ReviewProof.objects.select_for_update().filter(pk=proof_id).first()
        if proof is None:
            raise ProofError('not_found', 'Скриншот не найден', 404)
        if proof.status != ReviewProof.Status.PENDING:
            raise ProofError('not_pending', f'Скриншот уже «{proof.get_status_display()}»', 409)
        # Причина — для истории; гостю уходит текст из настроек сети (ТЗ 15.10.2026).
        reason = (reason or '').strip()
        proof.status = ReviewProof.Status.REJECTED
        proof.reject_reason = reason[:255]
        proof.reviewed_by, proof.reviewed_at = who[:150], timezone.now()
        proof.save(update_fields=['status', 'reject_reason', 'reviewed_by', 'reviewed_at'])
    if notify:
        notify_guest(proof)
    return proof


# ── Сообщения гостю и скриншоты из диалога ВК (ТЗ 15.10.2026) ─────────────────

def reward(cfg=None) -> int:
    """Вознаграждение сети: настройка отзывов, иначе прежняя настройка платформы."""
    from .models import ReviewAutoReplyConfig
    свои = ReviewAutoReplyConfig.get_singleton().proof_coins
    if свои is not None:
        return int(свои)
    if cfg is None:
        from django.db import connection
        cfg = getattr(getattr(connection, 'tenant', None), 'config', None)
    return int(getattr(cfg, 'review_proof_coins', 0) or 0)


def fill(text: str, coins: int) -> str:
    return (text or '').replace('{вознаграждение}', str(coins))


def notify_guest(proof) -> str:
    """Сообщение гостю по решению. Один раз: отправленное повторно не шлём.

    → 'sent' | 'failed' | 'skipped'. Ошибку сохраняем — «Повторить сообщение»
    шлёт ТОЛЬКО сообщение, баллы второй раз не начисляются.
    """
    from apps.tenant.senler.models import SenlerConfig
    from apps.tenant.senler.services import send_vk_message
    from .models import ReviewAutoReplyConfig

    if proof.message_status == 'sent':
        return 'sent'
    cfg = ReviewAutoReplyConfig.get_singleton()
    if proof.status == ReviewProof.Status.APPROVED:
        текст = fill(cfg.proof_approved_text, proof.coins)
    elif proof.status == ReviewProof.Status.REJECTED:
        текст = fill(cfg.proof_rejected_text, reward())
    else:
        return ''
    vk_id = getattr(getattr(proof.client, 'client', None), 'vk_id', None)
    итог, ошибка = 'skipped', ''
    if текст.strip() and vk_id:
        conf = (SenlerConfig.objects.filter(branch_id=proof.client.branch_id).first()
                or SenlerConfig.objects.filter(is_active=True).order_by('branch_id').first())
        if conf is None or not conf.vk_community_token:
            итог, ошибка = 'failed', 'ВК сообщества не настроен'
        else:
            try:
                ok, err, _mid = send_vk_message(conf, int(vk_id), текст.strip())
                итог, ошибка = ('sent', '') if ok else ('failed', str(err or 'ВК отказал'))
            except Exception as exc:                     # noqa: BLE001
                итог, ошибка = 'failed', str(exc)
    proof.message_status, proof.message_error = итог, ошибка[:255]
    proof.save(update_fields=['message_status', 'message_error'])
    return итог


def resend(proof_id: int) -> str:
    proof = ReviewProof.objects.select_related('client__client').filter(pk=proof_id).first()
    if proof is None:
        raise ProofError('not_found', 'Скриншот не найден', 404)
    if proof.status == ReviewProof.Status.PENDING:
        raise ProofError('not_decided', 'Сначала подтвердите или отклоните скриншот', 409)
    return notify_guest(proof)


LINKS_WINDOW_DAYS = 14


def from_vk_photo(message_pk: int) -> ReviewProof | None:
    """Скриншот ответом в диалоге ВК → заявка «На проверке» (если ссылки на карты слали).

    Ждём фото только от того, кому в последние 14 дней уходили кнопки карт (наш
    автоответ или ручной ответ со ссылками). Одно сообщение — одна заявка.
    """
    import os
    import urllib.request
    from django.core.files.base import ContentFile
    from .models import ClientBranch, ReviewLinkClick, TestimonialMessage

    msg = TestimonialMessage.objects.select_related('conversation').filter(pk=message_pk).first()
    if msg is None or not msg.attachments or not msg.vk_message_id:
        return None
    if ReviewProof.objects.filter(vk_message_id=msg.vk_message_id).exists():
        return None
    conv = msg.conversation
    since = timezone.now() - timedelta(days=LINKS_WINDOW_DAYS)
    from .models import ReviewAutoReplyConfig
    хвост = (ReviewAutoReplyConfig.get_singleton().auto_send_links_text or '').strip()[:40]
    слали = ReviewLinkClick.objects.filter(conversation=conv, created_at__gte=since).exists() or (
        хвост and TestimonialMessage.objects.filter(
            conversation=conv, source=TestimonialMessage.Source.ADMIN_REPLY,
            created_at__gte=since, text__contains=хвост).exists())
    if not слали:
        return None
    guest = conv.vk_guest or (conv.client.client if conv.client_id else None)
    if guest is None and conv.vk_sender_id:
        from apps.shared.guest.models import Client as Guest
        guest = Guest.objects.filter(vk_id=conv.vk_sender_id).first()
    if guest is None:
        return None
    branch_id = conv.branch_id or getattr(conv, 'inferred_branch_id', None)
    cb = (ClientBranch.objects.filter(client=guest, branch_id=branch_id).first() if branch_id else None) \
        or ClientBranch.objects.filter(client=guest).order_by('-updated_at').first()
    if cb is None:
        return None
    att = msg.attachments[0] or {}
    данные = None
    from django.conf import settings
    локальный = os.path.join(settings.MEDIA_ROOT, att['file']) if att.get('file') else ''
    if локальный and os.path.exists(локальный):
        with open(локальный, 'rb') as fh:
            данные = fh.read()
    elif att.get('src'):
        with urllib.request.urlopen(att['src'], timeout=15) as r:   # noqa: S310 — адрес VK из callback
            данные = r.read(MAX_IMAGE_BYTES + 1)
    if not данные or len(данные) > MAX_IMAGE_BYTES:
        return None
    proof = ReviewProof(client=cb, platform='', source='vk', vk_message_id=msg.vk_message_id)
    proof.image.save(f'vk_{msg.vk_message_id}.jpg', ContentFile(данные), save=False)
    proof.save()
    return proof
