"""
Celery-задачи AI-маркетолога.

Диспетчер тикает раз в час (beat) и раскидывает пер-тенантные задачи —
только тем, у кого маркетолог включён и настал час дайджеста. Дедуп по
last_digest_at (одна генерация в день). Всё выключено по умолчанию →
тик вхолостую, ничего не стоит.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(bind=True, ignore_result=True)
def run_marketer_digest_task(self) -> dict:
    """Часовой диспетчер: находит тенантов, которым пора готовить дайджест."""
    from django_tenants.utils import get_tenant_model, schema_context

    from apps.tenant.marketer.models import MarketerSettings

    now_msk = timezone.localtime()
    summary = {'checked': 0, 'dispatched': [], 'errors': []}

    for tenant in get_tenant_model().objects.exclude(schema_name='public'):
        schema = tenant.schema_name
        try:
            with schema_context(schema):
                cfg = MarketerSettings.objects.first()
                if not cfg or not cfg.is_enabled or not cfg.digest_enabled:
                    continue
                summary['checked'] += 1
                if cfg.digest_weekday != now_msk.weekday():
                    continue
                if cfg.digest_hour != now_msk.hour:
                    continue
                if cfg.last_digest_at and timezone.localtime(cfg.last_digest_at).date() == now_msk.date():
                    continue  # сегодня уже готовили
            run_marketer_digest_for_tenant_task.delay(schema)
            summary['dispatched'].append(schema)
        except Exception as e:
            logger.exception('marketer dispatcher failed for %s', schema)
            summary['errors'].append(f'{schema}: {e}')

    if summary['dispatched'] or summary['errors']:
        logger.info('marketer dispatcher: %s', summary)
    return summary


@shared_task(bind=True, ignore_result=True)
def run_marketer_digest_for_tenant_task(self, schema_name: str) -> dict:
    """Готовит дайджест «Что нового?» одному тенанту (черновик или автопост)."""
    from django_tenants.utils import schema_context

    from apps.tenant.marketer.generator import generate_post
    from apps.tenant.marketer.knowledge import collect_knowledge
    from apps.tenant.marketer.models import (
        MarketerPost, MarketerPostStatus, MarketerPostType, MarketerSettings,
    )
    from apps.tenant.marketer.publisher import publish_post

    with schema_context(schema_name):
        cfg = MarketerSettings.objects.first()
        if not cfg or not cfg.is_enabled or not cfg.digest_enabled:
            return {'schema': schema_name, 'skipped': 'disabled'}

        # Ставим отметку ДО генерации: упавшая генерация не должна
        # ретраиться каждый час до конца дня (повтор — руками из админки).
        cfg.last_digest_at = timezone.now()
        cfg.save(update_fields=['last_digest_at', 'updated_at'])

        knowledge = collect_knowledge()
        try:
            text, model = generate_post(
                knowledge,
                brand_voice=cfg.brand_voice,
                extra_facts=cfg.extra_facts,
            )
        except Exception as e:
            logger.exception('marketer digest generation failed for %s', schema_name)
            return {'schema': schema_name, 'error': str(e)}

        post = MarketerPost.objects.create(
            post_type=MarketerPostType.DIGEST,
            status=MarketerPostStatus.DRAFT,
            text=text,
            context_snapshot=knowledge,
            model_used=model,
            created_by='ai',
        )
        result = {'schema': schema_name, 'post_id': post.pk, 'published': False}

        if cfg.autopost_enabled:
            result['published'] = publish_post(post)

        logger.info('marketer digest for %s: %s', schema_name, result)
        return result
