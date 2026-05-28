from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from apps.shared.config.admin_sites import public_admin
from .models import User


@admin.register(User, site=public_admin)
class UserPublicAdmin(BaseUserAdmin):
    """Управление всеми пользователями платформы — только для суперадмина."""
    list_display = ('username', 'email', 'role', 'get_companies', 'is_active')
    list_filter = ('role', 'is_active')
    search_fields = ('username', 'email')
    # Переопределяем fieldsets полностью: убираем groups/user_permissions/is_staff,
    # так как они не имеют эффекта — права управляются исключительно через role.
    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Личные данные', {'fields': ('first_name', 'last_name', 'email', 'phone', 'city', 'birthday', 'birthday_set_at')}),
        ('Роль и доступ', {'fields': ('role', 'companies', 'is_active', 'is_superuser')}),
        ('Даты', {'fields': ('last_login', 'date_joined')}),
    )
    add_fieldsets = (
        (None, {'classes': ('wide',), 'fields': ('username', 'password1', 'password2')}),
        ('Роль и доступ', {'fields': ('role', 'companies')}),
    )
    readonly_fields = ('last_login', 'date_joined')

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        # Только is_superuser может выдавать/снимать флаг is_superuser другим
        if not request.user.is_superuser:
            ro.append('is_superuser')
        return ro

    @admin.display(description='Компании')
    def get_companies(self, obj):
        return ', '.join(obj.companies.values_list('name', flat=True)) or '—'

    # ── NETWORK_ADMIN: no access to User model at all ─────────────────────────

    def has_view_permission(self, request, obj=None):
        if getattr(request.user, 'role', None) == 'network_admin':
            return False
        return super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        if getattr(request.user, 'role', None) == 'network_admin':
            return False
        return super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        if getattr(request.user, 'role', None) == 'network_admin':
            return False
        if obj is not None and obj.pk != request.user.pk:
            # is_superuser пользователей не трогает никто кроме самого is_superuser
            if obj.is_superuser and not request.user.is_superuser:
                return False
            # superadmin роль не может менять других superadmin
            if obj.role == User.Role.SUPERADMIN and not request.user.is_superuser:
                return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if getattr(request.user, 'role', None) == 'network_admin':
            return False
        if obj is not None:
            # is_superuser нельзя удалить никому
            if obj.is_superuser:
                return False
            # superadmin роль может удалять только is_superuser
            if obj.role == User.Role.SUPERADMIN and not request.user.is_superuser:
                return False
        return super().has_delete_permission(request, obj)

    def get_actions(self, request):
        actions = super().get_actions(request)
        # Убираем стандартное массовое удаление — слишком опасно
        actions.pop('delete_selected', None)
        return actions

    def delete_queryset(self, request, queryset):
        # Не позволяем удалять SUPERADMIN через bulk-action
        queryset.exclude(role=User.Role.SUPERADMIN).delete()
