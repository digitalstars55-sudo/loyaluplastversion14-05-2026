from django.apps import AppConfig


class AuditConfig(AppConfig):
    """
    Журнал действий (аудит-лог). Живёт в SHARED-схеме (public), чтобы
    суперадмин видел активность СРАЗУ по всем тенантам в одной таблице:
    кто (ник/роль), у какого клиента, что сделал, какой эндпоинт, во сколько,
    с какого IP. Пишется middleware-ом на каждый осмысленный запрос +
    явные события (вход/неудачный вход).
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.shared.audit'
    label = 'audit'
    verbose_name = 'Журнал действий'

    def ready(self):
        # Подключаем сигналы (вход в Django-админку через сессию).
        from . import signals  # noqa: F401
