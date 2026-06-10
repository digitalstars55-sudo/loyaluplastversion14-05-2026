from django.contrib import admin
from django.db.models import Count
from django.utils.html import format_html, mark_safe

from apps.shared.config.admin_sites import tenant_admin

from .models import Product, ProductBranch, ProductCategory


# ── Style constants ───────────────────────────────────────────────────────────

_BADGE = (
    'display:inline-block;padding:2px 8px;border-radius:10px;'
    'font-size:11px;font-weight:600;white-space:nowrap;'
)
_SUPER_STYLE = _BADGE + 'background:#fff3cd;color:#856404;border:1px solid #ffe08a;'
_BDAY_STYLE  = _BADGE + 'background:#fce4ec;color:#880e4f;border:1px solid #f8bbd0;'
_STORY_STYLE = _BADGE + 'background:#e0f2f1;color:#004d40;border:1px solid #b2dfdb;'
_PRICE_STYLE = _BADGE + 'background:#e3f2fd;color:#0d47a1;border:1px solid #bbdefb;'
_FREE_STYLE  = _BADGE + 'background:#e8f5e9;color:#1b5e20;border:1px solid #c8e6c9;'
_ARCHIVED_STYLE = _BADGE + 'background:#f3f4f6;color:#374151;border:1px solid #d1d5db;'


# ── Inlines ───────────────────────────────────────────────────────────────────

class ProductBranchInline(admin.TabularInline):
    """Manages per-branch settings of a product (used inside ProductAdmin)."""
    model = ProductBranch
    extra = 1
    fields = ('branch', 'category', 'ordering', 'is_active')
    ordering = ('branch', 'ordering')


class ProductBranchFromCategoryInline(admin.TabularInline):
    """Shows product assignments that belong to this category (used inside ProductCategoryAdmin)."""
    model = ProductBranch
    fk_name = 'category'
    extra = 0
    fields = ('product', 'branch', 'ordering', 'is_active')
    ordering = ('ordering',)
    show_change_link = True


# ── ProductCategory admin ─────────────────────────────────────────────────────

@admin.register(ProductCategory, site=tenant_admin)
class ProductCategoryAdmin(admin.ModelAdmin):
    inlines = [ProductBranchFromCategoryInline]
    list_display = ('name', 'branch', 'products_count', 'ordering', 'updated_at')
    list_filter = ('branch',)
    search_fields = ('name',)
    list_select_related = ('branch',)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            products_count=Count('product_assignments'),
        )

    @admin.display(description='Товаров', ordering='products_count')
    def products_count(self, obj):
        return obj.products_count or '—'


# ── Product admin ─────────────────────────────────────────────────────────────

@admin.register(Product, site=tenant_admin)
class ProductAdmin(admin.ModelAdmin):
    inlines = [ProductBranchInline]
    list_display = (
        'image_thumb', 'name', 'branches_display',
        'price_badge', 'flags_badges', 'updated_at',
    )
    list_display_links = ('image_thumb', 'name')
    list_filter = ('is_archived', 'branches', 'is_super_prize', 'is_birthday_prize', 'is_story_prize')
    search_fields = ('name', 'description')
    actions = [
        'mark_super_prize', 'unmark_super_prize',
        'mark_birthday_prize', 'unmark_birthday_prize',
        'mark_story_prize', 'unmark_story_prize',
        'archive_products', 'unarchive_products',
    ]
    readonly_fields = ('image_preview', 'created_at', 'updated_at')

    fieldsets = (
        (None, {
            'fields': ('name', 'description'),
        }),
        ('Изображение', {
            'fields': ('image', 'image_preview'),
        }),
        ('Параметры', {
            'fields': ('price',),
        }),
        ('Сценарии выдачи', {
            'fields': ('is_super_prize', 'is_birthday_prize', 'is_story_prize'),
            'description': (
                'Флаги не взаимоисключающие. '
                'Товар без флагов доступен только для покупки за баллы. '
                '«Подарок для игры через сториз» — отдельный набор для внешних '
                'пользователей, не смешивается с супер-призами основной игры.'
            ),
        }),
        ('Архив', {
            'fields': ('is_archived',),
            'description': (
                'Архивированный подарок скрыт от выдачи на всех точках: магазин, '
                'ДР-пул, супер-пул. Уже выданные гостям подарки в инвентаре '
                'остаются доступными к активации — данные не теряются.'
            ),
        }),
        ('Служебное', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    # ── Queryset ──────────────────────────────────────────────────────────

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('branches')

    # ── List columns ──────────────────────────────────────────────────────

    @admin.display(description='Точки')
    def branches_display(self, obj):
        names = [str(b) for b in obj.branches.all()]
        if not names:
            return mark_safe('<span style="color:var(--body-quiet-color,#aaa);font-size:12px;">—</span>')
        return ', '.join(names)

    @admin.display(description='')
    def image_thumb(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="width:44px;height:44px;'
                'object-fit:cover;border-radius:6px;'
                'border:1px solid var(--border-color,#ddd);" />',
                obj.image.url,
            )
        return mark_safe(
            '<div style="width:44px;height:44px;border-radius:6px;'
            'background:var(--darkened-bg,#f0f0f0);'
            'border:1px solid var(--border-color,#ddd);'
            'display:flex;align-items:center;justify-content:center;'
            'font-size:20px;">🎁</div>'
        )

    @admin.display(description='Цена', ordering='price')
    def price_badge(self, obj):
        if obj.price == 0:
            return format_html('<span style="{}">Бесплатно</span>', _FREE_STYLE)
        return format_html('<span style="{}">{} ★</span>', _PRICE_STYLE, obj.price)

    @admin.display(description='Флаги')
    def flags_badges(self, obj):
        badges = []
        if obj.is_archived:
            badges.append(f'<span style="{_ARCHIVED_STYLE}">🗄️ Архив</span>')
        if obj.is_super_prize:
            badges.append(f'<span style="{_SUPER_STYLE}">🏆 Суперприз</span>')
        if obj.is_birthday_prize:
            badges.append(f'<span style="{_BDAY_STYLE}">🎂 День рождения</span>')
        if obj.is_story_prize:
            badges.append(f'<span style="{_STORY_STYLE}">📱 Сториз</span>')
        if badges:
            return mark_safe('&nbsp;'.join(badges))
        return mark_safe(
            '<span style="color:var(--body-quiet-color,#aaa);font-size:12px;">—</span>'
        )

    @admin.display(description='Фото')
    def image_preview(self, obj):
        if obj.image:
            return format_html(
                '<img src="{}" style="max-width:280px;max-height:280px;'
                'border-radius:8px;border:1px solid var(--border-color,#ddd);" />',
                obj.image.url,
            )
        return '—'

    # ── Actions ───────────────────────────────────────────────────────────

    @admin.action(description='Добавить флаг «Суперприз»')
    def mark_super_prize(self, request, queryset):
        self.message_user(request, f'Суперприз: {queryset.update(is_super_prize=True)}')

    @admin.action(description='Убрать флаг «Суперприз»')
    def unmark_super_prize(self, request, queryset):
        self.message_user(request, f'Флаг снят: {queryset.update(is_super_prize=False)}')

    @admin.action(description='Добавить флаг «Подарок на ДР»')
    def mark_birthday_prize(self, request, queryset):
        self.message_user(request, f'Подарок на ДР: {queryset.update(is_birthday_prize=True)}')

    @admin.action(description='Убрать флаг «Подарок на ДР»')
    def unmark_birthday_prize(self, request, queryset):
        self.message_user(request, f'Флаг снят: {queryset.update(is_birthday_prize=False)}')

    @admin.action(description='Добавить флаг «Подарок для сториз»')
    def mark_story_prize(self, request, queryset):
        self.message_user(request, f'Подарок для сториз: {queryset.update(is_story_prize=True)}')

    @admin.action(description='Убрать флаг «Подарок для сториз»')
    def unmark_story_prize(self, request, queryset):
        self.message_user(request, f'Флаг снят: {queryset.update(is_story_prize=False)}')

    @admin.action(description='🗄️ Архивировать (скрыть от выдачи)')
    def archive_products(self, request, queryset):
        self.message_user(request, f'Архивировано: {queryset.update(is_archived=True)}')

    @admin.action(description='♻️ Восстановить из архива')
    def unarchive_products(self, request, queryset):
        self.message_user(request, f'Восстановлено: {queryset.update(is_archived=False)}')
