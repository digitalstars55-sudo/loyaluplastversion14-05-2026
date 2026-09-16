from django.apps import AppConfig


class MonitoringConfig(AppConfig):
    """
    Мониторинг платформы. Живёт в SHARED-схеме (public), потому что следит за
    вещами, общими для ВСЕХ сетей сразу: срок TLS-сертификата, продление
    доменов, оплата тенантов, callback ВК, доступность входа.

    Почему это вообще понадобилось: каждое падение 2026 года (истёкший
    wildcard-сертификат, просроченный домен levonework.ru, отключённый ВК
    callback) мы узнавали от гостей и владельца — то есть когда уже легло.
    Задача beat раз в час смотрит на те же сроки заранее и будит суперадминов
    пушем, пока чинить ещё дёшево.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.shared.monitoring'
    label = 'monitoring'
    verbose_name = 'Мониторинг платформы'
