"""
Beat-задача мониторинга платформы (раз в час, main/celery.py).

Здесь и только здесь читаются settings и БД: checks.py остаётся чистым, а
tasks.py собирает ему вход — списки хостов, компании, ВК-группы тенантов.

Две вещи важнее аккуратности кода:
  * пока PLATFORM_MONITOR_ENABLED не включён, задача не делает НИЧЕГО — ни
    сетевых вызовов, ни пушей (волна 0 катится на прод выключенной);
  * упавшая проверка не должна ронять остальные. Каждая обёрнута отдельно и
    сама превращается в сигнал monitor:<проверка>:error — молчащий мониторинг
    хуже отсутствующего, потому что создаёт ложное чувство покоя.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from .alerts import deliver
from .checks import (
    SEVERITY_WARN,
    Finding,
    check_domains,
    check_probes,
    check_tenants_paid,
    check_tls,
    check_vk_callbacks,
    fetch_vk_callback_servers,
)

logger = logging.getLogger(__name__)


def _collect_companies() -> list:
    """Все тенанты платформы. public и выключенные отсеет сама проверка оплаты."""
    from django_tenants.utils import get_tenant_model

    return list(get_tenant_model().objects.all())


def _collect_tenant_groups() -> list[tuple[str, int, str]]:
    """
    (schema_name, vk_group_id, token) по активным тенантам.

    SenlerConfig лежит пер-точку, и у сети из пяти кафе на одну ВК-группу
    приходится пять одинаковых конфигов — дедуплицируем по vk_group_id, иначе
    получили бы пять одинаковых сигналов про один callback. Токен берём у
    первого конфига, где он непустой (у части точек поле не заполняют).
    """
    from django_tenants.utils import get_tenant_model, schema_context

    from apps.tenant.senler.models import SenlerConfig

    groups: list[tuple[str, int, str]] = []
    tenants = get_tenant_model().objects.exclude(schema_name='public').filter(is_active=True)

    for tenant in tenants:
        try:
            with schema_context(tenant.schema_name):
                by_group: dict[int, str] = {}
                for config in SenlerConfig.objects.order_by('id'):
                    group_id = config.vk_group_id
                    if not group_id:
                        continue
                    if not by_group.get(group_id):
                        by_group[group_id] = (config.vk_community_token or '').strip()
        except Exception as e:
            # Схема может быть на миграции/битой — это забота не мониторинга.
            logger.warning('monitoring: не прочитать SenlerConfig у %s: %s', tenant.schema_name, e)
            continue

        for group_id, token in by_group.items():
            if not token:
                logger.info('monitoring: у %s группа %s без токена — пропускаем', tenant.schema_name, group_id)
                continue
            groups.append((tenant.schema_name, group_id, token))

    return groups


def _safe(name: str, func) -> list[Finding]:
    """Прогнать проверку; её падение превратить в сигнал, а не в упавшую задачу."""
    try:
        return list(func() or [])
    except Exception as e:
        logger.exception('monitoring: проверка %s упала: %s', name, e)
        return [Finding(
            key=f'monitor:{name}:error',
            severity=SEVERITY_WARN,
            title=f'Проверка «{name}» не отработала',
            body=f'Мониторинг не смог выполнить проверку {name}: {e}. Эта часть платформы сейчас не под присмотром.',
            data={'check': name, 'error': str(e)},
        )]


def platform_health_report() -> list[Finding]:
    """
    Прогнать все проверки и вернуть сигналы БЕЗ доставки.
    Удобно дёргать руками из shell, чтобы посмотреть, что увидел бы мониторинг,
    не рассылая при этом пуши владельцу.
    """
    today = timezone.localdate()
    warn_days = settings.PLATFORM_MONITOR_WARN_DAYS

    findings: list[Finding] = []
    findings += _safe('tls', lambda: check_tls(settings.PLATFORM_MONITOR_TLS_HOSTS, today, warn_days))
    findings += _safe('domains', lambda: check_domains(settings.PLATFORM_MONITOR_DOMAIN_EXPIRY, today, warn_days))
    findings += _safe('paid', lambda: check_tenants_paid(_collect_companies(), today, warn_days))
    findings += _safe('vk_callback', lambda: check_vk_callbacks(_collect_tenant_groups(), fetch_vk_callback_servers))
    findings += _safe('probes', lambda: check_probes(settings.PLATFORM_MONITOR_PROBE_URLS))
    return findings


@shared_task(name='apps.shared.monitoring.tasks.platform_health_check_task')
def platform_health_check_task() -> dict:
    """
    Ежечасный обход платформы: сертификаты, домены, оплата сетей, callback ВК,
    доступность входа. Сигналы уходят пушем суперадминам с дедупом.
    Никогда не бросает — beat не должен краснеть из-за недоступного whois.
    """
    if not settings.PLATFORM_MONITOR_ENABLED:
        return {'skipped': 'disabled'}

    findings = platform_health_report()
    now = timezone.now()

    try:
        stats = deliver(findings, now, settings.PLATFORM_MONITOR_REPEAT_HOURS)
    except Exception as e:
        logger.exception('monitoring: deliver упал: %s', e)
        stats = {'error': str(e)}

    result = dict(stats)
    result['checked'] = len(findings)
    result['keys'] = [f.key for f in findings]
    logger.info('platform_health_check: %s', result)
    return result
