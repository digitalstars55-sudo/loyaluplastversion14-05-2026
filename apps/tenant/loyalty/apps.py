from django.apps import AppConfig


class LoyaltyConfig(AppConfig):
    """
    Сервисное API лояльности для внешнего ordering-BFF (приложение заказа
    LevOne / Шавуха). Живёт в схеме тенанта.

    Содержит две собственные модели:
      • LoyaltyOrder           — лоялти-состояние заказа (для статусов и возврата);
      • LoyaltyIdempotencyKey  — журнал идемпотентности мутирующих вызовов.

    Сами движения монет по-прежнему пишутся в branch.CoinTransaction —
    баланс/админка/мобайл продолжают работать как раньше. Аддитивно: можно
    убрать из INSTALLED_APPS, и веб/мобайл ничего не заметят.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.tenant.loyalty'
    label = 'loyalty'
    verbose_name = 'Лояльность (сервис-API)'
