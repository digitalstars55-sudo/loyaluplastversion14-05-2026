from django.contrib import admin
from django.db import models
from django.utils import timezone
from django.utils.html import format_html, mark_safe

from apps.shared.config.admin_sites import tenant_admin

from .models import (
    AcquisitionSource, GiftCostEvent, InventoryItem, ItemStatus,
    RewardCatalogItem, RewardTier,
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


# ── GiftCostEvent admin (затраты на подарки, read-only) ───────────────────────

@admin.register(GiftCostEvent, site=tenant_admin)
class GiftCostEventAdmin(admin.ModelAdmin):
    """Снимки затрат на активированные подарки — только для аудита/просмотра."""

    list_display = ('activated_at', 'cost_col', 'kind_col', 'product_col', 'branch', 'client_branch')
    list_filter = ('kind', 'branch', 'activated_at')
    search_fields = ('product__name', 'client_branch__client__vk_id')
    date_hierarchy = 'activated_at'
    readonly_fields = (
        'client_branch', 'product', 'branch', 'kind', 'cost_rub', 'activated_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('product', 'branch', 'client_branch__client')

    @admin.display(description='Себестоимость', ordering='cost_rub')
    def cost_col(self, obj):
        return f'{obj.cost_rub} ₽'

    @admin.display(description='Тип', ordering='kind')
    def kind_col(self, obj):
        return obj.get_kind_display()

    @admin.display(description='Подарок', ordering='product__name')
    def product_col(self, obj):
        if obj.product:
            return obj.product.name
        return mark_safe('<span style="color:var(--body-quiet-color,#aaa);font-style:italic;">—</span>')


# ── Каталог наград ────────────────────────────────────────────────────────────

_TIER_STYLES = {
    RewardTier.G1: _BADGE + 'background:#e8f5e9;color:#1b5e20;border:1px solid #c8e6c9;',
    RewardTier.G2: _BADGE + 'background:#e3f2fd;color:#0d47a1;border:1px solid #bbdefb;',
    RewardTier.G3: _BADGE + 'background:#fff3cd;color:#856404;border:1px solid #ffe08a;',
}
_LIMIT_OK_STYLE   = _BADGE + 'background:#f1f5f9;color:#334155;border:1px solid #cbd5e1;'
_LIMIT_OUT_STYLE  = _BADGE + 'background:#fdecea;color:#b71c1c;border:1px solid #f5c6cb;'
_RFM_ON_STYLE     = _BADGE + 'background:#ede7f6;color:#4527a0;border:1px solid #d1c4e9;'
_OFF_STYLE        = _BADGE + 'background:#f3f4f6;color:#374151;border:1px solid #d1d5db;'
_ON_STYLE         = _BADGE + 'background:#e8f5e9;color:#1b5e20;border:1px solid #c8e6c9;'


class RewardAvailabilityFilter(admin.SimpleListFilter):
    """Быстрый ответ на вопрос «что реально может выпасть прямо сейчас»."""

    title = 'Доступность'
    parameter_name = 'availability'

    def lookups(self, request, model_admin):
        return (
            ('available',   'Может выпасть сейчас'),
            ('limit_out',   'Лимит исчерпан'),
            ('out_of_period', 'Вне периода доступности'),
        )

    def queryset(self, request, queryset):
        now = timezone.now()
        in_period = (
            models.Q(available_from__isnull=True) | models.Q(available_from__lte=now)
        ) & (
            models.Q(available_to__isnull=True) | models.Q(available_to__gte=now)
        )
        has_room = (
            models.Q(activation_limit__isnull=True)
            | models.Q(issued_count__lt=models.F('activation_limit'))
        )

        value = self.value()
        if value == 'available':
            return queryset.filter(
                in_period & has_room, is_active=True, is_archived=False,
            )
        if value == 'limit_out':
            return queryset.filter(
                activation_limit__isnull=False,
                issued_count__gte=models.F('activation_limit'),
            )
        if value == 'out_of_period':
            return queryset.exclude(in_period)
        return queryset


@admin.register(RewardCatalogItem, site=tenant_admin)
class RewardCatalogItemAdmin(admin.ModelAdmin):
    """
    Справочник наград для RFM-кампаний и RF-авторассылок.

    Позиция — надстройка над подарком из каталога: тир, вес выбора, лимит
    и срок жизни. Пустые поля карточки наследуются от выбранного подарка.
    """

    list_display = (
        'image_thumb', 'name_col', 'tier_badge', 'weight_col',
        'issues_col', 'lifetime_col', 'cost_col', 'branch_col',
        'rfm_badge', 'state_badge', 'updated_at',
    )
    list_display_links = ('image_thumb', 'name_col')
    list_filter = (
        'tier', 'is_active', 'is_archived', 'available_for_rfm',
        RewardAvailabilityFilter, 'branch',
    )
    search_fields = ('name', 'internal_code', 'description', 'product__name')
    list_select_related = ('product', 'branch')
    ordering = ('tier', 'name', 'pk')
    readonly_fields = ('issued_count', 'image_preview', 'created_at', 'updated_at')
    actions = [
        'activate_items', 'deactivate_items',
        'archive_items', 'unarchive_items',
        'allow_for_rfm', 'deny_for_rfm',
        'reset_issued_count',
    ]

    fieldsets = (
        (None, {
            'fields': ('product', 'name', 'internal_code', 'description'),
            'description': (
                'Позиция каталога наград — надстройка над подарком. Выберите подарок, '
                'и пустые поля карточки (название, описание, изображение, себестоимость) '
                'подтянутся из него.'
            ),
        }),
        ('Изображение', {
            'fields': ('image', 'image_preview'),
        }),
        ('Экономика', {
            'fields': ('cost_price', 'min_order_amount'),
        }),
        ('Правила выбора', {
            'fields': ('tier', 'weight', 'default_lifetime_days'),
            'description': (
                'Сценарий задаёт тир, а система выбирает позицию внутри тира случайно '
                'с учётом веса: чем больше вес, тем чаще позиция достаётся гостю.'
            ),
        }),
        ('Лимиты и доступность', {
            'fields': (
                'activation_limit', 'issued_count',
                'available_from', 'available_to', 'branch',
            ),
        }),
        ('Флаги', {
            'fields': ('is_active', 'is_archived', 'available_for_rfm'),
        }),
        ('Служебное', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    # ── Колонки ───────────────────────────────────────────────────────────────

    @admin.display(description='')
    def image_thumb(self, obj):
        image = obj.display_image
        if image:
            return format_html(
                '<img src="{}" style="width:44px;height:44px;'
                'object-fit:cover;border-radius:6px;'
                'border:1px solid var(--border-color,#ddd);" />',
                image.url,
            )
        return mark_safe(
            '<div style="width:44px;height:44px;border-radius:6px;'
            'background:var(--darkened-bg,#f0f0f0);'
            'border:1px solid var(--border-color,#ddd);'
            'display:flex;align-items:center;justify-content:center;'
            'font-size:18px;">🎁</div>'
        )

    @admin.display(description='Изображение')
    def image_preview(self, obj):
        image = obj.display_image
        if not image:
            return mark_safe(
                '<span style="color:var(--body-quiet-color,#aaa);font-style:italic;">'
                'Нет изображения — будет использовано изображение подарка.</span>'
            )
        source = 'своё' if obj.image else 'от подарка'
        return format_html(
            '<div><img src="{}" style="max-width:220px;border-radius:8px;'
            'border:1px solid var(--border-color,#ddd);" />'
            '<div style="color:var(--body-quiet-color,#888);font-size:11px;'
            'margin-top:4px;">Источник: {}</div></div>',
            image.url, source,
        )

    @admin.display(description='Название', ordering='name')
    def name_col(self, obj):
        if obj.product_id and not obj.name:
            return format_html(
                '{} <span style="color:var(--body-quiet-color,#888);font-size:11px;">'
                '(из подарка)</span>',
                obj.display_name,
            )
        return obj.display_name

    @admin.display(description='Тир', ordering='tier')
    def tier_badge(self, obj):
        style = _TIER_STYLES.get(obj.tier, _OFF_STYLE)
        return format_html('<span style="{}">{}</span>', style, obj.tier)

    @admin.display(description='Вес', ordering='weight')
    def weight_col(self, obj):
        if not obj.weight:
            return mark_safe(
                '<span style="color:var(--body-quiet-color,#aaa);" title="Позиция выпадет '
                'только если других не осталось.">0</span>'
            )
        return obj.weight

    @admin.display(description='Выдано / лимит', ordering='issued_count')
    def issues_col(self, obj):
        if obj.activation_limit is None:
            return format_html(
                '<span style="{}">{} / ∞</span>', _LIMIT_OK_STYLE, obj.issued_count,
            )
        style = _LIMIT_OUT_STYLE if obj.is_limit_reached else _LIMIT_OK_STYLE
        return format_html(
            '<span style="{}">{} / {}</span>',
            style, obj.issued_count, obj.activation_limit,
        )

    @admin.display(description='Срок', ordering='default_lifetime_days')
    def lifetime_col(self, obj):
        return f'{obj.default_lifetime_days} дн.'

    @admin.display(description='Себестоимость')
    def cost_col(self, obj):
        value = obj.effective_cost_price
        if obj.product_id and not obj.cost_price:
            return format_html(
                '{} ₽ <span style="color:var(--body-quiet-color,#888);font-size:11px;">'
                '(из подарка)</span>', value,
            )
        return f'{value} ₽'

    @admin.display(description='Точка', ordering='branch__name')
    def branch_col(self, obj):
        if obj.branch_id:
            return obj.branch.name
        return mark_safe(
            '<span style="color:var(--body-quiet-color,#888);">Вся сеть</span>'
        )

    @admin.display(description='RFM', ordering='available_for_rfm')
    def rfm_badge(self, obj):
        if obj.available_for_rfm:
            return format_html('<span style="{}">✓ RFM</span>', _RFM_ON_STYLE)
        return format_html('<span style="{}">— не для RFM</span>', _OFF_STYLE)

    @admin.display(description='Состояние')
    def state_badge(self, obj):
        if obj.is_archived:
            return format_html('<span style="{}">📦 Архив</span>', _OFF_STYLE)
        if not obj.is_active:
            return format_html('<span style="{}">⏸ Выключена</span>', _OFF_STYLE)
        if obj.is_limit_reached:
            return format_html('<span style="{}">🚫 Лимит исчерпан</span>', _LIMIT_OUT_STYLE)
        if not obj.is_within_period():
            return format_html('<span style="{}">🕒 Вне периода</span>', _OFF_STYLE)
        return format_html('<span style="{}">✅ Выдаётся</span>', _ON_STYLE)

    # ── Действия ──────────────────────────────────────────────────────────────

    @admin.action(description='Включить позиции')
    def activate_items(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f'Включено позиций: {updated}.')

    @admin.action(description='Выключить позиции')
    def deactivate_items(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f'Выключено позиций: {updated}.')

    @admin.action(description='В архив')
    def archive_items(self, request, queryset):
        updated = queryset.update(is_archived=True)
        self.message_user(request, f'В архив отправлено позиций: {updated}.')

    @admin.action(description='Вернуть из архива')
    def unarchive_items(self, request, queryset):
        updated = queryset.update(is_archived=False)
        self.message_user(request, f'Возвращено из архива позиций: {updated}.')

    @admin.action(description='Разрешить для RFM')
    def allow_for_rfm(self, request, queryset):
        updated = queryset.update(available_for_rfm=True)
        self.message_user(request, f'Разрешено для RFM позиций: {updated}.')

    @admin.action(description='Запретить для RFM')
    def deny_for_rfm(self, request, queryset):
        updated = queryset.update(available_for_rfm=False)
        self.message_user(request, f'Запрещено для RFM позиций: {updated}.')

    @admin.action(description='Обнулить счётчик выдач')
    def reset_issued_count(self, request, queryset):
        updated = queryset.update(issued_count=0)
        self.message_user(
            request,
            f'Счётчик выдач обнулён у позиций: {updated}. '
            'Лимит снова считается с нуля.',
        )
