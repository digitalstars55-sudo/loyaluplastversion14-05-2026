from django.utils import timezone

from apps.shared.clients.models import Company, Domain


class CompanyNotFound(Exception):
    pass


class CompanyInactive(Exception):
    pass


class CompanyExpired(Exception):
    pass


def get_tenant_domain(client_id: int) -> dict:
    """
    По client_id компании возвращает домен, название и флаги входа.

    Флаги (`ClientConfig`, оба default=False) фронт читает ДО того, как узнал
    домен тенанта, — поэтому они отдаются здесь, а не в `/branches/<id>/`:
      web_entry_enabled — мини-апп работает и вне ВК (вход через VK ID);
      degrade_enabled   — при сбое механизмов ВК предлагать «Продолжить в браузере».
    Конфига у компании может не быть — тогда оба False (поведение как раньше).

    Raises:
        CompanyNotFound  — компания с таким client_id не найдена
        CompanyInactive  — компания деактивирована
        CompanyExpired   — подписка истекла
    """
    try:
        company = Company.objects.get(client_id=client_id)
    except Company.DoesNotExist:
        raise CompanyNotFound

    if not company.is_active:
        raise CompanyInactive

    if company.paid_until < timezone.localdate():
        raise CompanyExpired

    domain = (
        Domain.objects
        .filter(tenant=company, is_primary=True)
        .first()
        or Domain.objects.filter(tenant=company).first()
    )

    # OneToOne-обратная связь: при отсутствии конфига getattr вернёт default,
    # т.к. RelatedObjectDoesNotExist — подкласс AttributeError.
    config = getattr(company, 'config', None)

    return {
        'domain': domain.domain if domain else None,
        'name': company.name,
        'web_entry_enabled': bool(getattr(config, 'web_entry_enabled', False)),
        'degrade_enabled': bool(getattr(config, 'degrade_enabled', False)),
        # №78: кнопка «Поделиться номером» в мини-аппе = общий выключатель платформы
        # (settings.GUEST_PHONE_ENABLED) И флаг сети (ClientConfig). Выкл — как раньше.
        'guest_phone_enabled': _guest_phone_enabled(config),
        'guest_phone_reward_coins': int(getattr(config, 'guest_phone_reward_coins', 0) or 0) if _guest_phone_enabled(config) else 0,
    }


def _guest_phone_enabled(config) -> bool:
    from django.conf import settings
    return bool(getattr(settings, 'GUEST_PHONE_ENABLED', False)) and bool(getattr(config, 'guest_phone_enabled', False))
