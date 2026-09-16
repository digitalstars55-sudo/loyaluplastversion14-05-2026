from django.contrib import admin

from apps.shared.config.admin_sites import public_admin

from .models import MonitorAlertState


class ResolvedFilter(admin.SimpleListFilter):
    """«Что горит прямо сейчас» — первый вопрос к этой таблице, поэтому отдельный фильтр."""
    title = 'Состояние'
    parameter_name = 'state'

    def lookups(self, request, model_admin):
        return (('active', 'Активные'), ('resolved', 'Закрытые'))

    def queryset(self, request, queryset):
        if self.value() == 'active':
            return queryset.filter(resolved_at__isnull=True)
        if self.value() == 'resolved':
            return queryset.filter(resolved_at__isnull=False)
        return queryset


@admin.register(MonitorAlertState, site=public_admin)
class MonitorAlertStateAdmin(admin.ModelAdmin):
    """
    Read-only витрина сигналов мониторинга: что сломано, с какого момента и
    сколько раз об этом уже будили. Руками здесь ничего не правят — состояние
    ведёт задача; ручное «закрытие» сигнала только рассинхронизировало бы
    дедуп, а реальная беда всё равно вернулась бы через час.
    """

    list_display = ('key', 'severity', 'title', 'first_seen_at', 'last_seen_at', 'last_sent_at', 'resolved_at', 'sent_count')
    list_filter = ('severity', ResolvedFilter)
    search_fields = ('key', 'title')
    date_hierarchy = 'last_seen_at'
    ordering = ('-last_seen_at',)
    readonly_fields = (
        'key', 'severity', 'title', 'body', 'data',
        'first_seen_at', 'last_seen_at', 'last_sent_at', 'resolved_at', 'sent_count',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
