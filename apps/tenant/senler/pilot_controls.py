"""Opt-in per-tenant budget limits for RF pilots; zero preserves existing behaviour."""
import hashlib
from contextlib import contextmanager
from decimal import Decimal
from functools import wraps

from django.db import connection
from django.utils import timezone


def limits(config):
    cost = getattr(config, 'rf_reward_max_cost_rub', 0)
    contacts = getattr(config, 'rf_daily_contact_limit', 0)
    # Unknown configuration must not be confused with an actual numeric limit.
    cost = Decimal(str(cost)) if isinstance(cost, (Decimal, int, float, str)) else Decimal('0')
    contacts = int(contacts) if isinstance(contacts, (int, str)) else 0
    return (cost if cost > 0 else None), max(contacts, 0)


def remaining_contacts(daily_limit, now, events):
    if not daily_limit:
        return None
    from apps.tenant.senler.models import AutoBroadcastLog
    local = timezone.localtime(now)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    used = AutoBroadcastLog.objects.filter(trigger_type__in=events, sent_at__gte=midnight, sent_at__lte=now).count()
    return max(daily_limit - used, 0)


@contextmanager
def pilot_lock():
    # LoyalUP connects directly to PostgreSQL (no transaction-mode pooler).
    # Keep concurrent Celery/manual executions from exceeding the daily cap.
    digest = hashlib.sha256(('loyalup-rf-pilot:' + connection.schema_name).encode()).digest()[:8]
    key = int.from_bytes(digest, byteorder='big', signed=True)
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s)', [key])
        acquired = cursor.fetchone()[0]
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [key])


def guard_pilot(function):
    @wraps(function)
    def guarded(rule, now=None, dry_run=False):
        from apps.tenant.senler.engine import _tenant_client_config, _rf_events
        _, cap = limits(_tenant_client_config())
        if dry_run or not cap or rule.event not in _rf_events():
            return function(rule, now=now, dry_run=dry_run)
        with pilot_lock() as acquired:
            if not acquired:
                return {'sent': 0, 'reason': 'pilot_running'}
            return function(rule, now=now, dry_run=dry_run)
    return guarded
