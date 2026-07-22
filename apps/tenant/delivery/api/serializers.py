from rest_framework import serializers


# ── Request ────────────────────────────────────────────────────────────────────

class WebhookRequestSerializer(serializers.Serializer):
    """Payload sent by a POS system (iiko / Dooglys)."""
    source    = serializers.ChoiceField(choices=['iiko', 'dooglys'])
    branch_id = serializers.CharField()
    code      = serializers.CharField()


class CodeActivationRequestSerializer(serializers.Serializer):
    """Guest submits the 5-digit short code from their delivery receipt."""
    short_code = serializers.CharField(min_length=5, max_length=5)
    vk_id      = serializers.IntegerField()
    branch_id  = serializers.IntegerField()


class CodeResolveRequestSerializer(serializers.Serializer):
    """
    Сетевой резолв: гость вводит код БЕЗ точки (тенант-QR). Точку определяем
    по коду среди всех точек тенанта.
    """
    short_code = serializers.CharField(min_length=5, max_length=5)
    vk_id      = serializers.IntegerField()


# ── Response ───────────────────────────────────────────────────────────────────

class DeliverySerializer(serializers.Serializer):
    """Shared response for webhook registration and code activation."""
    id           = serializers.IntegerField()
    short_code   = serializers.CharField()
    order_source = serializers.CharField()
    status       = serializers.CharField()   # computed @property
    expires_at   = serializers.DateTimeField()
    activated_at = serializers.DateTimeField(allow_null=True)
