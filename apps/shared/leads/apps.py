from django.apps import AppConfig


class LeadsConfig(AppConfig):
    """
    Lead-объекты — заявки от потенциальных клиентов через мобильный
    онбординг. Живут в SHARED-схеме (public), потому что их нужно видеть
    супер-администратору, а тенант ещё не создан.

    После одобрения (status=CONFIRMED) — на base lead создаётся новая
    Company + Domain + User в shared, а в их tenant-схеме — все стандартные
    миграции (django-tenants делает это автоматически).
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.shared.leads'
    label = 'leads'
    verbose_name = 'Заявки клиентов (Leads)'
