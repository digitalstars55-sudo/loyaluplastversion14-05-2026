from django.contrib import admin

from apps.shared.config.admin_sites import public_admin

from .models import CheckUpIdentity


@admin.register(CheckUpIdentity, site=public_admin)
class CheckUpIdentityAdmin(admin.ModelAdmin):
    """Только чтение: личности заводит обмен токена, руками их не правят."""
    list_display = ('checkup_user_id', 'tenant_schema', 'user', 'last_role', 'exchanges_count', 'last_exchanged_at')
    list_filter = ('tenant_schema', 'last_role')
    search_fields = ('checkup_user_id', 'user__username', 'display_name', 'email')
    readonly_fields = [f.name for f in CheckUpIdentity._meta.fields]
    ordering = ('-last_exchanged_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
