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
def run_marketer_digest_for_tenant_task(self, schema_name: str, brief: str = '', job_id: int | None = None) -> dict:
    """Готовит дайджест «Что нового?» одному тенанту (черновик или автопост)."""
    from django_tenants.utils import schema_context

    from apps.tenant.marketer import progress
    from apps.tenant.marketer.generator import generate_post_ex
    from apps.tenant.marketer.knowledge import collect_knowledge
    from apps.tenant.marketer.models import (
        MarketerPost, MarketerPostStatus, MarketerPostType, MarketerSettings,
    )
    from apps.tenant.marketer.publisher import publish_post

    with schema_context(schema_name):
        cfg = MarketerSettings.objects.first()
        if not cfg or not cfg.is_enabled or (not cfg.digest_enabled and not brief):
            progress.finish(job_id, error='AI-маркетолог выключен в настройках')
            return {'schema': schema_name, 'skipped': 'disabled'}

        # Ставим отметку ДО генерации: упавшая генерация не должна
        # ретраиться каждый час до конца дня (повтор — руками из админки).
        cfg.last_digest_at = timezone.now()
        cfg.save(update_fields=['last_digest_at', 'updated_at'])

        progress.stage(job_id, 'kb')
        knowledge = collect_knowledge()
        progress.stage(job_id, 'write')
        try:
            out = generate_post_ex(
                knowledge,
                brand_voice=cfg.brand_voice,
                extra_facts=cfg.extra_facts,
                brief=brief,
            )
            text, model = out['text'], out['model']
        except Exception as e:
            progress.finish(job_id, error=f'Генерация не удалась: {str(e)[:500]}')
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

        progress.stage(job_id, 'save')
        post = MarketerPost.objects.create(
            post_type=MarketerPostType.DIGEST,
            status=MarketerPostStatus.DRAFT,
            text=text,
            visual=out.get('visual', ''),
            context_snapshot=knowledge,
            model_used=model,
            created_by='ai',
        )
        progress.finish(job_id, result={'post_ids': [post.pk]})
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


def occupied_days(posts) -> set:
    """Дни, на которые в плане уже стоит живой пост (черновик, запланирован, опубликован)."""
    from apps.tenant.marketer.models import MarketerPostStatus
    alive = {MarketerPostStatus.DRAFT, MarketerPostStatus.SCHEDULED, MarketerPostStatus.PUBLISHED}
    return {timezone.localtime(p.scheduled_at).date() for p in posts
            if p.scheduled_at and p.status in alive}


def free_slots(slots: list, posts) -> tuple[list, list]:
    """(свободные слоты, занятые) — второй пост на тот же день план не ставит (16.10.2026:
    повторное нажатие «Контент-план» удвоило 16, 18 и 21 октября)."""
    busy = occupied_days(posts)
    free, taken = [], []
    for s in slots:
        (taken if timezone.localtime(s).date() in busy else free).append(s)
    return free, taken


@shared_task(bind=True, ignore_result=True)
def build_content_plan_task(self, schema_name: str, slots: list[str], author: str = '',
                            brief: str = '', with_images: bool = False, job_id: int | None = None) -> dict:
    """
    Контент-план по базе знаний: сначала ИИ раскладывает темы по датам (рубрики,
    поводы, запреты — из базы знаний), потом пишет по черновику на каждую дату,
    по желанию — с картинкой. Черновики НЕ планируются сами — человек
    проверяет и жмёт «Запланировать». День, где уже стоит пост, пропускается.
    """
    from datetime import datetime, timedelta

    from django_tenants.utils import schema_context

    from apps.tenant.marketer import progress
    from apps.tenant.marketer.generator import _DIGEST_TASK, generate_post_ex, plan_posts
    from apps.tenant.marketer.knowledge import collect_knowledge
    from apps.tenant.marketer.models import MarketerPost, MarketerPostStatus, MarketerSettings

    created, failed, images_failed = [], 0, 0
    with schema_context(schema_name):
        cfg = MarketerSettings.objects.first()
        if not cfg or not cfg.is_enabled:
            progress.finish(job_id, error='AI-маркетолог выключен в настройках')
            return {'schema': schema_name, 'skipped': 'disabled'}
        when_all = [datetime.fromisoformat(s) for s in slots]
        lo, hi = min(when_all), max(when_all)
        around = list(MarketerPost.objects.filter(scheduled_at__date__gte=timezone.localtime(lo).date(),
                                                  scheduled_at__date__lte=timezone.localtime(hi).date()))
        when_all, skipped = free_slots(when_all, around)
        if not when_all:
            progress.finish(job_id, result={'post_ids': [], 'skipped': [s.isoformat() for s in skipped]})
            return {'schema': schema_name, 'created': [], 'skipped': len(skipped)}

        progress.stage(job_id, 'kb', total=len(when_all))
        knowledge = collect_knowledge()
        dates = [timezone.localtime(w).date().isoformat() for w in when_all]
        existing = [f'{timezone.localtime(p.scheduled_at):%Y-%m-%d}: {p.topic or p.text[:60]}'
                    for p in MarketerPost.objects.filter(
                        scheduled_at__gte=timezone.now() - timedelta(days=14),
                        status__in=[MarketerPostStatus.DRAFT, MarketerPostStatus.SCHEDULED,
                                    MarketerPostStatus.PUBLISHED]).order_by('scheduled_at')[:40]]
        progress.stage(job_id, 'plan')
        try:
            plan = plan_posts(knowledge, dates, brief=brief, existing=existing,
                              brand_voice=cfg.brand_voice, extra_facts=cfg.extra_facts)
        except Exception:
            logger.exception('marketer plan topics failed for %s, falling back to fixed topics', schema_name)
            plan = [None] * len(dates)

        for i, when in enumerate(when_all):
            item = plan[i] or dict(zip(('post_type', 'topic', 'task'), PLAN_TOPICS[i % len(PLAN_TOPICS)]))
            progress.stage(job_id, 'write', f'Пишу пост {i + 1} из {len(when_all)}: {item["topic"]}',
                           step=i)
            visual = ''
            try:
                out = generate_post_ex(knowledge, task=item['task'] or _DIGEST_TASK,
                                       brand_voice=cfg.brand_voice, extra_facts=cfg.extra_facts,
                                       brief=brief)
                text, model, visual = out['text'], out['model'], out.get('visual', '')
                status, error = MarketerPostStatus.DRAFT, ''
            except Exception as e:
                text, model, status, error = '', '', MarketerPostStatus.FAILED, f'Генерация не удалась: {str(e)[:900]}'
                failed += 1
            post = MarketerPost.objects.create(
                post_type=item['post_type'], status=status, text=text, context_snapshot=knowledge,
                model_used=model, created_by=author or 'ai', error=error,
                topic=item['topic'], scheduled_at=when, visual=visual,
            )
            created.append(post.pk)

        if with_images:
            from apps.tenant.marketer.images import generate_for_post
            drawable = [pk for pk in created
                        if MarketerPost.objects.filter(pk=pk, status=MarketerPostStatus.DRAFT).exists()]
            for i, pk in enumerate(drawable):
                progress.stage(job_id, 'draw', f'Рисую картинку {i + 1} из {len(drawable)}', step=i,
                               total=len(drawable))
                post = MarketerPost.objects.get(pk=pk)
                try:
                    generate_for_post(post)
                except Exception as e:                   # noqa: BLE001 — пост остаётся без картинки
                    images_failed += 1
                    post.error = f'Картинка не получилась: {str(e)[:500]}'
                    post.save(update_fields=['error', 'updated_at'])
    result = {'post_ids': created, 'failed': failed, 'images_failed': images_failed,
              'skipped': [s.isoformat() for s in skipped]}
    progress.finish(job_id, result=result,
                    error='' if len(created) > failed else 'Ни один пост не написан — см. ошибку в черновиках')
    logger.info('marketer content plan %s: %s posts, %s failed, %s skipped', schema_name, len(created),
                failed, len(skipped))
    return {'schema': schema_name, 'created': created, 'failed': failed, 'skipped': len(skipped)}


@shared_task(bind=True, ignore_result=True)
def generate_post_image_task(self, schema_name: str, post_id: int, job_id: int | None = None,
                             allow_bank: bool | None = False) -> dict:
    """Волна 4: картинка к посту в фоне (ИИ рисует до минуты).

    Кнопка «Нарисовать ИИ» — всегда рисует (allow_bank=False); None — по
    настройке сети «сначала фотобанк».
    """
    from django_tenants.utils import schema_context

    from apps.tenant.marketer import progress
    from apps.tenant.marketer.images import ImageError, generate_for_post
    from apps.tenant.marketer.models import MarketerPost

    with schema_context(schema_name):
        post = MarketerPost.objects.filter(pk=post_id).first()
        if post is None:
            progress.finish(job_id, error='Пост не найден')
            return {'skipped': 'no_post'}
        try:
            source = generate_for_post(post, job_id, allow_bank=allow_bank)
        except (ImageError, Exception) as e:
            post.error = f'Картинка не получилась: {str(e)[:500]}'
            post.save(update_fields=['error', 'updated_at'])
            progress.finish(job_id, error=post.error)
            logger.warning('marketer image failed %s/%s: %s', schema_name, post_id, e)
            return {'error': str(e)}
        if post.error.startswith('Картинка не получилась'):
            post.error = ''
            post.save(update_fields=['error', 'updated_at'])
        progress.finish(job_id, result={'post_ids': [post.pk], 'source': source})
    return {'ok': True}
