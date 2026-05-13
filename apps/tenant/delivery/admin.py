from django.contrib import admin
from django.utils import timezone
from django.utils.html import format_html, mark_safe

from apps.shared.config.admin_sites import tenant_admin

from .models import Delivery, OrderSource

# ── Style constants ───────────────────────────────────────────────────────────

_BADGE = (
    'display:inline-block;padding:2px 8px;border-radius:10px;'
    'font-size:11px;font-weight:600;white-space:nowrap;'
)

_PENDING_STYLE   = _BADGE + 'background:#fff8e1;color:#f57f17;border:1px solid #ffe082;'
_ACTIVATED_STYLE = _BADGE + 'background:#e8f5e9;color:#1b5e20;border:1px solid #c8e6c9;'
_EXPIRED_STYLE   = _BADGE + 'background:#fbe9e7;color:#bf360c;border:1px solid #ffab91;'

_IIKO_STYLE    = _BADGE + 'background:#e8eaf6;color:#283593;border:1px solid #9fa8da;'
_DOOGLYS_STYLE = _BADGE + 'background:#e0f2f1;color:#004d40;border:1px solid #80cbc4;'

_STATUS_STYLES = {
    'pending':   _PENDING_STYLE,
    'activated': _ACTIVATED_STYLE,
    'expired':   _EXPIRED_STYLE,
}
_STATUS_LABELS = {
    'pending':   '⏳ Ожидает',
    'activated': '✅ Активирован',
    'expired':   '⌛ Истёк',
}
_SOURCE_STYLES = {
    OrderSource.IIKO:    _IIKO_STYLE,
    OrderSource.DOOGLYS: _DOOGLYS_STYLE,
}


# ── Filters ───────────────────────────────────────────────────────────────────

class DeliveryStatusFilter(admin.SimpleListFilter):
    title = 'Статус'
    parameter_name = 'delivery_status'

    def lookups(self, request, model_admin):
        return [
            ('pending',   '⏳ Ожидает'),
            ('activated', '✅ Активирован'),
            ('expired',   '⌛ Истёк'),
        ]

    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == 'pending':
            return queryset.filter(activated_at__isnull=True, expires_at__gt=now)
        if self.value() == 'activated':
            return queryset.filter(activated_at__isnull=False)
        if self.value() == 'expired':
            return queryset.filter(activated_at__isnull=True, expires_at__lte=now)
        return queryset


# ── Delivery admin ────────────────────────────────────────────────────────────

@admin.register(Delivery, site=tenant_admin)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = (
        'code_col', 'branch', 'source_badge', 'status_badge',
        'time_col', 'activated_by_col', 'created_at',
    )
    list_display_links = ('code_col',)
    list_filter = (DeliveryStatusFilter, 'order_source', 'branch')
    search_fields = ('code', 'short_code', 'activated_by__client__first_name', 'activated_by__client__last_name')
    list_select_related = ('branch', 'activated_by__client')
    date_hierarchy = 'created_at'
    readonly_fields = (
        'short_code', 'status_display', 'activated_at', 'expires_at',
        'created_at', 'updated_at',
    )

    fieldsets = (
        (None, {
            'fields': ('branch', 'order_source', 'code', 'short_code'),
        }),
        ('Активация', {
            'fields': ('duration', 'expires_at', 'status_display', 'activated_at', 'activated_by'),
            'description': (
                'Коды одноразовые и не ограничены по времени до активации. '
                'expires_at ставится на 100 лет вперёд при создании и сбрасывается в now() '
                'после того, как гость использовал код в игре.'
            ),
        }),
        ('Служебное', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    # ── Queryset ──────────────────────────────────────────────────────────

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'branch', 'activated_by__client',
        )

    # ── List columns ──────────────────────────────────────────────────────

    @admin.display(description='Код', ordering='code')
    def code_col(self, obj):
        masked = f'…{obj.short_code}'
        return format_html(
            '<span style="font-family:monospace;font-weight:600;">{}</span>', masked
        )

    @admin.display(description='Источник', ordering='order_source')
    def source_badge(self, obj):
        style = _SOURCE_STYLES.get(obj.order_source, _BADGE)
        label = obj.get_order_source_display()
        return format_html('<span style="{}">{}</span>', style, label)

    @admin.display(description='Статус')
    def status_badge(self, obj):
        status = obj.status
        style  = _STATUS_STYLES.get(status, _PENDING_STYLE)
        label  = _STATUS_LABELS.get(status, status)
        return format_html('<span style="{}">{}</span>', style, label)

    @admin.display(description='Время', ordering='expires_at')
    def time_col(self, obj):
        status = obj.status
        if status == 'pending':
            # Коды больше не ограничены по времени до активации.
            return format_html(
                '<span style="color:#f57f17;font-weight:600;" title="Без ограничения по времени">∞</span>'
            )
        if status == 'activated':
            if obj.is_active_window:
                return format_html(
                    '<span style="color:#1b5e20;font-weight:600;" title="Не использовано в игре">✓</span>'
                )
        return mark_safe('<span style="color:var(--body-quiet-color,#aaa);">—</span>')

    @admin.display(description='Активировал', ordering='activated_by__client__first_name')
    def activated_by_col(self, obj):
        if not obj.activated_by_id:
            return mark_safe('<span style="color:var(--body-quiet-color,#aaa);">—</span>')
        c = obj.activated_by.client
        return c.first_name or c.phone

    @admin.display(description='Статус')
    def status_display(self, obj):
        if not obj.pk:
            return '—'
        status = obj.status
        style  = _STATUS_STYLES.get(status, _PENDING_STYLE)
        label  = _STATUS_LABELS.get(status, status)
        return format_html('<span style="{}">{}</span>', style, label)
