from rest_framework import serializers


# ── Branch info ───────────────────────────────────────────────────────────────

class BranchInfoSerializer(serializers.Serializer):
    """Response for GET /branches/<branch_id>/"""
    id            = serializers.IntegerField()
    branch_id     = serializers.IntegerField()
    name          = serializers.CharField()
    address       = serializers.CharField()
    phone         = serializers.CharField()
    yandex_map    = serializers.CharField()
    gis_map       = serializers.CharField()
    # From ClientConfig (None if tenant config not found)
    logotype_url    = serializers.CharField(allow_null=True)
    coin_icon_url   = serializers.CharField(allow_null=True)
    vk_group_id     = serializers.IntegerField(allow_null=True)
    vk_group_name   = serializers.CharField(allow_null=True)
    # Story image template (from Branch.story_image)
    story_image_url = serializers.CharField(allow_null=True)
    # Кастомные сообщения для гостей (branch override → tenant default → fallback)
    code_prompt_message = serializers.CharField()
    quest_show_message  = serializers.CharField()
    # Цвета бренда — фронт перекрашивает весь миниапп от них (LU-14).
    # brand_color_secondary ОБЯЗАТЕЛЬНО объявить, иначе DRF выкидывает поле из
    # ответа и второй (акцентный) цвет не доходит до фронта.
    brand_color           = serializers.CharField()
    brand_color_secondary = serializers.CharField()


# ── Client profile ────────────────────────────────────────────────────────────

class ClientProfileResponseSerializer(serializers.Serializer):
    """
    Flat response combining ClientBranch + guest.Client + VK subscription status.
    Input: ClientBranch with select_related('client', 'vk_status') and _coins_balance annotation.
    """

    # ClientBranch
    id          = serializers.IntegerField()
    birth_date  = serializers.DateField(allow_null=True)
    is_employee = serializers.BooleanField()
    coins_balance = serializers.SerializerMethodField()

    # guest.Client (flattened)
    vk_id      = serializers.IntegerField(source='client.vk_id')
    first_name = serializers.CharField(source='client.first_name')
    last_name  = serializers.CharField(source='client.last_name')
    photo_url  = serializers.URLField(source='client.photo_url', allow_blank=True)

    # ClientVKStatus (OneToOne — may not exist yet → defaults to "not subscribed")
    is_community_member      = serializers.SerializerMethodField()
    community_via_app        = serializers.SerializerMethodField()
    is_newsletter_subscriber = serializers.SerializerMethodField()
    newsletter_via_app       = serializers.SerializerMethodField()
    is_story_uploaded        = serializers.SerializerMethodField()
    story_uploaded_at        = serializers.SerializerMethodField()

    # Raw VK birthday string (e.g. "15.3" or "1990-03-15") — only present in VK OAuth flow
    vk_bdate = serializers.SerializerMethodField()

    def _vk(self, obj):
        # RelatedObjectDoesNotExist is a subclass of AttributeError,
        # so getattr with a default safely returns None if no record exists.
        return getattr(obj, 'vk_status', None)

    def get_coins_balance(self, obj) -> int:
        return getattr(obj, '_coins_balance', obj.coins_balance)

    def get_is_community_member(self, obj) -> bool:
        vk = self._vk(obj)
        return vk.is_community_member if vk else False

    def get_community_via_app(self, obj) -> bool | None:
        vk = self._vk(obj)
        return vk.community_via_app if vk else None

    def get_is_newsletter_subscriber(self, obj) -> bool:
        vk = self._vk(obj)
        return vk.is_newsletter_subscriber if vk else False

    def get_newsletter_via_app(self, obj) -> bool | None:
        vk = self._vk(obj)
        return vk.newsletter_via_app if vk else None

    def get_is_story_uploaded(self, obj) -> bool:
        vk = self._vk(obj)
        return vk.is_story_uploaded if vk else False

    def get_story_uploaded_at(self, obj) -> str | None:
        vk = self._vk(obj)
        return vk.story_uploaded_at if vk else None

    def get_vk_bdate(self, obj) -> str | None:
        # _vk_bdate is set by vk_web_auth() only during OAuth flow.
        # For regular GET/POST/PATCH it won't be present → returns None.
        return getattr(obj, '_vk_bdate', None)


# ── Request serializers ───────────────────────────────────────────────────────

class ClientGetRequestSerializer(serializers.Serializer):
    vk_id     = serializers.IntegerField()
    branch_id = serializers.IntegerField()


class ClientRegistrationRequestSerializer(serializers.Serializer):
    vk_id             = serializers.IntegerField()
    branch_id         = serializers.IntegerField()
    first_name        = serializers.CharField(required=False, allow_blank=True, default='')
    last_name         = serializers.CharField(required=False, allow_blank=True, default='')
    photo_url         = serializers.URLField(required=False, allow_blank=True, default='')
    birth_date        = serializers.DateField(required=False, allow_null=True, default=None)
    source            = serializers.ChoiceField(
        choices=['restaurant', 'delivery'], required=False, default='restaurant',
    )
    invited_by_cb_id  = serializers.IntegerField(required=False, allow_null=True, default=None)


class ClientUpdateRequestSerializer(serializers.Serializer):
    """
    PATCH: only provided fields are updated.
    Not included ≠ set to null — absent fields are simply ignored.

    community_via_app=True  → marks community subscription as done via app
    newsletter_via_app=True → marks newsletter subscription as done via app
    """
    vk_id     = serializers.IntegerField()
    branch_id = serializers.IntegerField()
    # guest.Client
    first_name = serializers.CharField(required=False, allow_null=True)
    last_name  = serializers.CharField(required=False, allow_null=True)
    photo_url  = serializers.URLField(required=False, allow_null=True)
    # ClientBranch
    birth_date = serializers.DateField(required=False, allow_null=True)
    # VK subscriptions — attribution (user clicked Join in app)
    community_via_app  = serializers.BooleanField(required=False, allow_null=True)
    newsletter_via_app = serializers.BooleanField(required=False, allow_null=True)
    # VK subscriptions — current status from VK Bridge (sent on app init, no attribution)
    is_community_member      = serializers.BooleanField(required=False, allow_null=True)
    is_newsletter_subscriber = serializers.BooleanField(required=False, allow_null=True)


class VKAuthRequestSerializer(serializers.Serializer):
    """
    POST /api/v1/vk/auth/

    VK ID OAuth2 PKCE — параметры для server-side обмена кода на токен.

    code          — authorization code из redirect-callback VK.
    device_id     — device_id из того же callback (привязан к PKCE сессии).
    code_verifier — PKCE code_verifier, сгенерированный фронтом перед OAuth2 запросом.
    redirect_uri  — тот же redirect_uri, что использовался при инициации OAuth2.
    branch_id     — ID торговой точки из QR-кода.
    birth_date    — дата рождения (заполняется при онбординге).
    """
    code          = serializers.CharField()
    device_id     = serializers.CharField()
    code_verifier = serializers.CharField()
    redirect_uri  = serializers.URLField()
    state         = serializers.CharField()
    branch_id     = serializers.IntegerField()
    birth_date    = serializers.DateField(required=False, allow_null=True, default=None)


class BranchIdRequestSerializer(serializers.Serializer):
    branch_id = serializers.IntegerField()


# ── Employee ───────────────────────────────────────────────────────────────────

class EmployeeSerializer(serializers.Serializer):
    """Response for GET /employees/ — ClientBranch with is_employee=True."""
    id            = serializers.IntegerField()
    birth_date    = serializers.DateField(allow_null=True)
    coins_balance = serializers.SerializerMethodField()
    vk_id      = serializers.IntegerField(source='client.vk_id')
    first_name = serializers.CharField(source='client.first_name')
    last_name  = serializers.CharField(source='client.last_name')
    photo_url  = serializers.URLField(source='client.photo_url', allow_blank=True)

    def get_coins_balance(self, obj) -> int:
        return getattr(obj, '_coins_balance', obj.coins_balance)


# ── Promotion ──────────────────────────────────────────────────────────────────

class PromotionSerializer(serializers.Serializer):
    """Response for GET /promotions/"""
    id       = serializers.IntegerField()
    title    = serializers.CharField()
    discount = serializers.CharField()
    dates    = serializers.CharField()
    image_url = serializers.SerializerMethodField()

    def get_image_url(self, obj) -> str | None:
        if not (obj.images and obj.images.name):
            return None
        request = self.context.get('request')
        return request.build_absolute_uri(obj.images.url) if request else obj.images.url


# ── CoinTransaction ────────────────────────────────────────────────────────────

class CoinTransactionSerializer(serializers.Serializer):
    """Response for GET /transactions/"""
    id          = serializers.IntegerField()
    type        = serializers.CharField()
    source      = serializers.CharField()
    amount      = serializers.IntegerField()
    description = serializers.CharField()
    created_at  = serializers.DateTimeField()


# ── VK Story ───────────────────────────────────────────────────────────────────

class VKStoryRequestSerializer(serializers.Serializer):
    """POST /api/v1/vk/story/ — mark that the guest published a VK story."""
    vk_id     = serializers.IntegerField()
    branch_id = serializers.IntegerField()


class VKStoryResponseSerializer(serializers.Serializer):
    """Response for POST /api/v1/vk/story/"""
    is_story_uploaded = serializers.BooleanField()
    story_uploaded_at = serializers.DateTimeField(allow_null=True)
    # True when this call triggered the first upload (useful for game cooldown bypass UI)
    first_upload      = serializers.BooleanField()


# ── Testimonials ───────────────────────────────────────────────────────────────

class TestimonialCreateSerializer(serializers.Serializer):
    """
    POST /api/v1/testimonials/
    Принимает отзыв из мини-приложения ВКонтакте.
    """
    vk_id     = serializers.IntegerField(help_text='VK ID гостя из мини-приложения')
    branch_id = serializers.IntegerField(help_text='branch_id торговой точки')
    review    = serializers.CharField(help_text='Текст отзыва')
    rating    = serializers.IntegerField(min_value=1, max_value=5, required=False, allow_null=True)
    phone     = serializers.CharField(max_length=20, required=False, allow_blank=True, default='')
    table     = serializers.IntegerField(required=False, allow_null=True)
