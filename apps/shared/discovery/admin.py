from django.contrib import admin

from apps.shared.config.admin_sites import public_admin
from .models import DiscoveryEvent, DiscoveryClaim


class DiscoveryEventAdmin(admin.ModelAdmin):
    list_display = ('vk_id', 'stage', 'created_at')
    list_filter = ('stage', 'created_at')
    search_fields = ('vk_id',)
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)


class DiscoveryClaimAdmin(admin.ModelAdmin):
    list_display = ('vk_id', 'city', 'company', 'created_at', 'redeemed_at')
    list_filter = ('city', 'created_at', 'redeemed_at')
    search_fields = ('vk_id', 'city')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    raw_id_fields = ('company',)


public_admin.register(DiscoveryEvent, DiscoveryEventAdmin)
public_admin.register(DiscoveryClaim, DiscoveryClaimAdmin)
