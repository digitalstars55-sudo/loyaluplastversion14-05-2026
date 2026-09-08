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
