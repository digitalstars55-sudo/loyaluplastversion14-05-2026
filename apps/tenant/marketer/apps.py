from django.apps import AppConfig


class MarketerAppConfig(AppConfig):
    """
    AI-маркетолог (VK Autopilot, PoV).

    Генерирует контент для стены VK-сообщества из живых данных лояльности
    (Knowledge Core): еженедельный дайджест «Что нового?», позже — инсайты
    и промо. Публикация через ОТДЕЛЬНЫЙ токен (НЕ senler-токен: спам-флаг
    за постинг уронил бы канал отзывов/рассылок).

    Аддитивно: по умолчанию всё выключено (MarketerSettings.is_enabled=False),
    без настроек ни одна задача ничего не делает.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.tenant.marketer'
    label = 'marketer'
    verbose_name = 'AI-маркетолог'
