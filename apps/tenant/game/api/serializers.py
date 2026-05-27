from rest_framework import serializers

from apps.tenant.catalog.models import Product


# ── Request ────────────────────────────────────────────────────────────────────

class GameStartSerializer(serializers.Serializer):
    """Phase 1: guest initiates the game."""
    vk_id     = serializers.IntegerField()
    branch_id = serializers.IntegerField()
    code      = serializers.CharField(required=False, allow_blank=True, allow_null=True, default=None)
    delivery  = serializers.BooleanField(required=False, default=False)


class GameClaimSerializer(serializers.Serializer):
    """Phase 2: guest claims the reward after the animation completes."""
    session_token = serializers.CharField()
    employee_id   = serializers.IntegerField(required=False, allow_null=True)
    delivery      = serializers.BooleanField(required=False, default=False)


class GameCooldownRequestSerializer(serializers.Serializer):
    vk_id     = serializers.IntegerField()
    branch_id = serializers.IntegerField()


# ── Response ───────────────────────────────────────────────────────────────────

class GameSessionSerializer(serializers.Serializer):
    """Phase 1 response — given to the client before the animation plays."""
    session_token = serializers.CharField()
    score         = serializers.IntegerField()   # 1–10, drives animation height


class SuperPrizeProductSerializer(serializers.Serializer):
    """Single product from the super prize pool."""
    id        = serializers.IntegerField()
    name      = serializers.CharField()
    description = serializers.CharField()
    image_url = serializers.SerializerMethodField()

    def get_image_url(self, obj) -> str | None:
        if not (obj.image and obj.image.name):
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.image.url) if request else obj.image.url


class SuperPrizeRewardSerializer(serializers.Serializer):
    """SuperPrizeEntry + the pool of available products for the guest to choose from."""
    super_prize_id     = serializers.IntegerField(source='id')
    available_products = serializers.SerializerMethodField()

    def get_available_products(self, obj) -> list:
        from django.db.models import F
        branch = obj.client_branch.branch
        products = (
            Product.objects
            .filter(branch_assignments__branch=branch, is_super_prize=True, is_archived=False)
            .annotate(branch_ordering=F('branch_assignments__ordering'))
            .order_by('branch_ordering', 'name')
        )
        return SuperPrizeProductSerializer(products, many=True, context=self.context).data


class GameCooldownSerializer(serializers.Serializer):
    """GAME cooldown state for a guest."""
    is_active         = serializers.BooleanField()
    expires_at        = serializers.DateTimeField()
    seconds_remaining = serializers.SerializerMethodField()

    def get_seconds_remaining(self, obj) -> int:
        remaining = obj.remaining
        return int(remaining.total_seconds()) if remaining else 0
