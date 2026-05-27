from rest_framework import serializers

from apps.tenant.catalog.models import Product


# ── Request ────────────────────────────────────────────────────────────────────

class InventoryRequestSerializer(serializers.Serializer):
    vk_id     = serializers.IntegerField()
    branch_id = serializers.IntegerField()


class SuperPrizeClaimSerializer(serializers.Serializer):
    vk_id      = serializers.IntegerField()
    branch_id  = serializers.IntegerField()
    product_id = serializers.IntegerField()


BirthdayPrizeClaimSerializer = SuperPrizeClaimSerializer


class InventoryActivateSerializer(serializers.Serializer):
    vk_id      = serializers.IntegerField()
    branch_id  = serializers.IntegerField()
    item_id    = serializers.IntegerField()
    code       = serializers.CharField(required=False, allow_blank=True, allow_null=True, default=None)


# ── Response ───────────────────────────────────────────────────────────────────

class InventoryItemSerializer(serializers.Serializer):
    """Single item in a guest's inventory."""
    id                = serializers.IntegerField()
    product_id        = serializers.IntegerField(source='product.pk', allow_null=True, default=None)
    product_name      = serializers.CharField(source='product.name', allow_null=True, default=None)
    product_description = serializers.CharField(source='product.description', allow_null=True, default=None)
    product_image_url = serializers.SerializerMethodField()
    acquired_from     = serializers.CharField()
    status            = serializers.CharField()   # computed @property
    duration          = serializers.IntegerField()
    activated_at      = serializers.DateTimeField(allow_null=True)
    expires_at        = serializers.DateTimeField(allow_null=True)
    created_at        = serializers.DateTimeField()

    def get_product_image_url(self, obj) -> str | None:
        if not obj.product:
            return None
        img = obj.product.image
        if not (img and img.name):
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(img.url) if request else img.url


class _SuperPrizeProductSerializer(serializers.Serializer):
    """Minimal product representation inside a super prize entry."""
    id          = serializers.IntegerField()
    name        = serializers.CharField()
    description = serializers.CharField()
    image_url   = serializers.SerializerMethodField()

    def get_image_url(self, obj) -> str | None:
        if not (obj.image and obj.image.name):
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.image.url) if request else obj.image.url


class SuperPrizeEntrySerializer(serializers.Serializer):
    """
    SuperPrizeEntry response.

    - status == 'pending'  → product is null, available_products is populated
    - status == 'claimed'  → product holds the chosen item, available_products is []
    - status == 'issued'   → same as claimed
    - status == 'expired'  → product is null, available_products is []
    """
    id                 = serializers.IntegerField()
    acquired_from      = serializers.CharField()
    status             = serializers.CharField()   # computed @property
    created_at         = serializers.DateTimeField()
    claimed_at         = serializers.DateTimeField(allow_null=True)
    product            = serializers.SerializerMethodField()
    available_products = serializers.SerializerMethodField()

    def get_product(self, obj) -> dict | None:
        if not obj.product:
            return None
        img = obj.product.image
        img_url = None
        if img and img.name:
            request = self.context.get('request')
            img_url = request.build_absolute_uri(img.url) if request else img.url
        return {
            'id':        obj.product.pk,
            'name':      obj.product.name,
            'description': obj.product.description,
            'image_url': img_url,
        }

    def get_available_products(self, obj) -> list:
        if obj.status != 'pending':
            return []
        from django.db.models import F as _F
        branch = obj.client_branch.branch
        products = (
            Product.objects
            .filter(branch_assignments__branch=branch, is_super_prize=True)
            .annotate(branch_ordering=_F('branch_assignments__ordering'))
            .order_by('branch_ordering', 'name')
        )
        return _SuperPrizeProductSerializer(products, many=True, context=self.context).data


class BirthdayStatusSerializer(serializers.Serializer):
    """
    Birthday status for the frontend.

    is_birthday_window — today is within ±5 days of birth_date
    already_claimed    — a birthday prize was already claimed this calendar year
    can_claim          — all conditions met (window + not claimed + birth_date established ≥30 days)
    """
    is_birthday_window = serializers.BooleanField()
    already_claimed    = serializers.BooleanField()
    can_claim          = serializers.BooleanField()


class BirthdayProductSerializer(serializers.Serializer):
    """Product available as a birthday prize."""
    id          = serializers.IntegerField()
    name        = serializers.CharField()
    description = serializers.CharField(allow_blank=True, default='')
    image_url   = serializers.SerializerMethodField()
    price       = serializers.IntegerField()

    def get_image_url(self, obj) -> str | None:
        if not (obj.image and obj.image.name):
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.image.url) if request else obj.image.url


class InventoryCooldownSerializer(serializers.Serializer):
    """INVENTORY cooldown state for a guest."""
    is_active         = serializers.BooleanField()
    expires_at        = serializers.DateTimeField()
    seconds_remaining = serializers.SerializerMethodField()

    def get_seconds_remaining(self, obj) -> int:
        remaining = obj.remaining
        return int(remaining.total_seconds()) if remaining else 0
