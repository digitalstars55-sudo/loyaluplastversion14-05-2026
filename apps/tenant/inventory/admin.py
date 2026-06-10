from django.contrib import admin
from django.db import models
from django.utils import timezone
from django.utils.html import format_html, mark_safe

from apps.shared.config.admin_sites import tenant_admin

from .models import (
    AcquisitionSource, InventoryItem, ItemStatus,
    StoryGiftEntry, StoryStatus,
    SuperPrizeEntry, SuperPrizeTrigger,
)

# ── Style constants ───────────────────────────────────────────────────────────

_BADGE = (
    'display:inline-block;padding:2px 8px;border-radius:10px;'
    'font-size:11px;font-weight:600;white-space:nowrap;'
)

_PENDING_STYLE  = _BADGE + 'background:#f5f5f5;color:#616161;border:1px solid #e0e0e0;'
_ACTIVE_STYLE   = _BADGE + 'background:#fff8e1;color:#f57f17;border:1px solid #ffe082;'
_USED_STYLE     = _BADGE + 'background:#e3f2fd;color:#0d47a1;border:1px solid #bbdefb;'

_SRC_PURCHASE = _BADGE + 'background:#f3e5f5;color:#4a148c;border:1px solid #e1bee7;'
_SRC_SUPER    = _BADGE + 'background:#fff3cd;color:#856404;border:1px solid #ffe08a;'
_SRC_BIRTHDAY = _BADGE + 'background:#fce4ec;color:#880e4f;border:1px solid #f8bbd0;'
_SRC_MANUAL   = _BADGE + 'background:#e8eaf6;color:#1a237e;border:1px solid #c5cae9;'

_SP_PENDING_STYLE = _BADGE + 'background:#fff8e1;color:#f57f17;border:1px solid #ffe082;'
_SP_CLAIMED_STYLE = _BADGE + 'background:#e8f5e9;color:#2e7d32;border:1px solid #a5d6a7;'
_SP_ISSUED_STYLE  = _BADGE + 'background:#e3f2fd;color:#0d47a1;border:1px solid #90caf9;'
_SP_EXPIRED_STYLE = _BADGE + 'background:#fbe9e7;color:#bf360c;border:1px solid #ffab91;'

_SP_STATUS_STYLES = {
    'pending': _SP_PENDING_STYLE,
    'claimed': _SP_CLAIMED_STYLE,
    'issued':  _SP_ISSUED_STYLE,
    'expired': _SP_EXPIRED_STYLE,
}
_SP_STATUS_LABELS = {
    'pending': '⏳ Ожидает выбора',
    'claimed': '⏱ Выбрал, ждёт выдачи',
    'issued':  '🏆 Получил суперприз',
    'expired': '❌ Не получил (истёк)',
}

_TRIGGER_STYLES = {
    SuperPrizeTrigger.GAME:     _BADGE + 'background:#e8eaf6;color:#283593;border:1px solid #9fa8da;',
    SuperPrizeTrigger.MANUAL:   _SRC_MANUAL,
    SuperPrizeTrigger.BIRTHDAY: _SRC_BIRTHDAY,
}
_TRIGGER_ICONS = {
    SuperPrizeTrigger.GAME:     '🎮',
    SuperPrizeTrigger.MANUAL:   '👤',
    SuperPrizeTrigger.BIRTHDAY: '🎂',
}


_SOURCE_STYLES = {
    AcquisitionSource.PURCHASE:    _SRC_PURCHASE,
    AcquisitionSource.SUPER_PRIZE: _SRC_SUPER,
    AcquisitionSource.BIRTHDAY:    _SRC_BIRTHDAY,
    AcquisitionSource.MANUAL:      _SRC_MANUAL,
}
_SOURCE_ICONS = {
    AcquisitionSource.PURCHASE:    '💰',
    AcquisitionSource.SUPER_PRIZE: '🏆',
    AcquisitionSource.BIRTHDAY:    '🎂',
    AcquisitionSource.MANUAL:      '👤',
}


# ── Custom filters ────────────────────────────────────────────────────────────

class StatusFilter(admin.SimpleListFilter):
    title = 'Статус'
    parameter_name = 'status'

    def lookups(self, request, model_admin):
        return [
            ('not_used', '⏳ Не использован'),
            ('active',   '⏱ Активирован'),
            ('used',     '✅ Использован'),
        ]

    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == 'not_used':
            return queryset.filter(activated_at__isnull=True)
        if self.value() == 'active':
            return queryset.filter(
                activated_at__isnull=False,
                used_at__isnull=True,
            ).filter(models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now))
        if self.value() == 'used':
            return queryset.filter(
                models.Q(used_at__isnull=False) |
                models.Q(activated_at__isnull=False, used_at__isnull=True, expires_at__lte=now)
            )
        return queryset


# ── InventoryItem admin ───────────────────────────────────────────────────────

@admin.register(InventoryItem, site=tenant_admin)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = (
        'product_thumb', 'product_col', 'client_col', 'branch_col',
        'source_badge', 'status_badge', 'time_col', 'created_at',
    )
    list_display_links = ('product_thumb', 'product_col')
    list_filter = (
        StatusFilter,
        'acquired_from',
        'client_branch__branch',
        'product__branch_assignments__category',
    )
    search_fields = (
        'client_branch__client__first_name',
        'client_branch__client__last_name',
        'client_branch__client__vk_id',
        'product__name',
    )
    list_select_related = (
        'client_branch__client',
        'client_branch__branch',
        'product',
    )
    date_hierarchy = 'created_at'
    actions = ['action_mark_used', 'action_reset_activation']
    readonly_fields = (
        'status_display', 'activated_at', 'expires_at', 'used_at',
        'created_at', 'updated_at',
    )

    fieldsets = (
        (None, {
            'fields': ('client_branch', 'product', 'acquired_from', 'description'),
        }),
        ('Активация', {
            'fields': ('duration', 'status_display', 'activated_at', 'expires_at', 'used_at'),
            'description': (
                'duration — окно в минутах, в течение которого приз действителен '
                'после активации. 0 — без ограничения.'
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
            'client_branch__client',
            'client_branch__branch',
            'product',
        )

    # ── List columns ──────────────────────────────────────────────────────

    @admin.display(description='')
    def product_thumb(self, obj):
        if obj.product and obj.product.image:
            return format_html(
                '<img src="{}" style="width:40px;height:40px;'
                'object-fit:cover;border-radius:6px;'
                'border:1px solid var(--border-color,#ddd);" />',
                obj.product.image.url,
            )
        return mark_safe(
            '<div style="width:40px;height:40px;border-radius:6px;'
            'background:var(--darkened-bg,#f0f0f0);'
            'border:1px solid var(--border-color,#ddd);'
            'display:flex;align-items:center;justify-content:center;'
            'font-size:18px;">🎁</div>'
        )

    @admin.display(description='Приз', ordering='product__name')
    def product_col(self, obj):
        return obj.product.name if obj.product else 'Удалено'

    @admin.display(description='Гость', ordering='client_branch__client__first_name')
    def client_col(self, obj):
        c = obj.client_branch.client
        name = f'{c.first_name} {c.last_name}'.strip() or f'vk{c.vk_id}'
        return format_html(
            '<a href="https://vk.com/id{}" target="_blank" rel="noopener">'
            '{}</a>'
            '<br><span style="font-size:11px;color:var(--body-quiet-color,#aaa);">'
            'vk{}</span>',
            c.vk_id, name, c.vk_id,
        )

    @admin.display(description='Точка', ordering='client_branch__branch__name')
    def branch_col(self, obj):
        return obj.client_branch.branch.name

    @admin.display(description='Источник')
    def source_badge(self, obj):
        style = _SOURCE_STYLES.get(obj.acquired_from, _SRC_MANUAL)
        icon  = _SOURCE_ICONS.get(obj.acquired_from, '')
        label = obj.get_acquired_from_display()
        return format_html('<span style="{}">{} {}</span>', style, icon, label)

    @admin.display(description='Статус')
    def status_badge(self, obj):
        s = obj.status
        if s == ItemStatus.ACTIVE:
            return format_html('<span style="{}">⏱ Активирован</span>', _ACTIVE_STYLE)
        if s in (ItemStatus.USED, ItemStatus.EXPIRED):
            return format_html('<span style="{}">✅ Использован</span>', _USED_STYLE)
        return format_html('<span style="{}">⏳ Не использован</span>', _PENDING_STYLE)

    @admin.display(description='Время')
    def time_col(self, obj):
        status = obj.status
        if status == ItemStatus.ACTIVE and obj.expires_at:
            remaining = obj.expires_at - timezone.now()
            mins = max(0, int(remaining.total_seconds()) // 60)
            return format_html(
                '<span style="color:#1b5e20;font-weight:600;">⏱ {} мин</span>', mins
            )
        if status == ItemStatus.PENDING:
            if obj.duration:
                return format_html(
                    '<span style="color:#757575;">{} мин</span>', obj.duration
                )
            return mark_safe('<span style="color:#757575;">∞</span>')
        return mark_safe('<span style="color:var(--body-quiet-color,#aaa);">—</span>')

    @admin.display(description='Статус')
    def status_display(self, obj):
        if not obj.pk:
            return '—'
        s = obj.status
        if s == ItemStatus.ACTIVE:
            return format_html('<span style="{}">⏱ Активирован</span>', _ACTIVE_STYLE)
        if s in (ItemStatus.USED, ItemStatus.EXPIRED):
            return format_html('<span style="{}">✅ Использован</span>', _USED_STYLE)
        return format_html('<span style="{}">⏳ Не использован</span>', _PENDING_STYLE)

    # ── Actions ───────────────────────────────────────────────────────────

    @admin.action(description='Отметить как использованный')
    def action_mark_used(self, request, queryset):
        now = timezone.now()
        active_qs = queryset.filter(
            activated_at__isnull=False,
            used_at__isnull=True,
        ).filter(
            models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now)
        )
        count = active_qs.update(used_at=now)
        self.message_user(request, f'Отмечено как использованных: {count}')

    @admin.action(description='Сбросить активацию')
    def action_reset_activation(self, request, queryset):
        count = queryset.filter(used_at__isnull=True).update(
            activated_at=None,
            expires_at=None,
        )
        self.message_user(request, f'Активация сброшена: {count}')


# ── SuperPrizeEntry filters ───────────────────────────────────────────────────

class SuperPrizeStatusFilter(admin.SimpleListFilter):
    title = 'Статус'
    parameter_name = 'sp_status'

    def lookups(self, request, model_admin):
        return [
            ('pending', '⏳ Ожидает выбора'),
            ('claimed', '⏱ Выбрал, ждёт выдачи'),
            ('issued',  '🏆 Получил суперприз'),
            ('expired', '❌ Не получил (истёк)'),
        ]

    def queryset(self, request, queryset):
        now = timezone.now()
        if self.value() == 'pending':
            return queryset.filter(
                claimed_at__isnull=True,
            ).filter(
                models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now)
            )
        if self.value() == 'claimed':
            return queryset.filter(claimed_at__isnull=False, issued_at__isnull=True)
        if self.value() == 'issued':
            return queryset.filter(issued_at__isnull=False)
        if self.value() == 'expired':
            return queryset.filter(claimed_at__isnull=True, expires_at__lte=now)
        return queryset


# ── SuperPrizeEntry admin ─────────────────────────────────────────────────────

@admin.register(SuperPrizeEntry, site=tenant_admin)
class SuperPrizeEntryAdmin(admin.ModelAdmin):
    list_display = (
        'client_col', 'branch_col', 'trigger_badge',
        'product_col', 'sp_status_badge', 'expires_col', 'created_at',
    )
    list_display_links = ('client_col',)
    list_filter = (
        SuperPrizeStatusFilter,
        'acquired_from',
        'client_branch__branch',
    )
    search_fields = (
        'client_branch__client__first_name',
        'client_branch__client__last_name',
        'client_branch__client__vk_id',
        'product__name',
    )
    list_select_related = (
        'client_branch__client',
        'client_branch__branch',
        'product',
    )
    date_hierarchy = 'created_at'
    actions = ['action_mark_issued', 'action_reset_claim']
    readonly_fields = (
        'sp_status_display', 'claimed_at', 'issued_at',
        'created_at', 'updated_at',
    )

    fieldsets = (
        (None, {
            'fields': ('client_branch', 'acquired_from', 'description'),
        }),
        ('Выбор приза', {
            'fields': ('product', 'expires_at', 'sp_status_display', 'claimed_at', 'issued_at'),
            'description': (
                'product заполняется автоматически когда гость делает выбор в приложении. '
                'expires_at — крайний срок для выбора. Пусто — бессрочно.'
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
            'client_branch__client',
            'client_branch__branch',
            'product',
        )

    # ── List columns ──────────────────────────────────────────────────────

    @admin.display(description='Гость', ordering='client_branch__client__first_name')
    def client_col(self, obj):
        c = obj.client_branch.client
        name = f'{c.first_name} {c.last_name}'.strip() or f'vk{c.vk_id}'
        return format_html(
            '<a href="https://vk.com/id{}" target="_blank" rel="noopener">'
            '{}</a>'
            '<br><span style="font-size:11px;color:var(--body-quiet-color,#aaa);">'
            'vk{}</span>',
            c.vk_id, name, c.vk_id,
        )

    @admin.display(description='Точка', ordering='client_branch__branch__name')
    def branch_col(self, obj):
        return obj.client_branch.branch.name

    @admin.display(description='Источник')
    def trigger_badge(self, obj):
        style = _TRIGGER_STYLES.get(obj.acquired_from, _SRC_MANUAL)
        icon  = _TRIGGER_ICONS.get(obj.acquired_from, '')
        label = obj.get_acquired_from_display()
        return format_html('<span style="{}">{} {}</span>', style, icon, label)

    @admin.display(description='Выбранный приз', ordering='product__name')
    def product_col(self, obj):
        if obj.product:
            return obj.product.name
        return mark_safe('<span style="color:var(--body-quiet-color,#aaa);font-style:italic;">не выбран</span>')

    @admin.display(description='Статус')
    def sp_status_badge(self, obj):
        status = obj.status
        style  = _SP_STATUS_STYLES.get(status, _SP_PENDING_STYLE)
        label  = _SP_STATUS_LABELS.get(status, status)
        return format_html('<span style="{}">{}</span>', style, label)

    @admin.display(description='Срок', ordering='expires_at')
    def expires_col(self, obj):
        if not obj.expires_at:
            return mark_safe('<span style="color:#757575;">∞</span>')
        if obj.status == 'pending':
            delta = obj.expires_at - timezone.now()
            days = delta.days
            if days < 0:
                return mark_safe('<span style="color:#bf360c;font-weight:600;">истёк</span>')
            if days == 0:
                return mark_safe('<span style="color:#e65100;font-weight:600;">сегодня</span>')
            return format_html(
                '<span style="color:#f57f17;font-weight:600;">{} дн.</span>', days
            )
        return mark_safe('<span style="color:var(--body-quiet-color,#aaa);">—</span>')

    @admin.display(description='Статус')
    def sp_status_display(self, obj):
        if not obj.pk:
            return '—'
        status = obj.status
        style  = _SP_STATUS_STYLES.get(status, _SP_PENDING_STYLE)
        label  = _SP_STATUS_LABELS.get(status, status)
        return format_html('<span style="{}">{}</span>', style, label)

    # ── Actions ───────────────────────────────────────────────────────────

    @admin.action(description='Отметить как выданный')
    def action_mark_issued(self, request, queryset):
        now = timezone.now()
        count = queryset.filter(
            claimed_at__isnull=False,
            issued_at__isnull=True,
        ).update(issued_at=now)
        self.message_user(request, f'Отмечено как выданных: {count}')

    @admin.action(description='Сбросить выбор приза')
    def action_reset_claim(self, request, queryset):
        count = queryset.filter(issued_at__isnull=True).update(
            product=None,
            claimed_at=None,
        )
        self.message_user(request, f'Выбор сброшен: {count}')


# ── StoryGiftEntry admin ──────────────────────────────────────────────────────

_STORY_STATUS_STYLES = {
    StoryStatus.AVAILABLE_TO_PLAY:  _BADGE + 'background:#f5f5f5;color:#616161;border:1px solid #e0e0e0;',
    StoryStatus.GAME_PLAYED:        _BADGE + 'background:#e8eaf6;color:#283593;border:1px solid #9fa8da;',
    StoryStatus.GIFT_SELECTED:      _BADGE + 'background:#e0f7fa;color:#006064;border:1px solid #80deea;',
    StoryStatus.WAITING_CAFE_VISIT: _BADGE + 'background:#fff8e1;color:#f57f17;border:1px solid #ffe082;',
    StoryStatus.ACTIVATED:          _BADGE + 'background:#e8f5e9;color:#2e7d32;border:1px solid #a5d6a7;',
    StoryStatus.EXPIRED:            _BADGE + 'background:#fbe9e7;color:#bf360c;border:1px solid #ffab91;',
    StoryStatus.USED:               _BADGE + 'background:#e3f2fd;color:#0d47a1;border:1px solid #90caf9;',
}


class StoryStatusFilter(admin.SimpleListFilter):
    title = 'Статус'
    parameter_name = 'story_status'

    def lookups(self, request, model_admin):
        return [(s.value, s.label) for s in StoryStatus]

    def queryset(self, request, queryset):
        v = self.value()
        now = timezone.now()
        if v == StoryStatus.AVAILABLE_TO_PLAY:
            return queryset.filter(played_at__isnull=True)
        if v == StoryStatus.GAME_PLAYED:
            return queryset.filter(played_at__isnull=False, selected_at__isnull=True)
        if v == StoryStatus.GIFT_SELECTED:
            return queryset.filter(selected_at__isnull=False, received_at__isnull=True)
        if v == StoryStatus.WAITING_CAFE_VISIT:
            return queryset.filter(received_at__isnull=False, activated_at__isnull=True)
        if v == StoryStatus.ACTIVATED:
            return queryset.filter(
                activated_at__isnull=False, used_at__isnull=True,
            ).filter(models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now))
        if v == StoryStatus.EXPIRED:
            return queryset.filter(
                activated_at__isnull=False, used_at__isnull=True, expires_at__lte=now,
            )
        if v == StoryStatus.USED:
            return queryset.filter(used_at__isnull=False)
        return queryset


@admin.register(StoryGiftEntry, site=tenant_admin)
class StoryGiftEntryAdmin(admin.ModelAdmin):
    """Подарки, выигранные внешними пользователями в игре из сториз."""
    list_display = (
        'client_col', 'branch_col', 'invited_by_col',
        'product_col', 'story_status_badge', 'received_at', 'activated_at',
    )
    list_display_links = ('client_col',)
    list_filter = (StoryStatusFilter, 'client_branch__branch')
    search_fields = (
        'client_branch__client__first_name',
        'client_branch__client__last_name',
        'client_branch__client__vk_id',
        'product__name',
    )
    list_select_related = (
        'client_branch__client',
        'client_branch__branch',
        'client_branch__invited_by__client',
        'product',
    )
    date_hierarchy = 'created_at'
    readonly_fields = (
        'story_status_display', 'played_at', 'selected_at', 'received_at',
        'activated_at', 'expires_at', 'used_at', 'created_at', 'updated_at',
    )

    fieldsets = (
        (None, {
            'fields': ('client_branch', 'product', 'campaign_key'),
        }),
        ('Условия (снимок)', {
            'fields': ('duration', 'min_order_amount'),
        }),
        ('Жизненный цикл', {
            'fields': (
                'story_status_display', 'played_at', 'selected_at', 'received_at',
                'activated_at', 'expires_at', 'used_at',
            ),
            'description': (
                'activated_at заполняется ТОЛЬКО при активации в кафе (после кода дня). '
                'received_at → метрика «Получили через сториз», '
                'activated_at → метрика «Активировали через сториз».'
            ),
        }),
        ('Служебное', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'client_branch__client',
            'client_branch__branch',
            'client_branch__invited_by__client',
            'product',
        )

    @admin.display(description='Гость', ordering='client_branch__client__first_name')
    def client_col(self, obj):
        c = obj.client_branch.client
        name = f'{c.first_name} {c.last_name}'.strip() or f'vk{c.vk_id}'
        return format_html(
            '<a href="https://vk.com/id{}" target="_blank" rel="noopener">{}</a>'
            '<br><span style="font-size:11px;color:var(--body-quiet-color,#aaa);">vk{}</span>',
            c.vk_id, name, c.vk_id,
        )

    @admin.display(description='Точка', ordering='client_branch__branch__name')
    def branch_col(self, obj):
        return obj.client_branch.branch.name

    @admin.display(description='Из сториз кого')
    def invited_by_col(self, obj):
        inviter = obj.client_branch.invited_by
        if not inviter:
            return mark_safe('<span style="color:var(--body-quiet-color,#aaa);">—</span>')
        c = inviter.client
        name = f'{c.first_name} {c.last_name}'.strip() or f'vk{c.vk_id}'
        return format_html(
            '<a href="https://vk.com/id{}" target="_blank" rel="noopener">{}</a>',
            c.vk_id, name,
        )

    @admin.display(description='Подарок', ordering='product__name')
    def product_col(self, obj):
        if obj.product:
            return obj.product.name
        return mark_safe('<span style="color:var(--body-quiet-color,#aaa);font-style:italic;">не выбран</span>')

    @admin.display(description='Статус')
    def story_status_badge(self, obj):
        style = _STORY_STATUS_STYLES.get(obj.status, _PENDING_STYLE)
        return format_html('<span style="{}">{}</span>', style, obj.status_label)

    @admin.display(description='Статус')
    def story_status_display(self, obj):
        if not obj.pk:
            return '—'
        style = _STORY_STATUS_STYLES.get(obj.status, _PENDING_STYLE)
        return format_html('<span style="{}">{}</span>', style, obj.status_label)
