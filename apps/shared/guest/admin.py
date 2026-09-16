from django.contrib import admin
from django.utils.html import format_html

from apps.shared.config.admin_sites import public_admin
from .models import Client


@admin.register(Client, site=public_admin)
class ClientAdmin(admin.ModelAdmin):

    def has_view_permission(self, request, obj=None):
        if getattr(request.user, 'role', None) == 'network_admin':
            return False
        return super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        # Только is_superuser может создавать/изменять/удалять гостей
        if not request.user.is_superuser:
            return False
        return super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        if not request.user.is_superuser:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)
    list_display = ('__str__', 'vk_id', 'photo_preview', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('vk_id', 'first_name', 'last_name', 'phone')
    readonly_fields = ('vk_id', 'photo_preview', 'phone_source', 'phone_consent_at', 'created_at', 'updated_at')

    fieldsets = (
        (None, {
            'fields': ('vk_id', 'first_name', 'last_name', 'photo_url', 'photo_preview'),
        }),
        ('Телефон (с согласия через ВК, №78)', {
            'fields': ('phone', 'phone_source', 'phone_consent_at'),
        }),
        ('Статус', {
            'fields': ('is_active',),
        }),
        ('Служебное', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Фото')
    def photo_preview(self, obj):
        if obj.photo_url:
            return format_html(
                '<img src="{}" style="width:40px;height:40px;border-radius:50%;object-fit:cover;" />',
                obj.photo_url,
            )
        return '—'
