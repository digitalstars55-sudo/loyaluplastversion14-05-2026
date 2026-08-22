"""
RFM-кампании: snapshot аудитории, массовое начисление, отмена.

Реализует серверную часть ТЗ «RFM-награды» (§6, §9):

  • состав получателей фиксируется в момент создания кампании и не меняется;
  • начисление идёт асинхронно батчами, идемпотентно по (campaign, client) —
    обрабатываются только PENDING-строки, ретрай не начисляет второй раз;
  • подарок = InventoryItem с claim_expires_at (сгорание ленивое, как у сториз),
    место в лимите позиции каталога занимает reward_catalog.issue_to_guest;
  • баллы = CoinTransaction (source=rfm) на «домашнюю точку» гостя —
    общего баланса в системе нет, баллы пер-точечные;
  • контрольная группа holdout_percent помечается при snapshot и не получает
    ничего — только так метрика «вернулись» отделима от естественного возврата;
  • отмена: неактивированные подарки отзываются с возвратом лимита,
    баллы откатываются в пределах неиспользованного остатка.

Сообщение гостям кампания НЕ отправляет — рассылка делается отдельно
существующим каналом send-broadcast по snapshot кампании.
"""

import logging
import random

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from apps.tenant.analytics.models import (
    RFMCampaign,
    RFMCampaignMember,
    RFMCampaignStatus,
    RFMMemberStatus,
    RFMRewardType,
)

logger = logging.getLogger(__name__)

# Сколько получателей обрабатываем между проверками «кампанию не отменили?».
CANCEL_CHECK_EVERY = 25


def build_campaign_name(*, segment_label, reward_label) -> str:
    today = timezone.localdate().strftime('%d.%m.%Y')
    cell = segment_label or 'RF-ячейка'
    return f'RFM / {cell} / {reward_label} / {today}'


def create_campaign_snapshot(
    *,
    client_ids,
    mode,
    segment=None,
    segment_label='',
    r_score=None,
    f_score=None,
    branch_ids=None,
    period_start=None,
    period_end=None,
    reward_type,
    catalog_item=None,
    points_amount=0,
    lifetime_days=0,
    holdout_percent=10,
    name='',
    comment='',
    created_by='',
) -> RFMCampaign:
    """
    Создаёт кампанию и фиксирует snapshot аудитории одним куском.

    client_ids — id гостей (guest.Client) из резолвера RF-ячейки. Начисление
    здесь НЕ выполняется — только фиксация; запускать run_rfm_campaign_task.
    """
    client_ids = list(dict.fromkeys(client_ids))  # стабильный порядок, без дублей

    if reward_type == RFMRewardType.POINTS:
        reward_label = f'{points_amount} баллов'
    else:
        reward_label = catalog_item.display_name if catalog_item else 'Подарок'

    holdout_percent = max(0, min(int(holdout_percent or 0), 50))
    control_n = round(len(client_ids) * holdout_percent / 100)
    # Контрольная группа осмысленна только если обе группы непусты.
    if control_n >= len(client_ids):
        control_n = 0
    control_ids = set(random.sample(client_ids, control_n)) if control_n else set()

    with transaction.atomic():
        campaign = RFMCampaign.objects.create(
            name=name or build_campaign_name(
                segment_label=segment_label, reward_label=reward_label,
            ),
            comment=comment,
            created_by=created_by,
            mode=mode,
            segment=segment,
            segment_label=segment_label,
            r_score=r_score,
            f_score=f_score,
            branch_ids=list(branch_ids or []),
            period_start=period_start,
            period_end=period_end,
            reward_type=reward_type,
            catalog_item=catalog_item,
            points_amount=points_amount or 0,
            lifetime_days=lifetime_days or 0,
            holdout_percent=holdout_percent,
            audience_total=len(client_ids),
            control_count=len(control_ids),
        )
        RFMCampaignMember.objects.bulk_create(
            [
                RFMCampaignMember(
                    campaign=campaign,
                    client_id=cid,
                    is_control=(cid in control_ids),
                    status=(
                        RFMMemberStatus.CONTROL
                        if cid in control_ids
                        else RFMMemberStatus.PENDING
                    ),
                )
                for cid in client_ids
            ],
            batch_size=500,
        )
    return campaign


def _resolve_home_cb(client_id, branch_ids):
    """
    «Домашняя точка» гостя: профиль ClientBranch с последним визитом
    (сначала внутри точек кампании, затем по всей сети), фолбэк — самый
    свежий профиль. None — у гостя нет ни одного профиля не-сотрудника.
    """
    from apps.tenant.branch.models import ClientBranch, ClientBranchVisit

    def _latest_visit_cb(scope_branch_ids):
        qs = ClientBranchVisit.objects.filter(
            client__client_id=client_id,
            client__is_employee=False,
        )
        if scope_branch_ids:
            qs = qs.filter(client__branch_id__in=scope_branch_ids)
        cb_id = qs.order_by('-visited_at').values_list('client_id', flat=True).first()
        return ClientBranch.objects.filter(pk=cb_id).first() if cb_id else None

    cb = _latest_visit_cb(branch_ids) or _latest_visit_cb(None)
    if cb:
        return cb

    qs = ClientBranch.objects.filter(client_id=client_id, is_employee=False)
    if branch_ids:
        scoped = qs.filter(branch_id__in=branch_ids).order_by('-pk').first()
        if scoped:
            return scoped
    return qs.order_by('-pk').first()


def _has_active_same_gift(client_id, product_id, now):
    """У гостя уже есть такой же несгоревший подарок (защита от дублей, ТЗ §9)."""
    from apps.tenant.inventory.models import InventoryItem

    return (
        InventoryItem.objects
        .filter(
            client_branch__client_id=client_id,
            product_id=product_id,
            used_at__isnull=True,
        )
        .filter(
            # ждёт активации и срок забора не вышел…
            Q(
                activated_at__isnull=True,
            ) & (Q(claim_expires_at__isnull=True) | Q(claim_expires_at__gt=now))
            # …или активирован и окно ещё действует
            | Q(activated_at__isnull=False)
            & (Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        )
        .exists()
    )


def process_campaign(campaign_id) -> dict:
    """
    Обрабатывает PENDING-получателей кампании. Возвращает сводку.
    Безопасно вызывать повторно (продолжение после celery-таймаута).

    SoftTimeLimitExceeded НЕ ловим — пробрасывается в таск, который ставит
    продолжение; прогресс сохранён построчно.
    """
    from apps.tenant.branch.models import (
        CoinTransaction, TransactionSource, TransactionType,
    )
    from apps.tenant.inventory.models import AcquisitionSource
    from apps.tenant.inventory import reward_catalog

    campaign = RFMCampaign.objects.select_related('catalog_item__product').get(pk=campaign_id)
    if campaign.status == RFMCampaignStatus.CANCELLED:
        return {'campaign': campaign_id, 'skipped': 'cancelled'}

    if not campaign.started_at:
        RFMCampaign.objects.filter(pk=campaign.pk, started_at__isnull=True).update(
            started_at=timezone.now(),
        )

    is_gift = campaign.reward_type == RFMRewardType.GIFT
    item = campaign.catalog_item if is_gift else None
    now = timezone.now()
    done = 0

    pending_ids = list(
        campaign.members.filter(status=RFMMemberStatus.PENDING)
        .order_by('pk')
        .values_list('pk', flat=True)
    )

    try:
        from celery.exceptions import SoftTimeLimitExceeded
    except Exception:  # pragma: no cover
        class SoftTimeLimitExceeded(BaseException):
            pass

    for member_pk in pending_ids:
        if done and done % CANCEL_CHECK_EVERY == 0:
            live = RFMCampaign.objects.filter(pk=campaign.pk).values_list('status', flat=True).first()
            if live == RFMCampaignStatus.CANCELLED:
                break

        try:
            # Всё начисление одного получателя — в собственной транзакции:
            # ошибка портит только её, соседей и пометку FAILED не задевает.
            with transaction.atomic():
                member = (
                    RFMCampaignMember.objects.select_for_update()
                    .get(pk=member_pk)
                )
                if member.status != RFMMemberStatus.PENDING:
                    done += 1
                    continue

                cb = _resolve_home_cb(member.client_id, campaign.branch_ids)
                if cb is None:
                    member.status = RFMMemberStatus.SKIPPED
                    member.reason = 'no_branch'
                    member.save(update_fields=['status', 'reason'])
                    done += 1
                    continue

                if is_gift:
                    if item is None or not item.product_id:
                        member.status = RFMMemberStatus.FAILED
                        member.reason = 'catalog_item_missing'
                        member.save(update_fields=['status', 'reason'])
                        done += 1
                        continue
                    if _has_active_same_gift(member.client_id, item.product_id, now):
                        member.status = RFMMemberStatus.SKIPPED
                        member.reason = 'duplicate_active_gift'
                        member.save(update_fields=['status', 'reason'])
                        done += 1
                        continue
                    issued = reward_catalog.issue_to_guest(
                        item,
                        cb,
                        source=AcquisitionSource.RFM,
                        lifetime_days=campaign.lifetime_days,
                        description=f'RFM-кампания «{campaign.name}» #{campaign.pk}',
                    )
                    if issued is None:
                        member.status = RFMMemberStatus.FAILED
                        member.reason = 'limit_exhausted'
                        member.save(update_fields=['status', 'reason'])
                        done += 1
                        continue
                    member.inventory_item = issued
                else:
                    tx = CoinTransaction.objects.create_transfer(
                        cb,
                        campaign.points_amount,
                        TransactionType.INCOME,
                        TransactionSource.RFM,
                        description=f'RFM-кампания «{campaign.name}» #{campaign.pk}',
                    )
                    member.coin_tx = tx

                member.client_branch = cb
                member.status = RFMMemberStatus.ASSIGNED
                member.assigned_at = timezone.now()
                member.save(update_fields=[
                    'client_branch', 'status', 'assigned_at',
                    'inventory_item', 'coin_tx',
                ])
        except SoftTimeLimitExceeded:
            # ⚠️ ловить ДО Exception: иначе celery-таймаут превратился бы в
            # FAILED-получателя, а таск не поставил бы продолжение.
            raise
        except Exception as exc:  # noqa: BLE001 — одна ошибка не валит кампанию
            logger.exception(
                'RFM campaign %s: member %s failed', campaign.pk, member_pk,
            )
            # Транзакция получателя откатилась целиком — пометку FAILED пишем
            # отдельной операцией уже вне сломанного atomic.
            RFMCampaignMember.objects.filter(
                pk=member_pk, status=RFMMemberStatus.PENDING,
            ).update(
                status=RFMMemberStatus.FAILED,
                reason=f'error:{exc}'[:64],
            )

        done += 1

    return finalize_campaign(campaign.pk)


def finalize_campaign(campaign_id) -> dict:
    """Пересчитывает счётчики; закрывает кампанию, когда PENDING не осталось."""
    campaign = RFMCampaign.objects.get(pk=campaign_id)
    counts = {
        row['status']: row['n']
        for row in campaign.members.values('status').annotate(n=Count('pk'))
    }
    campaign.assigned_count = counts.get(RFMMemberStatus.ASSIGNED, 0)
    campaign.skipped_count = counts.get(RFMMemberStatus.SKIPPED, 0)
    campaign.failed_count = counts.get(RFMMemberStatus.FAILED, 0)
    campaign.control_count = counts.get(RFMMemberStatus.CONTROL, 0)
    update_fields = ['assigned_count', 'skipped_count', 'failed_count', 'control_count']

    pending_left = counts.get(RFMMemberStatus.PENDING, 0)
    if campaign.status == RFMCampaignStatus.PROCESSING and not pending_left:
        campaign.status = (
            RFMCampaignStatus.PARTIALLY_FAILED
            if campaign.failed_count
            else RFMCampaignStatus.COMPLETED
        )
        campaign.finished_at = timezone.now()
        update_fields += ['status', 'finished_at']
    campaign.save(update_fields=update_fields)

    return {
        'campaign': campaign.pk,
        'status': campaign.status,
        'assigned': campaign.assigned_count,
        'skipped': campaign.skipped_count,
        'failed': campaign.failed_count,
        'control': campaign.control_count,
        'pending': pending_left,
    }


def cancel_campaign(campaign_id, *, actor='') -> dict:
    """
    Отмена кампании (ТЗ §9 «Отмена ошибки»):

      • PENDING → CANCELLED (начисление останавливается);
      • подарок не активирован → отзыв (revoke_issued_item: запись удаляется,
        место в лимите позиции возвращается);
      • подарок уже активирован/использован → остаётся у гостя (не отзываем);
      • баллы → откат в пределах неиспользованного остатка гостя.

    Повторный вызов безопасен: обрабатываются только ASSIGNED/PENDING-строки.
    """
    campaign = RFMCampaign.objects.get(pk=campaign_id)
    RFMCampaign.objects.filter(pk=campaign.pk).update(status=RFMCampaignStatus.CANCELLED)

    revoked = refunded = kept = 0

    campaign.members.filter(status=RFMMemberStatus.PENDING).update(
        status=RFMMemberStatus.CANCELLED, reason='campaign_cancelled',
    )

    assigned = campaign.members.filter(status=RFMMemberStatus.ASSIGNED).select_related(
        'inventory_item', 'client_branch',
    )
    for member in assigned:
        try:
            _cancel_member(campaign, member, actor=actor)
        except Exception:  # noqa: BLE001 — один сбой не валит отмену остальных
            logger.exception(
                'RFM campaign %s cancel: member %s failed', campaign.pk, member.pk,
            )
            kept += 1
            continue
        if member.status == RFMMemberStatus.CANCELLED:
            if campaign.reward_type == RFMRewardType.GIFT:
                revoked += 1
            else:
                refunded += 1
        else:
            kept += 1

    logger.info(
        'RFM campaign %s cancelled by %s: revoked=%s refunded=%s kept=%s',
        campaign.pk, actor or '?', revoked, refunded, kept,
    )
    summary = finalize_campaign(campaign.pk)
    summary.update({'revoked': revoked, 'refunded': refunded, 'kept': kept})
    return summary


def _cancel_member(campaign, member, *, actor=''):
    """Откат одного получателя. Статус CANCELLED ставится только при успехе."""
    from apps.tenant.branch.models import (
        CoinTransaction, TransactionSource, TransactionType,
    )
    from apps.tenant.inventory import reward_catalog

    with transaction.atomic():
        if campaign.reward_type == RFMRewardType.GIFT:
            # Уже активирован/использован — не отзываем, остаётся у гостя.
            if member.inventory_item and reward_catalog.revoke_issued_item(member.inventory_item):
                member.status = RFMMemberStatus.CANCELLED
                member.reason = 'revoked'
                member.inventory_item = None
                member.save(update_fields=['status', 'reason', 'inventory_item'])
        else:
            cb = member.client_branch
            if cb is None:
                return
            refund = min(int(cb.coins_balance or 0), campaign.points_amount)
            if refund > 0:
                CoinTransaction.objects.create_transfer(
                    cb,
                    refund,
                    TransactionType.EXPENSE,
                    TransactionSource.RFM,
                    description=(
                        f'Откат RFM-кампании «{campaign.name}» #{campaign.pk}'
                        + (f' ({actor})' if actor else '')
                    ),
                )
            member.status = RFMMemberStatus.CANCELLED
            member.reason = f'refund:{refund}'
            member.save(update_fields=['status', 'reason'])
