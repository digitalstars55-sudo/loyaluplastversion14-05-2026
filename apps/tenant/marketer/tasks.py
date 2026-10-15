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
from apps.shared.clients.beat_guard import beat_tenants

logger = logging.getLogger(__name__)


@shared_task(bind=True, ignore_result=True)
def run_marketer_digest_task(self) -> dict:
    """Часовой диспетчер: находит тенантов, которым пора готовить дайджест."""
    from django_tenants.utils import get_tenant_model, schema_context

    from apps.tenant.marketer.models import MarketerSettings

    now_msk = timezone.localtime()
    summary = {'checked': 0, 'dispatched': [], 'errors': []}

    for tenant in beat_tenants():
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
def run_marketer_digest_for_tenant_task(self, schema_name: str, brief: str = '') -> dict:
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
        if not cfg or not cfg.is_enabled or (not cfg.digest_enabled and not brief):
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
                brief=brief,
            )
        except Exception as e:
            logger.exception('marketer digest generation failed for %s', schema_name)
            # Падение генерации (нет кредитов API, таймаут) должно быть ВИДНО
            # владельцу в админке, а не только в логе celery: 31.08 и 07.09
            # дайджесты молча пропали именно так. Запись со статусом FAILED
            # и текстом ошибки; публиковать её нельзя (текст пустой).
            try:
                MarketerPost.objects.create(
                    post_type=MarketerPostType.DIGEST,
                    status=MarketerPostStatus.FAILED,
                    text='',
                    context_snapshot=knowledge,
                    model_used='',
                    created_by='ai',
                    error=f'Генерация не удалась: {str(e)[:900]}',
                )
            except Exception:
                logger.exception('marketer: failed to record generation error for %s', schema_name)
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


@shared_task(bind=True, ignore_result=True)
def publish_due_marketer_posts_task(self) -> dict:
    """
    Волна 3: публикует запланированные посты, у которых настало время.

    Тикает раз в 5 минут. Запланировать = одобрить: autopost_enabled тут не нужен.
    Маркетолог выключен — пост остаётся запланированным (не теряется, не падает).
    Пост берётся под select_for_update и статус проверяется заново — два тика
    подряд не опубликуют его дважды.
    """
    from django.db import transaction
    from django_tenants.utils import schema_context

    from apps.tenant.marketer.models import MarketerPost, MarketerPostStatus, MarketerSettings
    from apps.tenant.marketer.publisher import publish_post

    summary = {'published': 0, 'failed': 0, 'errors': []}
    now = timezone.now()
    for tenant in beat_tenants():
        schema = tenant.schema_name
        try:
            with schema_context(schema):
                cfg = MarketerSettings.objects.first()
                if not cfg or not cfg.is_enabled:
                    continue
                due = list(MarketerPost.objects.filter(
                    status=MarketerPostStatus.SCHEDULED, scheduled_at__lte=now,
                ).order_by('scheduled_at').values_list('pk', flat=True)[:20])
                for pk in due:
                    with transaction.atomic():
                        post = MarketerPost.objects.select_for_update().filter(
                            pk=pk, status=MarketerPostStatus.SCHEDULED).first()
                        if post is None:
                            continue
                        ok = publish_post(post)
                    summary['published' if ok else 'failed'] += 1
        except Exception as e:
            logger.exception('marketer scheduled publish failed for %s', schema)
            summary['errors'].append(f'{schema}: {e}')
    if summary['published'] or summary['failed'] or summary['errors']:
        logger.info('marketer scheduled publish: %s', summary)
    return summary


PLAN_TOPICS = (
    ('insight', 'Что любят наши гости', 'Пост о том, что гостям нравится больше всего: блюда, точки, '
     'добрые отзывы. Тепло и по-соседски.'),
    ('promo', 'Подарки в приложении', 'Пост-напоминание: в приложении сети ждут подарки и бонусы, '
     'как их получить. Только то, что есть в данных.'),
    ('digest', 'Что нового за неделю', None),
    ('insight', 'Наша команда', 'Пост о людях и точках сети, о том, как нас оценивают гости, '
     'без имён гостей.'),
)


@shared_task(bind=True, ignore_result=True)
def build_content_plan_task(self, schema_name: str, slots: list[str], author: str = '') -> dict:
    """
    Контент-план: по одному черновику на каждый слот (ISO-время) с темой по кругу.
    Черновики НЕ планируются сами — человек проверяет и жмёт «Запланировать».
    """
    from datetime import datetime

    from django_tenants.utils import schema_context

    from apps.tenant.marketer.generator import generate_post, _DIGEST_TASK
    from apps.tenant.marketer.knowledge import collect_knowledge
    from apps.tenant.marketer.models import MarketerPost, MarketerPostStatus, MarketerSettings

    created, failed = [], 0
    with schema_context(schema_name):
        cfg = MarketerSettings.objects.first()
        if not cfg or not cfg.is_enabled:
            return {'schema': schema_name, 'skipped': 'disabled'}
        knowledge = collect_knowledge()
        for i, slot in enumerate(slots):
            post_type, topic, task = PLAN_TOPICS[i % len(PLAN_TOPICS)]
            when = datetime.fromisoformat(slot)
            try:
                text, model = generate_post(knowledge, task=task or _DIGEST_TASK,
                                            brand_voice=cfg.brand_voice, extra_facts=cfg.extra_facts)
                status, error = MarketerPostStatus.DRAFT, ''
            except Exception as e:
                text, model, status, error = '', '', MarketerPostStatus.FAILED, f'Генерация не удалась: {str(e)[:900]}'
                failed += 1
            post = MarketerPost.objects.create(
                post_type=post_type, status=status, text=text, context_snapshot=knowledge,
                model_used=model, created_by=author or 'ai', error=error,
                topic=topic, scheduled_at=when,
            )
            created.append(post.pk)
    logger.info('marketer content plan %s: %s posts, %s failed', schema_name, len(created), failed)
    return {'schema': schema_name, 'created': created, 'failed': failed}


@shared_task(bind=True, ignore_result=True)
def generate_post_image_task(self, schema_name: str, post_id: int) -> dict:
    """Волна 4: картинка к посту в фоне (ИИ рисует до минуты)."""
    from django_tenants.utils import schema_context

    from apps.tenant.marketer.images import ImageError, generate_for_post
    from apps.tenant.marketer.models import MarketerPost

    with schema_context(schema_name):
        post = MarketerPost.objects.filter(pk=post_id).first()
        if post is None:
            return {'skipped': 'no_post'}
        try:
            generate_for_post(post)
        except (ImageError, Exception) as e:
            post.error = f'Картинка не получилась: {str(e)[:500]}'
            post.save(update_fields=['error', 'updated_at'])
            logger.warning('marketer image failed %s/%s: %s', schema_name, post_id, e)
            return {'error': str(e)}
    return {'ok': True}
