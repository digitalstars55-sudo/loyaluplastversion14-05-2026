from rest_framework import serializers


class TenantDomainResponseSerializer(serializers.Serializer):
    domain = serializers.CharField()
    name = serializers.CharField()

    # Флаги входа из ClientConfig (этап 1 «Антихрупкость входа»). Фронт читает их
    # ДО резолва домена: web_entry_enabled — работать ли вне ВК (вход через VK ID),
    # degrade_enabled — предлагать ли «Продолжить в браузере» при сбое ВК.
    # default=False: старый ответ без этих ключей сериализуется как раньше.
    web_entry_enabled = serializers.BooleanField(default=False)
    degrade_enabled = serializers.BooleanField(default=False)
    # №78: показывать ли гостю кнопку «Поделиться номером» (settings.GUEST_PHONE_ENABLED,
    # общий на платформу). default=False: старый ответ сериализуется как раньше.
    guest_phone_enabled = serializers.BooleanField(default=False)
