import random

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.db.models import Count, OuterRef, Q, Subquery, Sum
from django.http import HttpResponseRedirect
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, mark_safe
from datetime import timedelta

from apps.shared.config.admin_sites import tenant_admin
from .models import (
    Branch, BranchConfig, ClientBranch, ClientBranchVisit, ClientVKStatus,
    CoinTransaction, TransactionSource, TransactionType,
    Cooldown, CooldownFeature,
    DailyCode, DailyCodePurpose, Promotions,
    TestimonialConversation, TestimonialMessage,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tenant_pos_type(request):
    """Returns the POSType value configured for the current tenant, or None."""
    from apps.shared.config.models import ClientConfig
    try:
        tenant = getattr(request, 'tenant', None)
        if tenant:
            return ClientConfig.objects.get(company=tenant).pos_type
    except ClientConfig.DoesNotExist:
        pass
    return None


# ── BranchConfig inline ───────────────────────────────────────────────────────

class BranchConfigInline(admin.StackedInline):
    model = BranchConfig
    extra = 0
    max_num = 1
    can_delete = False
    verbose_name_plural = 'Настройки точки'

    fieldsets = (
        ('Контакты', {
            'fields': ('address', 'phone'),
        }),
        ('Карты', {
            'fields': ('yandex_map', 'gis_map'),
            'description': 'Ссылки открываются как кнопки в мобильном приложении.',
        }),
        ('Сообщения для гостей (override уровня точки)', {
            'fields': ('code_prompt_message', 'quest_show_message'),
            'classes': ('collapse',),
            'description': (
                'Эти поля перезаписывают сообщения из общих настроек сети '
                'ТОЛЬКО для этой точки. Оставьте пустыми, чтобы использовать '
                'общий текст сети.'
            ),
        }),
        ('Подарок ко дню рождения', {
            'fields': ('birthday_window_days',),
            'description': (
                'Сколько дней до и после ДР гость может забрать подарок. '
                'Каждая точка настраивается независимо.'
            ),
        }),
        ('Игра через сториз (override уровня точки)', {
            'fields': (
                'story_game_enabled', 'story_min_order_amount',
                'story_cafe_address', 'story_activation_text', 'story_saved_text',
            ),
            'classes': ('collapse',),
            'description': (
                'Перезаписывает сетевые настройки сториз ТОЛЬКО для этой точки. '
                'Пустые поля = использовать значения сети. Набор подарков для '
                'сториз задаётся флагом у товаров и подключением к этой точке.'
            ),
        }),
    )


# ── Branch admin ──────────────────────────────────────────────────────────────

@admin.register(Branch, site=tenant_admin)
class BranchAdmin(admin.ModelAdmin):
    inlines = [BranchConfigInline]
    list_display = ('name', 'branch_id', 'dooglys_branch_id', 'is_active', 'pos_status', 'guests_link', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('name',)
    change_form_template = 'admin/branch/branch/change_form.html'

    def change_view(self, request, object_id=None, form_url='', extra_context=None):
        extra_context = extra_context or {}
        if object_id:
            obj = self.get_object(request, object_id)
            if obj:
                tenant = getattr(request, 'tenant', None)
                vk_app_id = getattr(settings, 'VK_MINI_APP_ID', '')
                company_id = tenant.client_id if tenant else ''

                extra_context['vk_link'] = (
                    f'https://vk.com/app{vk_app_id}/#/?company={company_id}&branch={obj.branch_id}'
                )
                extra_context['loyalupp_link'] = (
                    f'https://loyalupp.ru/#/?company={company_id}&branch={obj.branch_id}'
                )

                try:
                    from apps.shared.clients.models import Company, Domain
                    from django_tenants.utils import get_public_schema_name
                    pub = Company.objects.get(schema_name=get_public_schema_name())
                    pub_domain = Domain.objects.filter(tenant=pub, is_primary=True).first()
                    webhook_base = f'{request.scheme}://{pub_domain.domain}' if pub_domain else ''
                except Exception:
                    webhook_base = ''

                extra_context['delivery_info'] = {
                    'webhook_url': f'{webhook_base}/api/v1/delivery/webhook/',
                    'dooglys_branch_id': obj.dooglys_branch_id,
                }
        return super().change_view(request, object_id, form_url, extra_context)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            guests_count=Count('clients'),
        )

    def get_form(self, request, obj=None, change=False, **kwargs):
        """
        Возвращает форму с динамической валидацией POS-полей:
        обязательные поля зависят от ClientConfig.pos_type текущего тенанта.
        """
        from apps.shared.config.models import POSType

        pos_type = _tenant_pos_type(request)
        FormClass = super().get_form(request, obj, change=change, **kwargs)

        # Создаём подкласс формы с pos_type из замыкания — thread-safe
        class BranchForm(FormClass):
            def clean(form_self):
                cleaned_data = super().clean()

                if pos_type == POSType.IIKO:
                    if not cleaned_data.get('iiko_organization_id'):
                        form_self.add_error(
                            'iiko_organization_id',
                            'Обязательное поле — кассовая система настроена как iiko.',
                        )

                elif pos_type == POSType.DOOGLYS:
                    if not cleaned_data.get('dooglys_branch_id'):
                        form_self.add_error(
                            'dooglys_branch_id',
                            'Обязательное поле — кассовая система настроена как Dooglys.',
                        )
                    if not cleaned_data.get('dooglys_sale_point_id'):
                        form_self.add_error(
                            'dooglys_sale_point_id',
                            'Обязательное поле — кассовая система настроена как Dooglys.',
                        )

                return cleaned_data

        return BranchForm

    def get_fieldsets(self, request, obj=None):
        """
        Показывает только тот POS-блок, который настроен в ClientConfig.
        Если POS не настроен — оба блока свёрнуты.
        """
        from apps.shared.config.models import POSType

        pos_type = _tenant_pos_type(request)

        base = (None, {
            'fields': ('branch_id', 'name', 'description', 'is_active', 'story_image'),
        })

        if pos_type == POSType.IIKO:
            return [
                base,
                ('iiko', {
                    'fields': ('iiko_organization_id',),
                    'description': 'UUID организации из личного кабинета iiko.',
                }),
            ]

        if pos_type == POSType.DOOGLYS:
            return [
                base,
                ('Dooglys', {
                    'fields': ('dooglys_branch_id', 'dooglys_sale_point_id'),
                    'description': 'Идентификаторы точки в системе Dooglys.',
                }),
            ]

        # POS не настроен — показываем оба блока свёрнутыми
        return [
            base,
            ('iiko', {
                'fields': ('iiko_organization_id',),
                'classes': ('collapse',),
            }),
            ('Dooglys', {
                'fields': ('dooglys_branch_id', 'dooglys_sale_point_id'),
                'classes': ('collapse',),
            }),
        ]

    @admin.display(description='Касса')
    def pos_status(self, obj):
        """Показывает, какой POS ID настроен для этой точки."""
        if obj.iiko_organization_id:
            return mark_safe('<span style="color:#417690;font-weight:600;">iiko</span>')
        if obj.dooglys_branch_id:
            return mark_safe('<span style="color:#417690;font-weight:600;">Dooglys</span>')
        return mark_safe('<span style="color:#999;">—</span>')

    @admin.display(description='Гости', ordering='guests_count')
    def guests_link(self, obj):
        count = obj.guests_count
        if not count:
            return '—'
        url = (
            reverse('tenant_admin:branch_clientbranch_changelist')
            + f'?branch__id__exact={obj.pk}'
        )
        return format_html('<a href="{}">{} гост.</a>', url, count)


# ── ClientBranch admin ────────────────────────────────────────────────────────

@admin.register(ClientBranch, site=tenant_admin)
class ClientBranchAdmin(admin.ModelAdmin):
    list_display = ('client', 'branch', 'balance_col', 'visits_col', 'birth_date', 'is_employee', 'created_at')
    list_filter = ('branch', 'is_employee')
    search_fields = ('client__first_name', 'client__last_name', 'client__vk_id')
    date_hierarchy = 'created_at'
    readonly_fields = ('balance_col', 'transactions_link', 'visits_link', 'created_at', 'updated_at', 'invited_by')

    fieldsets = (
        (None, {
            'fields': ('client', 'branch', 'is_employee'),
        }),
        ('Личные данные', {
            'fields': ('birth_date', 'notes'),
        }),
        ('Монеты', {
            'fields': ('balance_col', 'transactions_link'),
        }),
        ('Активность', {
            'fields': ('visits_link',),
        }),
        ('Служебное', {
            'fields': ('created_at', 'updated_at', 'invited_by'),
            'classes': ('collapse',),
        }),
    )

    def get_queryset(self, request):
        income_sq = (
            CoinTransaction.objects
            .filter(client=OuterRef('pk'), type='income')
            .values('client')
            .annotate(s=Sum('amount'))
            .values('s')
        )
        expense_sq = (
            CoinTransaction.objects
            .filter(client=OuterRef('pk'), type='expense')
            .values('client')
            .annotate(s=Sum('amount'))
            .values('s')
        )
        return super().get_queryset(request).annotate(
            _balance=Coalesce(Subquery(income_sq), 0) - Coalesce(Subquery(expense_sq), 0),
            _visits=Count('visits', distinct=True),
        )

    @admin.display(description='Баланс ★', ordering='_balance')
    def balance_col(self, obj):
        balance = getattr(obj, '_balance', obj.coins_balance)
        if balance > 0:
            return format_html(
                '<span style="color:#1b5e20;font-weight:700;">★ {}</span>', balance
            )
        if balance < 0:
            return format_html(
                '<span style="color:#bf360c;font-weight:700;">★ {}</span>', balance
            )
        return mark_safe('<span style="color:var(--body-quiet-color,#aaa);">★ 0</span>')

    @admin.display(description='Транзакции')
    def transactions_link(self, obj):
        if not obj.pk:
            return '—'
        count = obj.transactions.count()
        url = (
            reverse('tenant_admin:branch_cointransaction_changelist')
            + f'?client__id__exact={obj.pk}'
        )
        return format_html('<a href="{}">{} транзакций →</a>', url, count)

    @admin.display(description='Визиты', ordering='_visits')
    def visits_col(self, obj):
        count = getattr(obj, '_visits', obj.visits.count())
        if not count:
            return mark_safe('<span style="color:var(--body-quiet-color,#aaa);">—</span>')
        return format_html('<span style="font-weight:600;">{}</span>', count)

    @admin.display(description='Визиты')
    def visits_link(self, obj):
        if not obj.pk:
            return '—'
        count = getattr(obj, '_visits', obj.visits.count())
        if not count:
            return '—'
        url = (
            reverse('tenant_admin:branch_clientbranchvisit_changelist')
            + f'?client__id__exact={obj.pk}'
        )
        return format_html('<a href="{}">{} визитов →</a>', url, count)


# ── DailyCode admin ───────────────────────────────────────────────────────────

_BADGE = (
    'display:inline-block;padding:2px 8px;border-radius:10px;'
    'font-size:11px;font-weight:600;white-space:nowrap;'
)
_PURPOSE_STYLES = {
    DailyCodePurpose.GAME:     _BADGE + 'background:#e8eaf6;color:#283593;border:1px solid #9fa8da;',
    DailyCodePurpose.QUEST:    _BADGE + 'background:#e0f2f1;color:#004d40;border:1px solid #80cbc4;',
    DailyCodePurpose.BIRTHDAY: _BADGE + 'background:#fce4ec;color:#880e4f;border:1px solid #f8bbd0;',
}
_PURPOSE_ICONS = {
    DailyCodePurpose.GAME:     '🎮',
    DailyCodePurpose.QUEST:    '🗺️',
    DailyCodePurpose.BIRTHDAY: '🎂',
}


@admin.register(DailyCode, site=tenant_admin)
class DailyCodeAdmin(admin.ModelAdmin):
    list_display = ('branch', 'purpose_badge', 'code', 'valid_date_col', 'created_at')
    list_display_links = ('branch',)
    list_filter = ('purpose', 'branch')
    list_editable = ('code',)
    date_hierarchy = 'valid_date'
    actions = ['generate_codes_today']
    readonly_fields = ('created_at', 'updated_at')

    fieldsets = (
        (None, {
            'fields': ('branch', 'purpose', 'code', 'valid_date'),
        }),
        ('Служебное', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    # ── List columns ──────────────────────────────────────────────────────

    @admin.display(description='Назначение', ordering='purpose')
    def purpose_badge(self, obj):
        style = _PURPOSE_STYLES.get(obj.purpose, _BADGE)
        icon  = _PURPOSE_ICONS.get(obj.purpose, '')
        label = obj.get_purpose_display()
        return format_html('<span style="{}">{} {}</span>', style, icon, label)

    @admin.display(description='Код', ordering='code')
    def code_col(self, obj):
        return format_html(
            '<span style="font-family:monospace;font-size:16px;'
            'font-weight:700;letter-spacing:2px;">{}</span>',
            obj.code,
        )

    @admin.display(description='Дата', ordering='valid_date')
    def valid_date_col(self, obj):
        today = timezone.localdate()
        if obj.valid_date == today:
            return format_html(
                '<span style="color:#1b5e20;font-weight:600;">Сегодня ({})</span>',
                obj.valid_date,
            )
        if obj.valid_date > today:
            return format_html(
                '<span style="color:#f57f17;">{}</span>', obj.valid_date
            )
        return format_html(
            '<span style="color:var(--body-quiet-color,#aaa);">{}</span>', obj.valid_date
        )

    # ── Actions ───────────────────────────────────────────────────────────

    @admin.action(description='⟳ Сгенерировать коды на сегодня (для всех точек)')
    def generate_codes_today(self, request, queryset):
        from apps.tenant.branch.models import current_code_date
        today = current_code_date()
        created_count = 0
        for branch in Branch.objects.all():
            for purpose_value, _ in DailyCodePurpose.choices:
                _, created = DailyCode.objects.get_or_create(
                    branch=branch,
                    purpose=purpose_value,
                    valid_date=today,
                    defaults={'code': f'{random.randint(0, 99999):05d}'},
                )
                if created:
                    created_count += 1
        self.message_user(request, f'Создано {created_count} новых кодов на {today}.')


# ── Cooldown admin ────────────────────────────────────────────────────────────

_FEATURE_STYLES = {
    CooldownFeature.GAME:      _BADGE + 'background:#e8eaf6;color:#283593;border:1px solid #9fa8da;',
    CooldownFeature.INVENTORY: _BADGE + 'background:#fff3cd;color:#856404;border:1px solid #ffe08a;',
    CooldownFeature.SHOP:      _BADGE + 'background:#f3e5f5;color:#4a148c;border:1px solid #e1bee7;',
    CooldownFeature.QUEST:     _BADGE + 'background:#e0f2f1;color:#004d40;border:1px solid #80cbc4;',
}
_FEATURE_ICONS = {
    CooldownFeature.GAME:      '🎮',
    CooldownFeature.INVENTORY: '🎁',
    CooldownFeature.SHOP:      '🛍️',
    CooldownFeature.QUEST:     '🗺️',
}
_LOCKED_STYLE = _BADGE + 'background:#fbe9e7;color:#bf360c;border:1px solid #ffab91;'
_READY_STYLE  = _BADGE + 'background:#e8f5e9;color:#1b5e20;border:1px solid #c8e6c9;'


class CooldownStatusFilter(admin.SimpleListFilter):
    title = 'Статус'
    parameter_name = 'cd_status'

    def lookups(self, request, model_admin):
        return [
            ('locked', '🔒 На перезарядке'),
            ('ready',  '✅ Готов'),
        ]

    def queryset(self, request, queryset):
        from django.utils import timezone
        now = timezone.now()
        if self.value() == 'locked':
            return queryset.filter(expires_at__gt=now)
        if self.value() == 'ready':
            return queryset.filter(expires_at__lte=now)
        return queryset


@admin.register(Cooldown, site=tenant_admin)
class CooldownAdmin(admin.ModelAdmin):
    list_display = (
        'client_col', 'branch_col', 'feature_badge',
        'status_badge', 'remaining_col', 'last_activated_at',
    )
    list_display_links = ('client_col',)
    list_filter = (CooldownStatusFilter, 'feature', 'client__branch')
    search_fields = ('client__client__first_name', 'client__client__last_name')
    list_select_related = ('client__client', 'client__branch')
    readonly_fields = ('last_activated_at', 'expires_at', 'created_at', 'updated_at')
    actions = ['action_reset_cooldown']

    fieldsets = (
        (None, {
            'fields': ('client', 'feature', 'duration'),
        }),
        ('Состояние', {
            'fields': ('last_activated_at', 'expires_at'),
        }),
        ('Служебное', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    # ── Queryset ──────────────────────────────────────────────────────────

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'client__client', 'client__branch',
        )

    # ── List columns ──────────────────────────────────────────────────────

    @admin.display(description='Гость', ordering='client__client__first_name')
    def client_col(self, obj):
        c = obj.client.client
        return c.first_name or f'vk{c.vk_id}'

    @admin.display(description='Точка', ordering='client__branch__name')
    def branch_col(self, obj):
        return obj.client.branch.name

    @admin.display(description='Функция', ordering='feature')
    def feature_badge(self, obj):
        style = _FEATURE_STYLES.get(obj.feature, _BADGE)
        icon  = _FEATURE_ICONS.get(obj.feature, '')
        label = obj.get_feature_display()
        return format_html('<span style="{}">{} {}</span>', style, icon, label)

    @admin.display(description='Статус')
    def status_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="{}">🔒 На перезарядке</span>', _LOCKED_STYLE)
        return format_html('<span style="{}">✅ Готов</span>', _READY_STYLE)

    @admin.display(description='Осталось', ordering='expires_at')
    def remaining_col(self, obj):
        if not obj.is_active:
            return mark_safe('<span style="color:var(--body-quiet-color,#aaa);">—</span>')
        delta = obj.expires_at - timezone.now()
        total = int(delta.total_seconds())
        hrs  = total // 3600
        mins = (total % 3600) // 60
        return format_html(
            '<span style="color:#bf360c;font-weight:600;">{}ч {}м</span>', hrs, mins
        )

    # ── Actions ───────────────────────────────────────────────────────────

    @admin.action(description='Снять перезарядку')
    def action_reset_cooldown(self, request, queryset):
        past = timezone.now() - timedelta(hours=1)
        count = queryset.update(expires_at=past)
        self.message_user(request, f'Перезарядка снята: {count}')


# ── CoinTransaction admin ─────────────────────────────────────────────────────

_INCOME_STYLE  = _BADGE + 'background:#e8f5e9;color:#1b5e20;border:1px solid #a5d6a7;'
_EXPENSE_STYLE = _BADGE + 'background:#fbe9e7;color:#bf360c;border:1px solid #ffab91;'

_TX_SOURCE_STYLES = {
    TransactionSource.GAME:     _BADGE + 'background:#e8eaf6;color:#283593;border:1px solid #9fa8da;',
    TransactionSource.QUEST:    _BADGE + 'background:#e0f2f1;color:#004d40;border:1px solid #80cbc4;',
    TransactionSource.SHOP:     _BADGE + 'background:#f3e5f5;color:#4a148c;border:1px solid #e1bee7;',
    TransactionSource.BIRTHDAY: _BADGE + 'background:#fce4ec;color:#880e4f;border:1px solid #f8bbd0;',
    TransactionSource.DELIVERY: _BADGE + 'background:#e0f7fa;color:#006064;border:1px solid #80deea;',
    TransactionSource.MANUAL:   _BADGE + 'background:#f5f5f5;color:#424242;border:1px solid #e0e0e0;',
}
_TX_SOURCE_ICONS = {
    TransactionSource.GAME:     '🎮',
    TransactionSource.QUEST:    '🗺️',
    TransactionSource.SHOP:     '🛍️',
    TransactionSource.BIRTHDAY: '🎂',
    TransactionSource.DELIVERY: '📦',
    TransactionSource.MANUAL:   '👤',
}


@admin.register(CoinTransaction, site=tenant_admin)
class CoinTransactionAdmin(admin.ModelAdmin):
    list_display = (
        'client_col', 'branch_col',
        'type_badge', 'source_badge',
        'amount_col', 'description', 'created_at',
    )
    list_display_links = ('client_col',)
    list_filter = ('type', 'source', 'client__branch')
    search_fields = ('client__client__first_name', 'client__client__last_name', 'description')
    list_select_related = ('client__client', 'client__branch')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('client',)
    readonly_fields = ('created_at',)

    def get_readonly_fields(self, request, obj=None):
        if obj:  # редактирование существующей — всё заблокировано
            return ('client', 'type', 'source', 'amount', 'description', 'created_at')
        return ('created_at',)  # создание — только системные поля

    fieldsets = (
        (None, {
            'fields': ('client', 'type', 'source', 'amount', 'description'),
        }),
        ('Служебное', {
            'fields': ('created_at',),
            'classes': ('collapse',),
        }),
    )

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop('delete_selected', None)
        return actions

    def delete_view(self, request, object_id, extra_context=None):
        from django.contrib import messages as _messages
        from django.http import HttpResponseRedirect
        from django.urls import reverse
        try:
            return super().delete_view(request, object_id, extra_context)
        except NotImplementedError as e:
            self.message_user(request, str(e), level=_messages.ERROR)
            url = reverse(
                f'{self.admin_site.name}:'
                f'{self.model._meta.app_label}_{self.model._meta.model_name}_change',
                args=[object_id],
            )
            return HttpResponseRedirect(url)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'client__client', 'client__branch',
        )

    # ── List columns ──────────────────────────────────────────────────────

    @admin.display(description='Гость', ordering='client__client__first_name')
    def client_col(self, obj):
        c = obj.client.client
        full = f'{c.first_name} {c.last_name}'.strip()
        return full or f'vk{c.vk_id}'

    @admin.display(description='Точка', ordering='client__branch__name')
    def branch_col(self, obj):
        return obj.client.branch.name

    @admin.display(description='Тип', ordering='type')
    def type_badge(self, obj):
        if obj.type == TransactionType.INCOME:
            return format_html('<span style="{}">▲ Начисление</span>', _INCOME_STYLE)
        return format_html('<span style="{}">▼ Списание</span>', _EXPENSE_STYLE)

    @admin.display(description='Источник', ordering='source')
    def source_badge(self, obj):
        style = _TX_SOURCE_STYLES.get(obj.source, _BADGE)
        icon  = _TX_SOURCE_ICONS.get(obj.source, '')
        label = obj.get_source_display()
        return format_html('<span style="{}">{} {}</span>', style, icon, label)

    @admin.display(description='Сумма ★', ordering='amount')
    def amount_col(self, obj):
        if obj.type == TransactionType.INCOME:
            return format_html(
                '<span style="color:#1b5e20;font-weight:700;font-size:13px;">+{}</span>',
                obj.amount,
            )
        return format_html(
            '<span style="color:#bf360c;font-weight:700;font-size:13px;">−{}</span>',
            obj.amount,
        )


# ── ClientBranchVisit admin ───────────────────────────────────────────────────

@admin.register(ClientBranchVisit, site=tenant_admin)
class ClientBranchVisitAdmin(admin.ModelAdmin):
    list_display = ('client_col', 'branch_col', 'visited_at')
    list_display_links = ('client_col',)
    list_filter = ('client__branch',)
    search_fields = ('client__client__first_name', 'client__client__last_name', 'client__client__vk_id')
    date_hierarchy = 'visited_at'
    list_select_related = ('client__client', 'client__branch')
    readonly_fields = ('client', 'visited_at')

    fieldsets = (
        (None, {
            'fields': ('client', 'visited_at'),
        }),
    )

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    @admin.display(description='Гость', ordering='client__client__first_name')
    def client_col(self, obj):
        c = obj.client.client
        full = f'{c.first_name} {c.last_name}'.strip()
        return full or f'vk{c.vk_id}'

    @admin.display(description='Точка', ordering='client__branch__name')
    def branch_col(self, obj):
        return obj.client.branch.name

# ── ClientVKStatus admin ──────────────────────────────────────────────────────

_VK_VIA_APP_STYLE = _BADGE + 'background:#e8f5e9;color:#1b5e20;border:1px solid #a5d6a7;'
_VK_PRE_STYLE     = _BADGE + 'background:#e8eaf6;color:#283593;border:1px solid #9fa8da;'
_VK_NONE_STYLE    = _BADGE + 'background:#f5f5f5;color:#9e9e9e;border:1px solid #e0e0e0;'


@admin.register(ClientVKStatus, site=tenant_admin)
class ClientVKStatusAdmin(admin.ModelAdmin):
    list_display = (
        'client_col', 'branch_col',
        'community_badge', 'newsletter_badge', 'is_story_uploaded',
        'checked_at',
    )
    list_display_links = ('client_col',)
    list_filter = (
        'is_community_member', 'community_via_app',
        'is_newsletter_subscriber', 'newsletter_via_app',
        'client__branch',
    )
    search_fields = ('client__client__first_name', 'client__client__last_name', 'client__client__vk_id')
    date_hierarchy = 'checked_at'
    list_select_related = ('client__client', 'client__branch')
    readonly_fields = ('community_badge', 'newsletter_badge', 'checked_at')

    fieldsets = (
        ('Сообщество', {
            'fields': ('is_community_member', 'community_joined_at', 'community_via_app', 'community_badge'),
        }),
        ('Рассылка', {
            'fields': ('is_newsletter_subscriber', 'newsletter_joined_at', 'newsletter_via_app', 'newsletter_badge'),
        }),
        ('Служебное', {
            'fields': ('checked_at',),
            'classes': ('collapse',),
        }),
    )

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('client__client', 'client__branch')

    @admin.display(description='Гость', ordering='client__client__first_name')
    def client_col(self, obj):
        c = obj.client.client
        full = f'{c.first_name} {c.last_name}'.strip()
        return full or f'vk{c.vk_id}'

    @admin.display(description='Точка', ordering='client__branch__name')
    def branch_col(self, obj):
        return obj.client.branch.name

    @admin.display(description='Сообщество', ordering='is_community_member')
    def community_badge(self, obj):
        if not obj.is_community_member:
            return format_html('<span style="{}">— Нет</span>', _VK_NONE_STYLE)
        if obj.community_via_app:
            return format_html('<span style="{}">✓ Через приложение</span>', _VK_VIA_APP_STYLE)
        return format_html('<span style="{}">✓ До приложения</span>', _VK_PRE_STYLE)

    @admin.display(description='Рассылка', ordering='is_newsletter_subscriber')
    def newsletter_badge(self, obj):
        if not obj.is_newsletter_subscriber:
            return format_html('<span style="{}">— Нет</span>', _VK_NONE_STYLE)
        if obj.newsletter_via_app:
            return format_html('<span style="{}">✓ Через приложение</span>', _VK_VIA_APP_STYLE)
        return format_html('<span style="{}">✓ До приложения</span>', _VK_PRE_STYLE)


@admin.register(Promotions, site=tenant_admin)
class PromotionsAdmin(admin.ModelAdmin):
    list_display = ('title', 'branch', 'discount', 'dates', 'created_at')
    list_filter = ('branch',)
    search_fields = ('title',)


# ── Testimonials ──────────────────────────────────────────────────────────────

# Source badge styles
_SOURCE_STYLES = {
    TestimonialMessage.Source.APP:         ('💬', '#1d72b8', 'Приложение'),
    TestimonialMessage.Source.VK_MESSAGE:  ('📩', '#4a76a8', 'ВКонтакте'),
    TestimonialMessage.Source.ADMIN_REPLY: ('✉️', '#16a34a', 'Ответ'),
}

_SENTIMENT_STYLES = {
    TestimonialConversation.Sentiment.POSITIVE:           ('#16a34a', '😊 Позитивный'),
    TestimonialConversation.Sentiment.NEGATIVE:           ('#dc2626', '😠 Негативный'),
    TestimonialConversation.Sentiment.PARTIALLY_NEGATIVE: ('#d97706', '😐 Частично негативный'),
    TestimonialConversation.Sentiment.NEUTRAL:            ('#6b7280', '😶 Нейтральный'),
    TestimonialConversation.Sentiment.SPAM:               ('#9ca3af', '🚫 Спам'),
    TestimonialConversation.Sentiment.WAITING:            ('#94a3b8', '⏳ Ожидает'),
}


class TestimonialReplyForm(forms.Form):
    """Форма для отправки ответа — отдельно, не привязана к модели."""
    reply_text = forms.CharField(
        label='Написать ответ',
        required=False,
        widget=forms.Textarea(attrs={
            'rows': 3,
            'placeholder': 'Введите текст ответа — будет отправлен в ВКонтакте...',
            'style': 'width:100%;font-size:13px;border:1px solid #ccc;border-radius:6px;padding:8px;',
        }),
    )


class TestimonialMessageInline(admin.TabularInline):
    """Чат-хронология внутри переписки (только просмотр)."""
    model       = TestimonialMessage
    extra       = 0
    can_delete  = False
    show_change_link = False
    classes     = ['collapse']
    ordering    = ('created_at',)
    fields      = ('chat_bubble',)
    readonly_fields = ('chat_bubble',)
    verbose_name = ''
    verbose_name_plural = 'История сообщений'

    def has_add_permission(self, request, obj=None):
        return request.user.is_superuser

    @admin.display(description='')
    def chat_bubble(self, msg):
        icon, color, label = _SOURCE_STYLES.get(
            msg.source, ('💬', '#888', msg.source)
        )
        is_reply = (msg.source == TestimonialMessage.Source.ADMIN_REPLY)
        align    = 'right' if is_reply else 'left'
        bg       = '#e8f5e9' if is_reply else '#f0f4ff'
        border   = f'2px solid {color}'

        extras = []
        if msg.rating:
            stars = '⭐' * msg.rating + '☆' * (5 - msg.rating)
            extras.append(f'<div style="font-size:15px;margin-bottom:4px;">{stars}</div>')
        if msg.phone:
            extras.append(f'<div style="font-size:11px;color:#6b7280;">📞 {msg.phone}</div>')
        if msg.table_number:
            extras.append(f'<div style="font-size:11px;color:#6b7280;">🪑 Столик {msg.table_number}</div>')

        extras_html = ''.join(extras)
        ts = msg.created_at.strftime('%d.%m.%Y %H:%M')

        return format_html(
            '''<div style="text-align:{align};margin:4px 0;">
              <div style="display:inline-block;max-width:80%;text-align:left;
                background:{bg};border:{border};border-radius:10px;padding:10px 14px;">
                <div style="font-size:10px;color:{color};font-weight:700;margin-bottom:4px;">
                  {icon} {label}
                </div>
                {extras}
                <div style="font-size:13px;white-space:pre-wrap;">{text}</div>
                <div style="font-size:10px;color:#9ca3af;margin-top:6px;text-align:right;">{ts}</div>
              </div>
            </div>''',
            align=align, bg=bg, border=border, color=color,
            icon=icon, label=label, extras=mark_safe(extras_html),
            text=msg.text, ts=ts,
        )


@admin.register(TestimonialConversation, site=tenant_admin)
class TestimonialConversationAdmin(admin.ModelAdmin):
    list_display    = (
        'sender_col', 'branch', 'source_icon_col', 'last_msg_col',
        'sentiment_col', 'rating_col', 'unread_col', 'replied_col', 'last_message_at',
    )
    list_display_links = ('sender_col',)
    list_filter     = (
        'branch', 'sentiment', 'has_unread', 'is_replied',
    )
    search_fields   = (
        'vk_sender_id',
        'client__client__first_name',
        'client__client__last_name',
        'messages__text',
    )
    date_hierarchy  = 'last_message_at'
    ordering        = ('-has_unread', '-last_message_at')
    readonly_fields = (
        'created_at', 'updated_at', 'last_message_at',
        'client', 'vk_sender_id', 'branch',
    )
    inlines         = [TestimonialMessageInline]

    fieldsets = (
        (None, {
            'fields': (('branch', 'vk_sender_id', 'client'),),
        }),
        ('Анализ', {
            'fields': (('sentiment', 'ai_comment'),),
        }),
        ('Статус', {
            'fields': (('has_unread', 'is_replied', 'last_message_at'),),
        }),
        ('Служебное', {
            'fields': (('created_at', 'updated_at'),),
            'classes': ('collapse',),
        }),
    )

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related('branch', 'client__client')
            .prefetch_related('messages')
        )

    def changelist_view(self, request, extra_context=None):
        """Pre-fetch Client names for unlinked conversations (avoids N+1)."""
        from apps.shared.guest.models import Client
        self._vk_name_cache: dict[str, str] = {}
        response = super().changelist_view(request, extra_context)
        try:
            qs = response.context_data['cl'].queryset
        except (AttributeError, KeyError):
            return response
        # Collect vk_sender_ids that have no ClientBranch linked
        unlinked_ids = [
            obj.vk_sender_id for obj in qs
            if not obj.client_id and obj.vk_sender_id
        ]
        if unlinked_ids:
            # Single bulk query to Client (public schema)
            for c in Client.objects.filter(
                vk_id__in=[v for v in unlinked_ids if v.isdigit()],
            ).only('vk_id', 'first_name', 'last_name'):
                name = f'{c.first_name} {c.last_name}'.strip()
                if name:
                    self._vk_name_cache[str(c.vk_id)] = name
        return response

    # ── list_display columns ──────────────────────────────────────────────────

    @admin.display(description='Отправитель', ordering='vk_sender_id')
    def sender_col(self, obj):
        if obj.client_id:
            c = obj.client.client
            name = f'{c.first_name} {c.last_name}'.strip() or f'VK {obj.vk_sender_id}'
            return format_html(
                '<strong>{}</strong><br>'
                '<span style="font-size:10px;color:#9ca3af;">VK {}</span>',
                name, obj.vk_sender_id,
            )
        # Fallback: name fetched in bulk by changelist_view
        vk_name = getattr(self, '_vk_name_cache', {}).get(obj.vk_sender_id or '')
        if vk_name:
            return format_html(
                '<strong>{}</strong><br>'
                '<span style="font-size:10px;color:#9ca3af;">VK {}</span>',
                vk_name, obj.vk_sender_id,
            )
        return format_html(
            '<span style="color:#4a76a8;font-weight:600;">VK {}</span>',
            obj.vk_sender_id or '—',
        )

    @admin.display(description='Источник')
    def source_icon_col(self, obj):
        sources = obj.messages.values_list('source', flat=True).distinct()
        icons = []
        for src in sources:
            icon, color, label = _SOURCE_STYLES.get(src, ('💬', '#888', src))
            icons.append(format_html(
                '<span title="{label}" style="display:inline-block;'
                'background:{color};color:#fff;border-radius:12px;'
                'padding:1px 8px;font-size:10px;margin:1px;">'
                '{icon}</span>',
                label=label, color=color, icon=icon,
            ))
        return mark_safe(' '.join(str(i) for i in icons)) if icons else '—'

    @admin.display(description='Последнее сообщение')
    def last_msg_col(self, obj):
        msg = obj.messages.last()
        if not msg:
            return '—'
        preview = msg.text[:80] + ('…' if len(msg.text) > 80 else '')
        return format_html(
            '<span style="font-size:12px;color:#374151;">{}</span>', preview,
        )

    @admin.display(description='Тональность', ordering='sentiment')
    def sentiment_col(self, obj):
        color, label = _SENTIMENT_STYLES.get(obj.sentiment, ('#888', obj.sentiment))
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 10px;'
            'border-radius:12px;font-size:11px;font-weight:600;">{}</span>',
            color, label,
        )

    @admin.display(description='Оценка')
    def rating_col(self, obj):
        msg = obj.messages.filter(rating__isnull=False).last()
        if not msg:
            return mark_safe('<span style="color:#d1d5db;">—</span>')
        stars = '⭐' * msg.rating + '<span style="color:#d1d5db;">☆</span>' * (5 - msg.rating)
        return mark_safe(f'<span style="font-size:14px;">{stars}</span>')

    @admin.display(description='Непрочит.', boolean=False, ordering='has_unread')
    def unread_col(self, obj):
        if obj.has_unread:
            return mark_safe(
                '<span style="background:#dc2626;color:#fff;border-radius:50%;'
                'width:18px;height:18px;display:inline-flex;align-items:center;'
                'justify-content:center;font-size:10px;font-weight:700;">●</span>'
            )
        return mark_safe('<span style="color:#d1d5db;">○</span>')

    @admin.display(description='Ответ', boolean=False, ordering='is_replied')
    def replied_col(self, obj):
        return format_html(
            '<span style="color:{};">{}</span>',
            '#16a34a' if obj.is_replied else '#9ca3af',
            '✓ Отвечено' if obj.is_replied else '— Нет',
        )

    # ── Change view: reply form + chat ───────────────────────────────────────

    def change_view(self, request, object_id, form_url='', extra_context=None):
        ctx: dict = extra_context or {}
        conv: TestimonialConversation | None = self.get_object(request, object_id)

        if conv is not None and request.method == 'POST' and '_send_reply' in request.POST:
            # Handle reply separately — do NOT fall through to super() which would
            # process the model admin form and overwrite has_unread / is_replied.
            reply_form = TestimonialReplyForm(request.POST)
            if reply_form.is_valid():
                text = reply_form.cleaned_data['reply_text'].strip()
                if text:
                    try:
                        from .api.services import send_vk_reply
                        send_vk_reply(conv, text)
                        messages.success(request, '✓ Ответ отправлен в ВКонтакте')
                    except ValueError as e:
                        messages.error(request, f'Ошибка отправки: {e}')
                    except Exception as e:
                        messages.error(request, f'VK API ошибка: {e}')
                else:
                    messages.warning(request, 'Текст ответа не может быть пустым')
            return HttpResponseRedirect(request.path)

        # ── Context for chat widget ───────────────────────────────────────────
        ctx['chat_messages'] = (
            conv.messages.all().order_by('created_at') if conv else []
        )
        ctx['reply_form'] = TestimonialReplyForm()
        ctx['can_reply']  = conv is not None and bool(conv.vk_sender_id)

        # Display name for the chat header
        if conv:
            if conv.client_id:
                gc = conv.client.client
                ctx['conv_name'] = (
                    f'{gc.first_name} {gc.last_name}'.strip() or f'VK {conv.vk_sender_id}'
                )
            else:
                ctx['conv_name'] = f'VK {conv.vk_sender_id}'
        else:
            ctx['conv_name'] = ''

        # Auto-mark as read when admin opens the conversation
        if conv and conv.has_unread:
            conv.has_unread = False
            conv.save(update_fields=['has_unread'])

        return super().change_view(request, object_id, form_url, ctx)

    # ── JSON endpoint for chat polling ────────────────────────────────────────

    def get_urls(self):
        from django.urls import path
        urls = super().get_urls()
        custom = [
            path(
                '<path:object_id>/messages-json/',
                self.admin_site.admin_view(self.messages_json_view),
                name='branch_testimonialconversation_messages_json',
            ),
        ]
        return custom + urls

    def messages_json_view(self, request, object_id):
        from django.http import JsonResponse

        try:
            conv = TestimonialConversation.objects.get(pk=object_id)
        except (TestimonialConversation.DoesNotExist, ValueError):
            return JsonResponse({'messages': []})

        qs = conv.messages.order_by('created_at')
        try:
            since_id = int(request.GET.get('since_id', 0))
        except (TypeError, ValueError):
            since_id = 0
        if since_id:
            qs = qs.filter(id__gt=since_id)

        data = [
            {
                'id':           m.id,
                'source':       m.source,
                'text':         m.text,
                'rating':       m.rating,
                'phone':        m.phone or '',
                'table_number': m.table_number,
                'created_at':   m.created_at.isoformat(),
                'created_fmt':  m.created_at.strftime('%d.%m.%Y %H:%M'),
            }
            for m in qs
        ]
        return JsonResponse({'messages': data})

    # ── Actions ───────────────────────────────────────────────────────────────

    @admin.action(description='✓ Отметить как прочитанные')
    def mark_read(self, request, queryset):
        updated = queryset.filter(has_unread=True).update(has_unread=False)
        self.message_user(request, f'Отмечено прочитанными: {updated}', messages.SUCCESS)

    @admin.action(description='🔄 Сбросить тональность → Ожидает анализа')
    def reset_sentiment(self, request, queryset):
        updated = queryset.update(sentiment=TestimonialConversation.Sentiment.WAITING)
        self.message_user(request, f'Сброшено: {updated}', messages.SUCCESS)

    @admin.action(description='👤 Привязать зарегистрированных гостей по VK ID')
    def link_registered_clients(self, request, queryset):
        linked = 0
        for conv in queryset.filter(client__isnull=True).select_related('branch'):
            if not conv.vk_sender_id:
                continue
            try:
                vk_id = int(conv.vk_sender_id)
            except (ValueError, TypeError):
                continue
            cb = ClientBranch.objects.filter(
                branch=conv.branch,
                client__vk_id=vk_id,
            ).first()
            if cb:
                conv.client = cb
                conv.save(update_fields=['client'])
                linked += 1
        self.message_user(request, f'Привязано: {linked}', messages.SUCCESS)

    actions = ['mark_read', 'reset_sentiment', 'link_registered_clients']

    # ── Custom change_form template for reply button ──────────────────────────

    change_form_template = 'admin/branch/testimonialconversation/change_form.html'
