"""
Ход генерации маркетолога (16.10.2026): «на каком шаге, сколько осталось».

Кабинет опрашивает `GET marketer/jobs/` раз в пару секунд и рисует шкалу
этапов. Строка `MarketerJob` заводится в запросе (до постановки задачи), задача
двигает этапы. Ошибка записи хода никогда не роняет саму генерацию.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# Задача, которая молчит дольше, считается упавшей (воркер перезапустили и т.п.).
STALE_AFTER = timedelta(minutes=10)

# Этапы по видам — в порядке шкалы. Подпись этапа уточняется на лету
# («Пишу пост 2 из 5»), ключ остаётся прежним.
STAGES = {
    'post': [('kb', 'Читаю базу знаний'), ('write', 'Пишу текст'), ('save', 'Сохраняю черновик')],
    'plan': [('kb', 'Читаю базу знаний'), ('plan', 'Составляю план по базе знаний'),
             ('write', 'Пишу посты'), ('draw', 'Рисую картинки'), ('save', 'Готово')],
    'image': [('bank', 'Смотрю фотобанк'), ('compose', 'Собираю образ: стиль сети и приём из базы'),
              ('draw', 'Рисую'), ('save', 'Сохраняю')],
}


def running(kind: str):
    """Живая генерация этого вида (для запрета двойного нажатия) или None."""
    from apps.tenant.marketer.models import MarketerJob
    fresh = timezone.now() - STALE_AFTER
    return (MarketerJob.objects.filter(kind=kind, status=MarketerJob.Status.RUNNING,
                                       updated_at__gte=fresh).order_by('-pk').first())


def start(kind: str, *, total: int = 0, post=None, author: str = ''):
    from apps.tenant.marketer.models import MarketerJob
    first = STAGES[kind][0]
    return MarketerJob.objects.create(kind=kind, total=total, post=post, created_by=author[:150],
                                      stage=first[0], stage_label=first[1])


def _load(job_or_id):
    from apps.tenant.marketer.models import MarketerJob
    if job_or_id is None:
        return None
    if isinstance(job_or_id, MarketerJob):
        return job_or_id
    return MarketerJob.objects.filter(pk=job_or_id).first()


def stage(job_or_id, key: str, label: str = '', *, step: int | None = None, total: int | None = None):
    try:
        job = _load(job_or_id)
        if job is None:
            return None
        job.stage = key
        job.stage_label = (label or dict(STAGES.get(job.kind, [])).get(key, ''))[:200]
        fields = ['stage', 'stage_label', 'updated_at']
        if step is not None:
            job.step = step
            fields.append('step')
        if total is not None:
            job.total = total
            fields.append('total')
        job.save(update_fields=fields)
        return job
    except Exception:                                    # noqa: BLE001 — ход вторичен
        logger.exception('marketer progress: stage failed')
        return None


def finish(job_or_id, *, result: dict | None = None, error: str = ''):
    from apps.tenant.marketer.models import MarketerJob
    try:
        job = _load(job_or_id)
        if job is None:
            return None
        job.status = MarketerJob.Status.FAILED if error else MarketerJob.Status.DONE
        job.error = error[:2000]
        job.result = result or {}
        job.finished_at = timezone.now()
        if not error:
            job.stage, job.stage_label = 'done', 'Готово'
        job.save(update_fields=['status', 'error', 'result', 'finished_at', 'stage',
                                'stage_label', 'updated_at'])
        return job
    except Exception:                                    # noqa: BLE001
        logger.exception('marketer progress: finish failed')
        return None


def to_dict(job) -> dict:
    status = job.status
    if status == 'running' and job.updated_at and job.updated_at < timezone.now() - STALE_AFTER:
        status = 'failed'
    return {
        'id': job.pk,
        'kind': job.kind,
        'status': status,
        'stage': job.stage,
        'stage_label': job.stage_label,
        'stages': [{'key': k, 'label': lbl} for k, lbl in STAGES.get(job.kind, [])],
        'step': job.step,
        'total': job.total,
        'post_id': job.post_id,
        'result': job.result or {},
        'error': job.error or ('Генерация оборвалась — попробуйте ещё раз.'
                               if status == 'failed' and job.status == 'running' else ''),
        'created_by': job.created_by,
        'created_at': job.created_at.isoformat() if job.created_at else None,
        'updated_at': job.updated_at.isoformat() if job.updated_at else None,
        'finished_at': job.finished_at.isoformat() if job.finished_at else None,
    }
