from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.http import HttpResponseRedirect
from django.shortcuts import render, get_object_or_404
from django.urls import path, reverse
from django.utils.html import format_html

from apps.shared.config.admin_sites import public_admin
from .models import User


@admin.register(User, site=public_admin)
class UserPublicAdmin(BaseUserAdmin):
    """Управление всеми пользователями платформы — только для суперадмина."""
    list_display = ('username', 'email', 'role', 'get_companies', 'branch_access_link', 'is_active')
    list_filter = ('role', 'is_active')
    search_fields = ('username', 'email')
    # Переопределяем fieldsets полностью: убираем groups/user_permissions/is_staff,
    # так как они не имеют эффекта — права управляются исключительно через role.
    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Личные данные', {'fields': ('first_name', 'last_name', 'email', 'phone', 'city', 'birthday', 'birthday_set_at')}),
        ('Роль и доступ', {'fields': ('role', 'companies', 'branch_access_summary', 'is_active', 'is_superuser')}),
        ('Даты', {'fields': ('last_login', 'date_joined')}),
    )
    add_fieldsets = (
        (None, {'classes': ('wide',), 'fields': ('username', 'password1', 'password2')}),
        ('Роль и доступ', {'fields': ('role', 'companies')}),
    )
    readonly_fields = ('last_login', 'date_joined', 'branch_access_summary')

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        # Только is_superuser может выдавать/снимать флаг is_superuser другим
        if not request.user.is_superuser:
            ro.append('is_superuser')
        return ro

    @admin.display(description='Компании')
    def get_companies(self, obj):
        return ', '.join(obj.companies.values_list('name', flat=True)) or '—'

    @admin.display(description='Доступ к точкам')
    def branch_access_link(self, obj):
        if obj.is_superuser:
            return format_html('<span style="color:#999">все (SU)</span>')
        access = obj.branch_access or {}
        if not access:
            return format_html('<span style="color:#999">все (по компаниям)</span>')
        parts = []
        for schema, val in access.items():
            if val == 'all':
                parts.append(f'<b>{schema}</b>: все')
            elif isinstance(val, list):
                parts.append(f'<b>{schema}</b>: {len(val)}')
            else:
                parts.append(f'<b>{schema}</b>: ?')
        return format_html(', '.join(parts))

    @admin.display(description='Точки (визуальный редактор)')
    def branch_access_summary(self, obj):
        if not obj.pk:
            return format_html('<i>Сначала сохраните пользователя</i>')
        if obj.is_superuser:
            return format_html('<span style="color:#666">SU видит ВСЕ точки во всех тенантах — branch_access игнорируется.</span>')
        url = reverse('admin:users_user_branch_access', args=[obj.pk])
        access = obj.branch_access or {}
        summary = ', '.join(
            f'{s}: {"все" if v == "all" else f"{len(v)} точек" if isinstance(v, list) else "?"}'
            for s, v in access.items()
        ) or 'все точки в каждой компании (по умолчанию)'
        return format_html(
            '<div style="margin-bottom:6px">{}</div>'
            '<a class="button" href="{}">⚙ Настроить доступ к точкам</a>',
            summary, url,
        )

    # ── Custom URL: /admin/users/user/<id>/branch-access/ ────────────────────

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                '<int:user_id>/branch-access/',
                self.admin_site.admin_view(self.branch_access_view),
                name='users_user_branch_access',
            ),
        ]
        return custom + urls

    def branch_access_view(self, request, user_id):
        """
        Per-tenant + per-branch RBAC редактор. Для каждого тенанта из user.companies
        показывает список точек с чекбоксами + master-чекбокс «Все точки».
        Точки живут в tenant schema → используем schema_context для каждой.
        """
        from django_tenants.utils import schema_context

        user = get_object_or_404(User, pk=user_id)
        access = dict(user.branch_access or {})

        # Собираем для каждой компании список branch'ей через schema_context
        tenants_data = []
        for company in user.companies.order_by('name'):
            schema = company.schema_name
            try:
                with schema_context(schema):
                    from apps.tenant.branch.models import Branch
                    branches = list(
                        Branch.objects.filter(is_active=True).order_by('name')
                        .values('id', 'name')
                    )
            except Exception:
                branches = []
            val = access.get(schema)
            all_on = val == 'all' or val is None
            allowed_set = set(val) if isinstance(val, list) else set()
            tenants_data.append({
                'schema': schema,
                'name': company.name,
                'branches': branches,
                'all_on': all_on,
                'allowed_set': allowed_set,
            })

        if request.method == 'POST':
            new_access: dict = {}
            for tenant in tenants_data:
                schema = tenant['schema']
                all_key = f'all_{schema}'
                if request.POST.get(all_key):
                    new_access[schema] = 'all'
                else:
                    ids = []
                    for br in tenant['branches']:
                        if request.POST.get(f'br_{schema}_{br["id"]}'):
                            ids.append(int(br['id']))
                    if ids:
                        new_access[schema] = ids
                    # если пусто — не записываем ключ → НЕТ доступа к точкам этого тенанта,
                    # но companies всё ещё содержит тенант → user видит нулевой список
            user.branch_access = new_access
            user.save(update_fields=['branch_access'])
            messages.success(request, f'Сохранены доступы к точкам для {user.username}.')
            return HttpResponseRedirect(reverse('admin:users_user_change', args=[user_id]))

        ctx = {
            **self.admin_site.each_context(request),
            'title': f'Доступ к точкам — {user.username}',
            'user_obj': user,
            'tenants': tenants_data,
            'opts': self.model._meta,
        }
        return render(request, 'admin/users/branch_access.html', ctx)

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
