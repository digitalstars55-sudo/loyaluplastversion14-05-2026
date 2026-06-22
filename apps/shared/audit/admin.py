from django.contrib import admin

from apps.shared.config.admin_sites import public_admin
from .models import AuditEvent


@admin.register(AuditEvent, site=public_admin)
class AuditEventAdmin(admin.ModelAdmin):
    """Read-only журнал в админке (основной просмотр — на /superadmin/audit/)."""

    list_display = ('created_at', 'actor_username', 'actor_role', 'tenant_name', 'action', 'target', 'method', 'path', 'ip')
    list_filter = ('action', 'actor_role', 'tenant_schema', 'created_at')
    search_fields = ('actor_username', 'target', 'path', 'ip', 'tenant_name')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Журнал чистить нельзя (целостность аудита).
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('actor')
