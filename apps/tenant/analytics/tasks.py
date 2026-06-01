"""
Celery tasks for analytics.

Schedule (defined in main/celery.py beat_schedule):
  — reclassify_waiting_reviews_task  — every 30 s, fans out per-conversation AI tasks
  — process_ai_review_task           — per-conversation AI classification (queued by above)
  — calculate_rf_all_tenants_task    — daily at 03:00, RF score recalc
"""
from __future__ import annotations

import logging

from celery import shared_task
from django_tenants.utils import get_tenant_model
from apps.tenant.analytics.pos_service import sync_get_guests_for_period

logger = logging.getLogger(__name__)


# ── Per-conversation AI classification ────────────────────────────────────────

@shared_task(
    name='apps.tenant.analytics.tasks.process_ai_review_task',
    bind=True,
    max_retries=2,
    default_retry_delay=120,
)
def process_ai_review_task(self, conversation_id: int, schema_name: str) -> dict:
    """
    Classify a single TestimonialConversation with AI and save sentiment + comment.
    Runs inside the correct tenant schema so ORM access is isolated.
    """
    from django_tenants.utils import schema_context
    from apps.tenant.analytics.ai_service import analyze_and_save
    from apps.tenant.branch.models import TestimonialConversation, TestimonialMessage

    try:
        with schema_context(schema_name):
            conv = TestimonialConversation.objects.get(pk=conversation_id)

            # Collect guest messages (exclude admin replies) oldest → newest
            messages = (
                conv.messages
                .exclude(source=TestimonialMessage.Source.ADMIN_REPLY)
                .order_by('created_at')
                .values_list('text', flat=True)
            )
            full_text = '\n---\n'.join(m for m in messages if m.strip())
            if not full_text:
                return {'skipped': True, 'reason': 'no_text'}

            # Determine source label from the first message
            first_source = (
                conv.messages
                .exclude(source=TestimonialMessage.Source.ADMIN_REPLY)
                .order_by('created_at')
                .values_list('source', flat=True)
                .first()
            ) or ''

            ok = analyze_and_save(conv.id, full_text, first_source)
            if ok:
                try:
                    auto_generate_draft_task.delay(conv.id, schema_name)
                except Exception:
                    logger.warning('auto_generate_draft_task dispatch failed', exc_info=True)
            return {'ok': ok, 'conversation_id': conversation_id}

    except TestimonialConversation.DoesNotExist:
        logger.warning('process_ai_review_task: conversation %s not found in %s', conversation_id, schema_name)
        return {'skipped': True, 'reason': 'not_found'}
    except Exception as exc:
        logger.exception('process_ai_review_task failed conv=%s schema=%s', conversation_id, schema_name)
        raise self.retry(exc=exc)


# ── Fan-out: reclassify all WAITING conversations ─────────────────────────────

@shared_task(name='apps.tenant.analytics.tasks.reclassify_waiting_reviews_task')
def reclassify_waiting_reviews_task() -> dict:
    """
    Scan all tenants for TestimonialConversations with sentiment=WAITING and
    dispatch a process_ai_review_task for each one.

    Runs every 30 s alongside poll_all_vk_messages_task so new messages get
    classified quickly after they arrive.
    """
    from django_tenants.utils import get_tenant_model, schema_context
    from apps.tenant.branch.models import TestimonialConversation

    TenantModel = get_tenant_model()
    dispatched  = 0

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        with schema_context(tenant.schema_name):
            from django.utils import timezone as _tz
            from datetime import timedelta as _td
            _cutoff = _tz.now() - _td(hours=6)
            _waiting = TestimonialConversation.objects.filter(
                sentiment=TestimonialConversation.Sentiment.WAITING
            )
            # LU-42: classify ONLY convs with a recent guest message. Historical
            # WAITING (dug up by reconcile/backfill -- sentiment defaults to WAITING)
            # must NOT be reprocessed: that re-flags has_unread, bumps
            # last_message_at and fires review_new/draft pushes for years-old msgs.
            _recent_ids = list(
                _waiting.filter(
                    messages__source__in=['VK_MESSAGE', 'APP'],
                    messages__created_at__gte=_cutoff,
                ).distinct().values_list('id', flat=True)
            )
            # Stale historical WAITING -> NEUTRAL so it leaves the queue silently.
            _n_stale = _waiting.exclude(id__in=_recent_ids).update(
                sentiment=TestimonialConversation.Sentiment.NEUTRAL
            )
            if _n_stale:
                logger.info('reclassify: %s stale WAITING->NEUTRAL in %s', _n_stale, tenant.schema_name)
            for conv_id in _recent_ids:
                process_ai_review_task.delay(conv_id, tenant.schema_name)
                dispatched += 1

    if dispatched:
        logger.info('reclassify_waiting_reviews: dispatched %d tasks', dispatched)

    return {'dispatched': dispatched}


@shared_task(
    name='apps.tenant.analytics.tasks.fetch_pos_data_all_tenants_task',
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def fetch_pos_data_all_tenants_task(self, date_str: str = None, day_offset: int = 1) -> dict:
    """
    Fetch POS guest counts for all tenants and cache in POSGuestCache.
    By default fetches yesterday's data (day_offset=1).
    Pass day_offset=0 to fetch today's data, or date_str='YYYY-MM-DD' for backfill.
    """
    from datetime import date, timedelta
    from django_tenants.utils import get_tenant_model, schema_context
    from apps.tenant.analytics.pos_service import sync_get_guests_for_period
    from apps.shared.config.models import POSType

    target_date = date.fromisoformat(date_str) if date_str else date.today() - timedelta(days=day_offset)
    TenantModel = get_tenant_model()
    summary = {'tenants': 0, 'branches': 0, 'errors': []}

    for tenant in TenantModel.objects.exclude(schema_name='public').select_related('config'):
        try:
            config = tenant.config
        except Exception:
            continue

        pos_type = getattr(config, 'pos_type', POSType.NONE)
        if pos_type == POSType.NONE:
            continue

        try:
            with schema_context(tenant.schema_name):
                from apps.tenant.branch.models import Branch
                from apps.tenant.analytics.models import POSGuestCache

                branches = list(Branch.objects.all())
                if not branches:
                    continue

                results = sync_get_guests_for_period(
                    config, target_date, target_date, branches=branches
                )

                for branch in branches:
                    if pos_type == POSType.IIKO:
                        pos_id = branch.iiko_organization_id or None
                    else:
                        pos_id = str(branch.dooglys_sale_point_id) if branch.dooglys_sale_point_id else None

                    count = results.get(pos_id, 0) if pos_id else 0
                    POSGuestCache.objects.update_or_create(
                        branch=branch,
                        date=target_date,
                        defaults={'guest_count': count},
                    )
                    summary['branches'] += 1

                summary['tenants'] += 1

        except Exception as exc:
            msg = f'[{tenant.schema_name}] {exc}'
            logger.exception('fetch_pos_data failed: %s', msg)
            summary['errors'].append(msg)
            try:
                raise self.retry(exc=exc)
            except Exception:
                pass

    return summary


@shared_task(
    name='apps.tenant.analytics.tasks.calculate_rf_all_tenants_task',
    bind=True,
    max_retries=2,
    default_retry_delay=300,
)
def calculate_rf_all_tenants_task(self) -> dict:
    """
    Recalculate RF scores (restaurant + delivery) for ALL active tenants.
    Uses django-tenants schema_context to isolate per-tenant data.
    """
    from django_tenants.utils import get_tenant_model, schema_context
    from apps.tenant.analytics.api.services import recalculate_rf_scores

    TenantModel = get_tenant_model()
    summary = {'tenants': 0, 'errors': []}

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                for mode in ('restaurant', 'delivery'):
                    result = recalculate_rf_scores(mode=mode)
                    logger.info(
                        'RF recalc schema=%s mode=%s → updated=%s created=%s migrations=%s',
                        tenant.schema_name, mode,
                        result.get('updated', 0),
                        result.get('created', 0),
                        result.get('migrations', 0),
                    )
            summary['tenants'] += 1
        except Exception as exc:
            msg = f'[{tenant.schema_name}] {exc}'
            logger.exception('RF recalc failed: %s', msg)
            summary['errors'].append(msg)

    if summary['errors']:
        logger.warning('RF recalc finished with errors: %s', summary['errors'])

    return summary


# ════════════════════════════════════════════════════════════════════
# AUTO-REPLY: AI draft generation + push notifications
# (added by hot-patch 2026-05-14)
# ════════════════════════════════════════════════════════════════════
@shared_task(name='apps.tenant.analytics.tasks.auto_generate_draft_task')
def auto_generate_draft_task(conversation_id: int, schema_name: str) -> dict:
    """
    После AI-классификации тональности:
    — генерит черновик ответа (если auto-reply включён и фильтры пройдены)
    — шлёт push 'draft_ready' админам тенанта
    Идемпотентен: если черновик уже есть/отвергнут/отвечен — skip.
    """
    from django_tenants.utils import schema_context, get_tenant_model
    from apps.tenant.analytics.auto_reply import (
        maybe_generate_auto_draft, push_draft_ready,
    )

    try:
        with schema_context(schema_name):
            text = maybe_generate_auto_draft(conversation_id)
        if not text:
            return {'skipped': True}

        TenantModel = get_tenant_model()
        tenant = TenantModel.objects.filter(schema_name=schema_name).first()
        tenant_name = tenant.name if tenant else schema_name

        push_result = push_draft_ready(schema_name, tenant_name, conversation_id)
        return {'ok': True, 'draft_len': len(text), 'push': push_result}
    except Exception as exc:
        logger.exception(
            'auto_generate_draft_task failed conv=%s schema=%s',
            conversation_id, schema_name,
        )
        return {'error': str(exc)}


@shared_task(name='apps.tenant.analytics.tasks.send_draft_reminders_task')
def send_draft_reminders_task() -> dict:
    """
    Beat-task (например каждые 30 мин): для каждого тенанта
    проверяет конверсации с черновиком, не ответом, и старше reminder_minutes —
    шлёт повторный push 'draft_ready'.
    """
    from django.utils import timezone
    from datetime import timedelta
    from django.db.models import Q
    from django_tenants.utils import get_tenant_model, schema_context
    from apps.tenant.analytics.auto_reply import push_draft_ready

    TenantModel = get_tenant_model()
    summary = {'tenants': 0, 'reminders_sent': 0}

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                from apps.tenant.branch.models import (
                    TestimonialConversation, ReviewAutoReplyConfig,
                )
                cfg = ReviewAutoReplyConfig.get_singleton()
                if not cfg.enabled:
                    continue
                reminder_min = int(cfg.reminder_minutes or 180)
                now = timezone.now()
                cutoff = now - timedelta(minutes=reminder_min)
                day_ago = now - timedelta(hours=24)

                # Неотвеченные отзывы с черновиком старше reminder_minutes,
                # которым НЕ напоминали последние 24ч (1 раз в сутки, без спама).
                qs = TestimonialConversation.objects.filter(
                    is_replied=False,
                    ai_draft_rejected=False,
                    updated_at__lt=cutoff,
                ).filter(
                    Q(last_reminded_at__isnull=True) | Q(last_reminded_at__lt=day_ago)
                ).exclude(ai_draft='').exclude(ai_draft__isnull=True)
                ids = list(qs.values_list('pk', flat=True)[:50])
                summary['tenants'] += 1
            for conv_id in ids:
                push_draft_ready(tenant.schema_name, tenant.name, conv_id)
                with schema_context(tenant.schema_name):
                    TestimonialConversation.objects.filter(pk=conv_id).update(last_reminded_at=timezone.now())
                summary['reminders_sent'] += 1
        except Exception as e:
            logger.warning(
                'send_draft_reminders_task failed for %s: %s',
                tenant.schema_name, e,
            )
    return summary
