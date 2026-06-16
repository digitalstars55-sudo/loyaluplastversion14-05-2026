from django.contrib import admin

from apps.shared.config.admin_sites import tenant_admin

from .models import LoyaltyIdempotencyKey, LoyaltyOrder


@admin.register(LoyaltyOrder, site=tenant_admin)
class LoyaltyOrderAdmin(admin.ModelAdmin):
    list_display = (
        'external_order_id', 'client', 'branch', 'order_amount',
        'points_earned', 'points_redeemed', 'status', 'created_at',
    )
    list_filter = ('status', 'branch', 'created_at')
    search_fields = ('external_order_id', 'client__vk_id', 'client__first_name', 'client__last_name')
    readonly_fields = (
        'client', 'branch', 'external_order_id', 'order_amount',
        'points_earned', 'points_redeemed', 'status', 'created_at', 'refunded_at',
    )
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False


@admin.register(LoyaltyIdempotencyKey, site=tenant_admin)
class LoyaltyIdempotencyKeyAdmin(admin.ModelAdmin):
    list_display = ('key', 'created_at')
    search_fields = ('key',)
    readonly_fields = ('key', 'response', 'created_at')

    def has_add_permission(self, request):
        return False
