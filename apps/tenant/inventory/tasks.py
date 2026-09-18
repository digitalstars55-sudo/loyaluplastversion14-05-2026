"""
Синхронизация себестоимости подарков для кабинета CheckUp (№34, контракт v1.8).

Кнопка «Синхронизировать затраты» в суперадминке гоняет команду
`backfill_gift_costs --commit --refill-zeros` СИНХРОННО в HTTP-запросе по всем
тенантам за всю историю (apps/shared/config/admin_sites.py). Для кабинета так
нельзя: запрос может жить минуты, а два нажатия = два полных обхода параллельно.
Поэтому здесь — celery-задача + блокировка в кэше + последний результат в кэше,
чтобы `status/` было чем отвечать. Саму команду не меняем.
"""
from __future__ import annotations

import io
import logging
import re

from celery import shared_task
from django.core.cache import cache
from django.utils import timezone

log = logging.getLogger(__name__)

LOCK_KEY = 'gift_costs:sync:lock'
LAST_KEY = 'gift_costs:sync:last'
LOCK_TTL = 30 * 60          # полный обход всех сетей за историю укладывается в минуты
LAST_TTL = 30 * 24 * 3600
RESULT_RE = re.compile(r'всего inventory \+(\d+), story \+(\d+)(?:, обновлено нулевых (\d+))?')  # итоговая строка команды


def run_gift_costs_sync(schema: str | None = None, commit: bool = True) -> dict:
    """Запустить команду и разобрать её итоговую строку. Бросает исключение команды как есть."""
    from django.core.management import call_command
    buf = io.StringIO()
    kwargs = {'commit': commit, 'refill_zeros': True, 'stdout': buf}
    if schema:
        kwargs['schema'] = schema
    call_command('backfill_gift_costs', **kwargs)
    output = buf.getvalue()
    m = RESULT_RE.search(output)
    return {
        'inventory': int(m.group(1)) if m else 0,
        'story': int(m.group(2)) if m else 0,
        'refilled_zeros': int(m.group(3)) if (m and m.group(3)) else 0,
        'commit': commit,
        'schema': schema or 'all',
        'output_tail': output[-500:],
    }


def sync_status() -> dict:
    """running + последний результат — для GET …/sync-gift-costs/status/."""
    return {'running': bool(cache.get(LOCK_KEY)), 'last_run': cache.get(LAST_KEY)}


@shared_task(name='apps.tenant.inventory.tasks.sync_gift_costs_task')
def sync_gift_costs_task(schema: str | None = None, started_by: str = '') -> dict:
    started_at = timezone.now()
    record = {'started_at': started_at.isoformat(), 'started_by': started_by, 'schema': schema or 'all',
              'finished_at': None, 'ok': False, 'result': None, 'error': ''}
    try:
        record['result'] = run_gift_costs_sync(schema, commit=True)
        record['ok'] = True
    except Exception as exc:                      # noqa: BLE001 — результат нужен кабинету, не трейсбек
        log.exception('sync_gift_costs_task failed')
        record['error'] = str(exc)[:500]
    finally:
        record['finished_at'] = timezone.now().isoformat()
        cache.set(LAST_KEY, record, LAST_TTL)
        cache.delete(LOCK_KEY)
        try:
            from apps.shared.clients.cross_stats import invalidate_overview_cache
            invalidate_overview_cache()           # иначе кабинет 5 минут видит старые цифры
        except Exception:                          # noqa: BLE001
            log.exception('overview cache invalidation failed')
    return record
