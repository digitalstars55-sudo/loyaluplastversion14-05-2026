from django.apps import AppConfig


class MobileConfig(AppConfig):
    """
    Контейнер для мобильных API-эндпоинтов. Не содержит моделей —
    только вьюхи, которые читают/пишут в существующие модели других
    приложений. Аддитивен: можно отключить из INSTALLED_APPS, ничего
    не сломается на веб-панели.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.tenant.mobile'
    label = 'mobile'
    verbose_name = 'Мобильное API'
