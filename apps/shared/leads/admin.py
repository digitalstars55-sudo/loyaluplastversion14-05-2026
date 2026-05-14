"""
Регистрация Lead-модели в супер-админке.

Аддитивно: используем существующий public_admin сайт. Наша модель просто
появляется в списке моделей супер-админа.
"""

from django.contrib import admin, messages
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from apps.shared.config.admin_sites import public_admin

from .models import Lead, LeadStatus


@admin.register(Lead, site=public_admin)
class LeadAdmin(admin.ModelAdmin):
    list_display = (
        'cafe_name', 'cafe_count', 'package_suggested', 'full_name',
        'email', 'status_pill', 'created_at',
    )
    list_filter = ('status', 'created_at')
    search_fields = ('cafe_name', 'full_name', 'email', 'domain_slug')
    readonly_fields = (
        'session_token', 'created_at', 'updated_at',
        'submitted_at', 'confirmed_at', 'rejected_at',
        'confirmed_by', 'company',
        'conversation_history_pretty',
    )
    fieldsets = (
        ('Заявка', {
            'fields': ('status', 'session_token'),
        }),
        ('Кафе', {
            'fields': ('cafe_name', 'cafe_count', 'traffic_estimate', 'package_suggested'),
        }),
        ('Клиент', {
            'fields': ('full_name', 'email', 'vk_token', 'domain_slug'),
        }),
        ('AI-чат', {
            'fields': ('conversation_history_pretty',),
            'classes': ('collapse',),
        }),
        ('Подтверждение', {
            'fields': (
                'submitted_at', 'confirmed_at', 'confirmed_by', 'company',
                'rejected_at', 'rejection_reason',
            ),
        }),
        ('Технические', {
            'fields': ('created_at', 'updated_at', 'notified_super_admin'),
            'classes': ('collapse',),
        }),
    )
    actions = ['action_confirm', 'action_reject']

    def status_pill(self, obj):
        colors = {
            LeadStatus.DRAFT:     '#A1A1AA',
            LeadStatus.SUBMITTED: '#A855F7',
            LeadStatus.CONFIRMED: '#16A34A',
            LeadStatus.REJECTED:  '#DC2626',
        }
        return format_html(
            '<span style="background:{};color:#fff;padding:3px 8px;'
            'border-radius:999px;font-size:11px;font-weight:700;">{}</span>',
            colors.get(obj.status, '#A1A1AA'),
            obj.get_status_display(),
        )
    status_pill.short_description = 'Статус'

    def conversation_history_pretty(self, obj):
        rows = []
        for msg in (obj.conversation_history or []):
            role = msg.get('role', '?')
            color = '#A855F7' if role == 'assistant' else '#16A34A'
            text = (msg.get('text') or '').replace('\n', '<br>')
            rows.append(format_html(
                '<div style="margin-bottom:8px"><b style="color:{}">{}:</b><br>{}</div>',
                color, role, text,
            ))
        return format_html('<div>{}</div>', format_html(''.join(str(r) for r in rows)) if rows else 'Пусто')
    conversation_history_pretty.short_description = 'История чата'

    # ── Actions ──────────────────────────────────────────────────────
    def action_confirm(self, request, queryset):
        """
        Pack F4: подтверждение → автоматически создаёт тенант (Company +
        Domain + User + Secrets), отправляет email с creds клиенту,
        уведомляет супер-админа.
        """
        from .provisioning import ProvisioningError, create_tenant_from_lead

        ok = 0
        errors = []
        for lead in queryset:
            if lead.status != LeadStatus.SUBMITTED:
                continue
            try:
                company = create_tenant_from_lead(lead, by_user=request.user)
                ok += 1
                messages.success(
                    request,
                    f'✓ «{lead.cafe_name}» → Company #{company.pk} ({company.schema_name}). '
                    f'Email с creds отправлен на {lead.email}.',
                )
            except ProvisioningError as e:
                errors.append(f'{lead.cafe_name}: {e}')
            except Exception as e:
                errors.append(f'{lead.cafe_name}: неожиданная ошибка — {e}')

        if errors:
            for err in errors:
                messages.error(request, f'❌ {err}')
        if not ok and not errors:
            messages.warning(request, 'Нет лидов в статусе «Submitted».')
    action_confirm.short_description = 'Подтвердить заявки и создать тенант'

    def action_reject(self, request, queryset):
        ok = queryset.filter(status=LeadStatus.SUBMITTED).update(
            status=LeadStatus.REJECTED,
            rejected_at=timezone.now(),
        )
        messages.info(request, f'Отклонено {ok}.')
    action_reject.short_description = 'Отклонить выбранные заявки'
