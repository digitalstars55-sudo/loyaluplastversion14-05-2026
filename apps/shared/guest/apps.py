from django.apps import AppConfig


class GuestConfig(AppConfig):
    name = 'apps.shared.guest'
    verbose_name = 'Гости'

    def ready(self):
        from . import signals  # noqa: F401 — каналы гостя пишутся сами
