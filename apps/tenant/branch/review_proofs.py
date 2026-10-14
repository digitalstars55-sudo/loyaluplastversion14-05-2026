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
        return proof, True


def reject(proof_id: int, reason: str, who: str):
    with transaction.atomic():
        proof = ReviewProof.objects.select_for_update().filter(pk=proof_id).first()
        if proof is None:
            raise ProofError('not_found', 'Скриншот не найден', 404)
        if proof.status != ReviewProof.Status.PENDING:
            raise ProofError('not_pending', f'Скриншот уже «{proof.get_status_display()}»', 409)
        reason = (reason or '').strip()
        if not reason:
            raise ProofError('invalid_payload', 'reason: причина — гость её увидит')
        proof.status = ReviewProof.Status.REJECTED
        proof.reject_reason = reason[:255]
        proof.reviewed_by, proof.reviewed_at = who[:150], timezone.now()
        proof.save(update_fields=['status', 'reject_reason', 'reviewed_by', 'reviewed_at'])
        return proof
