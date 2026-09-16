from django.apps import AppConfig


class CheckupConfig(AppConfig):
    """
    Стык с платформой CheckUp (контракт платформы v1.1, 16.09.2026).

    Живёт в SHARED-схеме: пользователи LoyalUP общие для всех сетей, и таблица
    соответствия «сотрудник CheckUp → User LoyalUP» тоже. Внутри — только
    обмен токена (POST /api/v1/internal/auth/exchange/): CheckUp приходит
    с того же сервера с секретом и получает короткий JWT LoyalUP от имени
    сотрудника. Пока CHECKUP_TOKEN_EXCHANGE_SECRET пуст, ручка отвечает 503
    и прод ничем не отличается от прежнего.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.shared.checkup'
    label = 'checkup'
    verbose_name = 'CheckUp: стык платформы'
