from __future__ import annotations

from rest_framework import serializers

from ..models import Lead


class LeadCreateSerializer(serializers.ModelSerializer):
    """
    Создание лида с минимальным набором полей. Возвращает session_token,
    который мобайл будет использовать в /chat/-эндпоинте.
    """
    class Meta:
        model = Lead
        fields = [
            'id', 'session_token', 'status',
            'cafe_name', 'cafe_count', 'traffic_estimate',
            'package_suggested', 'full_name', 'email',
            'domain_slug',
        ]
        read_only_fields = ['id', 'session_token', 'status']
        extra_kwargs = {
            'cafe_name': {'required': False, 'allow_blank': True},
            'cafe_count': {'required': False, 'allow_null': True},
            'traffic_estimate': {'required': False, 'allow_blank': True},
            'package_suggested': {'required': False, 'allow_blank': True},
            'full_name': {'required': False, 'allow_blank': True},
            'email': {'required': False, 'allow_blank': True},
            'domain_slug': {'required': False, 'allow_blank': True},
        }


class LeadSubmitSerializer(serializers.ModelSerializer):
    """
    Финальное обновление лида — мобайл шлёт всю собранную AI-менеджером
    информацию + vk_token. Сразу переводит в submitted, если is_complete.
    """
    class Meta:
        model = Lead
        fields = [
            'cafe_name', 'cafe_count', 'traffic_estimate',
            'package_suggested', 'full_name', 'email',
            'domain_slug', 'vk_token',
        ]
        # Все поля опциональны для PATCH — но для перехода в submitted нужны все.


class LeadDetailSerializer(serializers.ModelSerializer):
    """Полный сериализатор для super-admin / возврата состояния клиенту."""
    is_complete = serializers.BooleanField(read_only=True)

    class Meta:
        model = Lead
        fields = [
            'id', 'session_token', 'status', 'is_complete',
            'cafe_name', 'cafe_count', 'traffic_estimate', 'package_suggested',
            'full_name', 'email', 'domain_slug',
            'created_at', 'submitted_at', 'confirmed_at',
        ]
        read_only_fields = fields
