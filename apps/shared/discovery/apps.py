from django.apps import AppConfig


class DiscoveryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.shared.discovery'
    label = 'discovery'
    verbose_name = 'Сетевой вход из каталога VK'
