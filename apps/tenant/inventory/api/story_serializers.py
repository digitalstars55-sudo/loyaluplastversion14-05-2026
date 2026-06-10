from rest_framework import serializers
from django.utils import timezone

from ..models import StoryStatus


# ── Request ────────────────────────────────────────────────────────────────────

class StoryRequestSerializer(serializers.Serializer):
    vk_id     = serializers.IntegerField()
    branch_id = serializers.IntegerField()


class StorySelectSerializer(serializers.Serializer):
    vk_id      = serializers.IntegerField()
    branch_id  = serializers.IntegerField()
    product_id = serializers.IntegerField()


class StoryActivateSerializer(serializers.Serializer):
    vk_id     = serializers.IntegerField()
    branch_id = serializers.IntegerField()
    code      = serializers.CharField(required=False, allow_blank=True, allow_null=True, default=None)


# ── Response ───────────────────────────────────────────────────────────────────

class StoryAccessSerializer(serializers.Serializer):
    enabled        = serializers.BooleanField()
    can_play       = serializers.BooleanField()
    already_played = serializers.BooleanField()
    status         = serializers.CharField()
    has_gifts      = serializers.BooleanField()


class StoryProductSerializer(serializers.Serializer):
    """Подарок из набора сториз (для экрана выбора)."""
    id          = serializers.IntegerField()
    name        = serializers.CharField()
    description = serializers.CharField(allow_blank=True, default='')
    emoji       = serializers.CharField(allow_blank=True, default='')
    image_url   = serializers.SerializerMethodField()

    def get_image_url(self, obj) -> str | None:
        if not (obj.image and obj.image.name):
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.image.url) if request else obj.image.url


class StoryGiftSerializer(serializers.Serializer):
    """
    Подарок из сториз в «Мои подарки».

    Контекст:
      request  — для абсолютного URL картинки
      settings — резолв story-настроек (для условий и текста инструкции)
    """
    id                  = serializers.IntegerField()
    status              = serializers.CharField()        # computed @property
    status_label        = serializers.CharField()        # computed @property
    product_id          = serializers.IntegerField(source='product.pk', allow_null=True, default=None)
    product_name        = serializers.CharField(source='product.name', allow_null=True, default=None)
    product_description = serializers.CharField(source='product.description', allow_null=True, default=None)
    product_image_url   = serializers.SerializerMethodField()
    min_order_amount    = serializers.IntegerField()
    duration            = serializers.IntegerField()
    activated_at        = serializers.DateTimeField(allow_null=True)
    expires_at          = serializers.DateTimeField(allow_null=True)
    seconds_remaining   = serializers.SerializerMethodField()
    cafe_address        = serializers.SerializerMethodField()
    activation_text     = serializers.SerializerMethodField()
    created_at          = serializers.DateTimeField()

    def get_product_image_url(self, obj) -> str | None:
        if not (obj.product and obj.product.image and obj.product.image.name):
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.product.image.url) if request else obj.product.image.url

    def get_seconds_remaining(self, obj) -> int:
        if obj.status == StoryStatus.ACTIVATED and obj.expires_at:
            rem = (obj.expires_at - timezone.now()).total_seconds()
            return max(0, int(rem))
        return 0

    def get_cafe_address(self, obj) -> str:
        settings = self.context.get('settings') or {}
        return settings.get('cafe_address') or ''

    def get_activation_text(self, obj) -> str:
        settings = self.context.get('settings')
        if not settings:
            return ''
        from .story_services import render_story_text
        return render_story_text(
            settings.get('activation_text', ''),
            cafe_name=obj.client_branch.branch.name,
            settings=settings,
            gift_name=obj.product.name if obj.product else '',
        )
