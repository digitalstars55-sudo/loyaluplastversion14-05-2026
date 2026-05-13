from django import forms
from django.contrib import admin, messages
from django.http import HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, mark_safe

from apps.shared.config.admin_sites import tenant_admin

from .models import BranchSegmentSnapshot, GuestRFScore, KnowledgeBaseDocument, RFMigrationLog, RFSegment, RFSettings

# ── Style constants ───────────────────────────────────────────────────────────

_BADGE = (
    'display:inline-block;padding:2px 8px;border-radius:10px;'
    'font-size:11px;font-weight:600;white-space:nowrap;'
)


def _segment_badge(seg):
    if not seg:
        return mark_safe('<span style="color:var(--body-quiet-color,#aaa);">—</span>')
    return format_html(
        '<span style="background:{};color:#fff;padding:2px 10px;'
        'border-radius:12px;font-size:11px;font-weight:700;">{} {}</span>',
        seg.color, seg.emoji, seg.name,
    )


# ── RFSegment admin ───────────────────────────────────────────────────────────


class RFSegmentAdminForm(forms.ModelForm):
    """
    Форма с дополнительным служебным полем «Применить настройки ко всем кафе».

    Если чекбокс установлен — после сохранения записи её поля (имя, эмодзи,
    цвет, диапазоны R/F, стратегия, подсказка) будут скопированы в
    per-branch RFSegment-записи всех активных торговых точек с тем же кодом.
    На странице подтверждения пользователь увидит сводку и сможет отменить.
    """

    apply_to_all_branches = forms.BooleanField(
        required=False,
        label='Применить настройки ко всем кафе',
        help_text=(
            'После сохранения скопировать поля сегмента в per-branch версии '
            'всех активных торговых точек (с тем же кодом). Текущие значения '
            'этих полей у каждой точки будут перезаписаны. '
            'Поле «Дата последней рассылки» НЕ копируется.'
        ),
    )

    class Meta:
        model = RFSegment
        fields = '__all__'


@admin.register(RFSegment, site=tenant_admin)
class RFSegmentAdmin(admin.ModelAdmin):
    form = RFSegmentAdminForm

    list_display = (
        'code_badge', 'scope_col', 'name', 'recency_range_col',
        'frequency_range_col', 'guests_count_col', 'hint_preview_col',
        'last_campaign_date',
    )
    list_display_links = ('code_badge',)
    list_filter = ('branch',)
    search_fields = ('code', 'name', 'branch__name')
    readonly_fields = ('created_at', 'updated_at', 'guests_count_col')
    actions = ['apply_to_all_action']

    fieldsets = (
        (None, {
            'fields': ('branch', 'code', 'name', 'emoji', 'color'),
            'description': (
                'Оставьте поле «Торговая точка» пустым, чтобы создать общий сегмент '
                'для всей сети (используется как fallback для точек, у которых нет '
                'собственной версии этого сегмента).'
            ),
        }),
        ('RF-границы', {
            'fields': (('recency_min', 'recency_max'), ('frequency_min', 'frequency_max')),
            'description': (
                'Гость попадает в этот сегмент, если его давность и частота '
                'ОДНОВРЕМЕННО попадают в указанные диапазоны.'
            ),
        }),
        ('Маркетинг', {
            'fields': ('strategy', 'hint', 'last_campaign_date'),
            'description': (
                '<b>Подсказка</b> — краткая инструкция менеджеру по работе с рассылками для этого сегмента. '
                'Отображается в таблице сегментов и помогает принять правильное решение.'
            ),
        }),
        ('Применить ко всем точкам', {
            'fields': ('apply_to_all_branches',),
            'description': (
                'Удобно при первичной настройке сети: задайте поля сегмента один раз '
                'и одной галочкой раскопируйте их во все кафе. '
                'После применения для отдельной точки можно индивидуально '
                'переопределить значения — общие настройки используются как fallback.'
            ),
        }),
        ('Статистика', {
            'fields': ('guests_count_col',),
        }),
        ('Служебное', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    # ── Custom URLs ───────────────────────────────────────────────────────────

    def get_urls(self):
        urls = super().get_urls()
        return [
            path(
                '<int:pk>/send-broadcast/',
                self.admin_site.admin_view(self._send_broadcast_view),
                name='analytics_rfsegment_send_broadcast',
            ),
            path(
                '<int:pk>/export-senler/',
                self.admin_site.admin_view(self._export_senler_view),
                name='analytics_rfsegment_export_senler',
            ),
            path(
                '<int:pk>/apply-all/',
                self.admin_site.admin_view(self._apply_to_all_view),
                name='analytics_rfsegment_apply_all',
            ),
        ] + urls

    # ── Save logic: handle apply_to_all checkbox ─────────────────────────────

    def save_model(self, request, obj, form, change):
        """
        Обычное сохранение. Если стоит галочка «Применить ко всем кафе» —
        response_change/response_add перенаправляет на confirm-page.
        """
        super().save_model(request, obj, form, change)

    def _redirect_to_apply_all(self, request, obj):
        return HttpResponseRedirect(
            reverse(
                f'{self.admin_site.name}:analytics_rfsegment_apply_all',
                args=[obj.pk],
            )
        )

    def response_add(self, request, obj, post_url_continue=None):
        if request.POST.get('apply_to_all_branches'):
            return self._redirect_to_apply_all(request, obj)
        return super().response_add(request, obj, post_url_continue)

    def response_change(self, request, obj):
        if request.POST.get('apply_to_all_branches'):
            return self._redirect_to_apply_all(request, obj)
        return super().response_change(request, obj)

    def _apply_to_all_view(self, request, pk):
        """
        Промежуточная страница «Вы уверены, что хотите применить
        настройки ко всем точкам? Текущие значения будут заменены».
        """
        from apps.tenant.branch.models import Branch
        try:
            obj = RFSegment.objects.get(pk=pk)
        except RFSegment.DoesNotExist:
            self.message_user(request, 'Сегмент не найден.', level=messages.ERROR)
            return HttpResponseRedirect(reverse(f'{self.admin_site.name}:analytics_rfsegment_changelist'))

        if request.method == 'POST' and request.POST.get('_apply_all_confirmed') == '1':
            affected = obj.apply_to_all_branches()
            self.message_user(
                request,
                f'Готово: сегмент «{obj.emoji} {obj.name}» применён к {affected} торговым точкам.',
                level=messages.SUCCESS,
            )
            return HttpResponseRedirect(reverse(f'{self.admin_site.name}:analytics_rfsegment_changelist'))

        active_branches = list(Branch.objects.filter(is_active=True).order_by('name'))
        ctx = {
            'title': 'Применить RF-сегмент ко всем кафе?',
            'object': obj,
            'fields': obj._copy_defaults(),
            'branches': active_branches,
            'branches_count': len(active_branches),
            'opts': self.model._meta,
            'app_label': self.model._meta.app_label,
            'has_view_permission': True,
            'site_header': getattr(self.admin_site, 'site_header', 'Администрирование'),
            'site_title':  getattr(self.admin_site, 'site_title',  'Администрирование'),
        }
        return TemplateResponse(
            request,
            'admin/analytics/rfsegment/apply_all_confirm.html',
            ctx,
        )

    # ── Admin actions ─────────────────────────────────────────────────────────

    @admin.action(description='Применить выбранный сегмент ко ВСЕМ точкам')
    def apply_to_all_action(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(
                request,
                'Выберите ровно одну запись-источник: её настройки будут раскопированы '
                'во все активные торговые точки.',
                level=messages.WARNING,
            )
            return
        source = queryset.first()
        affected = source.apply_to_all_branches()
        self.message_user(
            request,
            f'Сегмент «{source.emoji} {source.name}» ({source.code}) '
            f'применён к {affected} торговым точкам.',
            level=messages.SUCCESS,
        )

    def _send_broadcast_view(self, request, pk):
        """
        Создаёт рассылку по всем гостям сегмента для каждой активной торговой точки
        и перенаправляет на страницу новой рассылки для редактирования текста.
        """
        from apps.tenant.branch.models import Branch
        from apps.tenant.senler.models import Broadcast

        segment = RFSegment.objects.get(pk=pk)
        branches = Branch.objects.filter(is_active=True)

        created_ids = []
        for branch in branches:
            broadcast = Broadcast.objects.create(
                branch=branch,
                name=f'Рассылка: {segment.emoji} {segment.name} ({segment.code})',
                message_text='',
                audience_type='all',
            )
            broadcast.rf_segments.set([segment])
            created_ids.append(broadcast.pk)

        if len(created_ids) == 1:
            self.message_user(
                request,
                f'Создана рассылка для сегмента «{segment.name}». '
                f'Заполните текст сообщения и нажмите «Отправить».',
                level=messages.SUCCESS,
            )
            return HttpResponseRedirect(
                reverse(f'{self.admin_site.name}:senler_broadcast_change', args=[created_ids[0]])
            )
        elif created_ids:
            self.message_user(
                request,
                f'Создано {len(created_ids)} рассылок (по одной на точку) для сегмента «{segment.name}». '
                f'Перейдите в раздел Рассылки, заполните тексты и отправьте.',
                level=messages.SUCCESS,
            )
            return HttpResponseRedirect(reverse(f'{self.admin_site.name}:senler_broadcast_changelist'))
        else:
            self.message_user(
                request,
                'Нет активных торговых точек.',
                level=messages.WARNING,
            )
            return HttpResponseRedirect(reverse(f'{self.admin_site.name}:analytics_rfsegment_changelist'))

    def _export_senler_view(self, request, pk):
        """
        Выгружает .txt файл с VK ID всех гостей сегмента — по одному на строку.
        Формат для импорта в Senler (конструктор чат-ботов ВКонтакте).
        Supports ?mode=delivery and ?branches=1,2,3 query params.
        """
        from apps.tenant.analytics.models import GuestRFScoreDelivery
        from urllib.parse import quote

        segment = RFSegment.objects.get(pk=pk)
        mode = request.GET.get('mode', 'restaurant')

        ScoreModel = GuestRFScoreDelivery if mode == 'delivery' else GuestRFScore
        qs = ScoreModel.objects.filter(segment=segment)

        branch_ids = request.GET.get('branches', '')
        if branch_ids:
            try:
                ids = [int(x) for x in branch_ids.split(',') if x.strip()]
                qs = qs.filter(client__branch_profiles__branch_id__in=ids).distinct()
            except ValueError:
                pass

        # Get all VK IDs for guests in this segment
        vk_ids = qs.values_list('client__vk_id', flat=True)

        # Filter out None values and build the file content
        lines = [str(vk_id) for vk_id in vk_ids if vk_id]
        content = '\n'.join(lines)

        safe_name = f'senler_{segment.code}.txt'
        full_name = f'senler_{segment.code}_{segment.name}.txt'
        response = HttpResponse(content, content_type='text/plain; charset=utf-8')
        response['Content-Disposition'] = (
            f'attachment; filename="{safe_name}"; '
            f"filename*=UTF-8''{quote(full_name)}"
        )
        return response

    # ── Display columns ───────────────────────────────────────────────────────

    @admin.display(description='Код')
    def code_badge(self, obj):
        return format_html(
            '<span style="background:{};color:#fff;padding:3px 12px;'
            'border-radius:12px;font-weight:700;font-size:13px;letter-spacing:1px;">'
            '{} {}</span>',
            obj.color, obj.emoji, obj.code,
        )

    @admin.display(description='Область применения', ordering='branch__name')
    def scope_col(self, obj):
        if obj.is_global:
            return format_html(
                '<span style="background:#e0f2fe;color:#0369a1;padding:3px 10px;'
                'border-radius:10px;font-weight:700;font-size:11px;">'
                '🌐 Все точки</span>'
            )
        return obj.branch.name if obj.branch else '—'

    @admin.display(description='Давность (дни)', ordering='recency_min')
    def recency_range_col(self, obj):
        return format_html(
            '<span style="font-family:monospace;">{}&thinsp;–&thinsp;{}</span>',
            obj.recency_min, obj.recency_max,
        )

    @admin.display(description='Частота (визиты)', ordering='frequency_min')
    def frequency_range_col(self, obj):
        return format_html(
            '<span style="font-family:monospace;">{}&thinsp;–&thinsp;{}</span>',
            obj.frequency_min, obj.frequency_max,
        )

    @admin.display(description='Гостей сейчас')
    def guests_count_col(self, obj):
        if not obj.pk:
            return '—'
        count = obj.guests.count()
        if not count:
            return mark_safe('<span style="color:var(--body-quiet-color,#aaa);">0</span>')
        return format_html('<strong>{}</strong>', count)

    @admin.display(description='Подсказка')
    def hint_preview_col(self, obj):
        if not obj.hint:
            return mark_safe('<span style="color:#aaa;">—</span>')
        # Show first line as a tooltip-enabled preview
        first_line = obj.hint.split('\n')[0]
        preview = first_line[:50] + ('…' if len(first_line) > 50 else '')
        tooltip = obj.hint.replace('\n', '&#10;')
        return format_html(
            '<span title="{}" style="cursor:help;border-bottom:1px dotted #aaa;">'
            '{}</span>',
            tooltip, preview,
        )

    @admin.display(description='Действия')
    def actions_col(self, obj):
        if not obj.pk:
            return '—'

        broadcast_url = reverse(f'{self.admin_site.name}:analytics_rfsegment_send_broadcast', args=[obj.pk])
        export_url = reverse(f'{self.admin_site.name}:analytics_rfsegment_export_senler', args=[obj.pk])

        return format_html(
            '<div style="white-space:nowrap;">'
            '<a class="button" href="{}" '
            'onclick="return confirm(\'Создать рассылку для сегмента «{}»?\');" '
            'style="font-size:11px;padding:4px 10px;margin-right:4px;'
            'background:#4a76a8;color:#fff;border:none;border-radius:4px;'
            'text-decoration:none;display:inline-block;">'
            '📨 Рассылка</a>'
            '<a class="button" href="{}" '
            'style="font-size:11px;padding:4px 10px;'
            'background:#5181b8;color:#fff;border:none;border-radius:4px;'
            'text-decoration:none;display:inline-block;">'
            '📥 Senler</a>'
            '</div>',
            broadcast_url, obj.name,
            export_url,
        )


# ── GuestRFScore filters ──────────────────────────────────────────────────────

class RScoreFilter(admin.SimpleListFilter):
    title = 'R-балл'
    parameter_name = 'r_score'

    def lookups(self, request, model_admin):
        scores = (
            GuestRFScore.objects
            .values_list('r_score', flat=True)
            .distinct()
            .order_by('r_score')
        )
        return [(s, f'R{s}') for s in scores]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(r_score=self.value())
        return queryset


class FScoreFilter(admin.SimpleListFilter):
    title = 'F-балл'
    parameter_name = 'f_score'

    def lookups(self, request, model_admin):
        scores = (
            GuestRFScore.objects
            .values_list('f_score', flat=True)
            .distinct()
            .order_by('f_score')
        )
        return [(s, f'F{s}') for s in scores]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(f_score=self.value())
        return queryset


# ── GuestRFScore admin ────────────────────────────────────────────────────────

@admin.register(GuestRFScore, site=tenant_admin)
class GuestRFScoreAdmin(admin.ModelAdmin):
    list_display = (
        'client_col', 'branch_col',
        'recency_days', 'frequency', 'score_col',
        'segment_badge_col', 'calculated_at',
    )
    list_display_links = ('client_col',)
    list_filter = ('segment', RScoreFilter, FScoreFilter, 'client__branch_profiles__branch')
    search_fields = ('client__first_name', 'client__last_name')
    list_select_related = ('client', 'segment')
    readonly_fields = ('calculated_at',)

    fieldsets = (
        (None, {
            'fields': ('client', 'segment'),
        }),
        ('RF-метрики', {
            'fields': (('recency_days', 'r_score'), ('frequency', 'f_score')),
        }),
        ('Служебное', {
            'fields': ('calculated_at',),
            'classes': ('collapse',),
        }),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('client', 'segment')

    @admin.display(description='Гость', ordering='client__first_name')
    def client_col(self, obj):
        c = obj.client
        return c.first_name or f'vk{c.vk_id}'

    @admin.display(description='Точки')
    def branch_col(self, obj):
        names = list(
            obj.client.branch_profiles.select_related('branch')
            .values_list('branch__name', flat=True)
        )
        return ', '.join(names) if names else '—'

    @admin.display(description='R / F', ordering='r_score')
    def score_col(self, obj):
        return format_html(
            '<span style="font-family:monospace;font-weight:700;font-size:13px;">'
            'R{}&thinsp;F{}</span>',
            obj.r_score, obj.f_score,
        )

    @admin.display(description='Сегмент', ordering='segment__name')
    def segment_badge_col(self, obj):
        return _segment_badge(obj.segment)


# ── RFMigrationLog admin ──────────────────────────────────────────────────────

@admin.register(RFMigrationLog, site=tenant_admin)
class RFMigrationLogAdmin(admin.ModelAdmin):
    list_display = (
        'client_col', 'branch_col',
        'from_seg_badge', 'arrow_col', 'to_seg_badge',
        'created_at',
    )
    list_display_links = ('client_col',)
    list_filter = ('from_segment', 'to_segment', 'client__branch_profiles__branch')
    search_fields = ('client__first_name', 'client__last_name')
    list_select_related = ('client', 'from_segment', 'to_segment')
    date_hierarchy = 'created_at'
    readonly_fields = ('created_at',)

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'client', 'from_segment', 'to_segment',
        )

    @admin.display(description='Гость', ordering='client__first_name')
    def client_col(self, obj):
        c = obj.client
        return c.first_name or f'vk{c.vk_id}'

    @admin.display(description='Точки')
    def branch_col(self, obj):
        names = list(
            obj.client.branch_profiles.select_related('branch')
            .values_list('branch__name', flat=True)
        )
        return ', '.join(names) if names else '—'

    @admin.display(description='Из сегмента', ordering='from_segment__name')
    def from_seg_badge(self, obj):
        return _segment_badge(obj.from_segment)

    @admin.display(description='')
    def arrow_col(self, obj):
        return mark_safe('<span style="color:var(--body-quiet-color,#aaa);font-size:16px;">→</span>')

    @admin.display(description='В сегмент', ordering='to_segment__name')
    def to_seg_badge(self, obj):
        return _segment_badge(obj.to_segment)


# ── RFSettings admin ──────────────────────────────────────────────────────────


class RFSettingsAdminForm(forms.ModelForm):
    """
    Форма с дополнительным служебным полем «Применить настройки ко всем кафе».

    Если чекбокс установлен — после сохранения записи её R/F-пороги
    и analysis_period будут скопированы во все RFSettings активных торговых
    точек. На странице подтверждения пользователь увидит сводку и сможет
    отменить операцию.
    """

    apply_to_all_branches = forms.BooleanField(
        required=False,
        label='Применить настройки ко всем кафе',
        help_text=(
            'После сохранения скопировать пороги R/F и период анализа во все '
            'торговые точки. Текущие значения этих полей будут перезаписаны. '
            'Поле «Дата обнуления статистики» НЕ копируется.'
        ),
    )

    class Meta:
        model = RFSettings
        fields = '__all__'


@admin.register(RFSettings, site=tenant_admin)
class RFSettingsAdmin(admin.ModelAdmin):
    form = RFSettingsAdminForm

    list_display = (
        'scope_col', 'analysis_period',
        'r_thresholds_col', 'f_thresholds_col',
        'stats_reset_date', 'updated_at',
    )
    list_filter = ('analysis_period',)
    search_fields = ('branch__name',)
    readonly_fields = ('created_at', 'updated_at', 'effective_thresholds_preview')
    actions = ['apply_to_all_action', 'apply_to_selected_action']

    fieldsets = (
        ('Область применения', {
            'fields': ('branch', 'analysis_period'),
            'description': (
                '<div style="background:#f0f9ff;border-left:4px solid #0284c7;padding:10px 14px;'
                'border-radius:4px;margin-bottom:8px;line-height:1.5;">'
                '🌐 <b>Оставьте «Торговая точка» пустым</b> — создадутся общие настройки '
                '«Все точки» (используются как fallback для всех кафе без своих настроек). '
                '<br>📍 <b>Выберите конкретное кафе</b> — настройки этой точки переопределят общие.'
                '</div>'
                '<b>Период анализа</b> — за сколько дней назад считать визиты для расчёта частоты F.'
            ),
        }),
        ('🎯 Текущие пороги (предпросмотр)', {
            'fields': ('effective_thresholds_preview',),
            'description': (
                'Так будут размечены гости по матрице RF. Меняйте поля ниже — превью обновится '
                'после сохранения.'
            ),
        }),
        ('R-пороги — Давность последнего визита (дни)', {
            'fields': (('r_fresh_max', 'r_warm_max', 'r_cooling_max'),),
            'description': (
                '<div style="line-height:1.7;">'
                '🟢 <b>R3 «Свежий»</b> — гости с давностью <b>≤ R3-границы</b> (вернулись недавно).<br>'
                '🟡 <b>R2 «Тёплый»</b> — давность от <b>R3+1</b> до <b>R2-границы</b>.<br>'
                '🟠 <b>R1 «Остывший»</b> — давность от <b>R2+1</b> до <b>R1-границы</b>.<br>'
                '🔴 <b>R0 «Холодный»</b> — давность <b>больше R1-границы</b> (давно не видели).'
                '</div>'
                '<b>Значения должны строго возрастать:</b> R3 &lt; R2 &lt; R1.'
            ),
        }),
        ('F-пороги — Количество визитов за период', {
            'fields': (('f_rare_max', 'f_moderate_max'),),
            'description': (
                '<div style="line-height:1.7;">'
                '⚪ <b>F1 «Редко»</b> — визитов <b>≤ F1-границы</b>.<br>'
                '🔵 <b>F2 «Умеренно»</b> — от <b>F1+1</b> до <b>F2-границы</b>.<br>'
                '🟣 <b>F3 «Часто»</b> — <b>больше F2-границы</b> (VIP-гости).'
                '</div>'
                '<b>F1 должно быть строго меньше F2.</b>'
            ),
        }),
        ('🚀 Применить ко всем точкам', {
            'fields': ('apply_to_all_branches',),
            'description': (
                '<div style="background:#fef3c7;border-left:4px solid #f59e0b;padding:10px 14px;'
                'border-radius:4px;line-height:1.5;">'
                'Поставьте галочку — после нажатия «Сохранить» вы попадёте на страницу '
                'подтверждения с предпросмотром и сможете одним кликом раскатить настройки '
                'на все активные торговые точки. Поле «Дата обнуления статистики» '
                '<u>НЕ копируется</u> — оно индивидуально для каждой точки.'
                '</div>'
            ),
        }),
        ('Обнуление статистики', {
            'fields': ('stats_reset_date',),
            'classes': ('collapse',),
            'description': (
                'Если задана дата — визиты ДО неё игнорируются при RF-расчёте. '
                'Полезно после смены концепции или ребрендинга. '
                '<b>Это поле НЕ копируется при «Применить ко всем кафе»</b>.'
            ),
        }),
        ('Служебное', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    # ── List columns ──────────────────────────────────────────────────────────

    @admin.display(description='Область применения', ordering='branch__name')
    def scope_col(self, obj):
        if obj.is_global:
            return format_html(
                '<span style="background:#e0f2fe;color:#0369a1;padding:3px 10px;'
                'border-radius:10px;font-weight:700;font-size:11px;">'
                '🌐 Все точки</span>'
            )
        return obj.branch.name if obj.branch else '—'

    @admin.display(description='R: ≤ R3 / ≤ R2 / ≤ R1')
    def r_thresholds_col(self, obj):
        return format_html(
            '<span style="font-family:monospace;">{} / {} / {}</span>',
            obj.r_fresh_max, obj.r_warm_max, obj.r_cooling_max,
        )

    @admin.display(description='F: ≤ F1 / ≤ F2')
    def f_thresholds_col(self, obj):
        return format_html(
            '<span style="font-family:monospace;">{} / {}</span>',
            obj.f_rare_max, obj.f_moderate_max,
        )

    @admin.display(description='Текущие пороги (предпросмотр)')
    def effective_thresholds_preview(self, obj):
        if not obj.pk:
            return '—'
        t = obj.thresholds_dict()
        return format_html(
            '<div style="font-family:monospace;line-height:1.6;">'
            'R3: 0–{r_fresh_max} дн. &nbsp;|&nbsp; '
            'R2: {r2_lo}–{r_warm_max} дн. &nbsp;|&nbsp; '
            'R1: {r1_lo}–{r_cooling_max} дн. &nbsp;|&nbsp; '
            'R0: &gt;{r_cooling_max} дн.<br>'
            'F1: 1–{f_rare_max} виз. &nbsp;|&nbsp; '
            'F2: {f2_lo}–{f_moderate_max} виз. &nbsp;|&nbsp; '
            'F3: {f3_lo}+ виз.'
            '</div>',
            r_fresh_max    = t['r_fresh_max'],
            r_warm_max     = t['r_warm_max'],
            r_cooling_max  = t['r_cooling_max'],
            f_rare_max     = t['f_rare_max'],
            f_moderate_max = t['f_moderate_max'],
            r2_lo          = t['r_fresh_max'] + 1,
            r1_lo          = t['r_warm_max'] + 1,
            f2_lo          = t['f_rare_max'] + 1,
            f3_lo          = t['f_moderate_max'] + 1,
        )

    # ── Save logic: handle the apply_to_all checkbox ─────────────────────────

    def save_model(self, request, obj, form, change):
        """
        Обычное сохранение. Если стоит галочка «Применить ко всем кафе» —
        response_change/response_add ниже сам перенаправляет на confirm-page.
        """
        super().save_model(request, obj, form, change)

    # ── После save: если стоит галочка — сразу на confirm-page ──────────────

    def _redirect_to_apply_all(self, request, obj):
        return HttpResponseRedirect(
            reverse(
                f'{self.admin_site.name}:analytics_rfsettings_apply_all',
                args=[obj.pk],
            )
        )

    def response_add(self, request, obj, post_url_continue=None):
        if request.POST.get('apply_to_all_branches'):
            return self._redirect_to_apply_all(request, obj)
        return super().response_add(request, obj, post_url_continue)

    def response_change(self, request, obj):
        if request.POST.get('apply_to_all_branches'):
            return self._redirect_to_apply_all(request, obj)
        return super().response_change(request, obj)

    # ── Custom URLs: confirmation page for mass-apply ─────────────────────────

    def get_urls(self):
        urls = super().get_urls()
        return [
            path(
                '<int:pk>/apply-all/',
                self.admin_site.admin_view(self._apply_to_all_view),
                name='analytics_rfsettings_apply_all',
            ),
        ] + urls

    def _apply_to_all_view(self, request, pk):
        """
        Промежуточная страница «Вы уверены, что хотите применить
        настройки ко всем точкам? Текущие значения будут заменены».
        """
        from apps.tenant.branch.models import Branch
        try:
            obj = RFSettings.objects.get(pk=pk)
        except RFSettings.DoesNotExist:
            self.message_user(request, 'Запись не найдена.', level=messages.ERROR)
            return HttpResponseRedirect(reverse(f'{self.admin_site.name}:analytics_rfsettings_changelist'))

        # POST = пользователь подтвердил.
        if request.method == 'POST' and request.POST.get('_apply_all_confirmed') == '1':
            affected = obj.apply_thresholds_to_all_branches()
            self.message_user(
                request,
                f'Готово: настройки применены к {affected} торговым точкам.',
                level=messages.SUCCESS,
            )
            return HttpResponseRedirect(reverse(f'{self.admin_site.name}:analytics_rfsettings_changelist'))

        # GET = показываем подтверждение.
        active_branches = list(Branch.objects.filter(is_active=True).order_by('name'))
        ctx = {
            'title': 'Применить RF-настройки ко всем кафе?',
            'object': obj,
            'thresholds': obj.thresholds_dict(),
            'branches': active_branches,
            'branches_count': len(active_branches),
            'opts': self.model._meta,
            'app_label': self.model._meta.app_label,
            'has_view_permission': True,
            'site_header': getattr(self.admin_site, 'site_header', 'Администрирование'),
            'site_title':  getattr(self.admin_site, 'site_title',  'Администрирование'),
        }
        return TemplateResponse(
            request,
            'admin/analytics/rfsettings/apply_all_confirm.html',
            ctx,
        )

    # ── Admin actions ─────────────────────────────────────────────────────────

    @admin.action(description='Применить пороги выбранной записи ко ВСЕМ точкам')
    def apply_to_all_action(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(
                request,
                'Выберите ровно одну запись-источник: её пороги будут раскопированы '
                'во все активные торговые точки.',
                level=messages.WARNING,
            )
            return
        source = queryset.first()
        affected = source.apply_thresholds_to_all_branches()
        self.message_user(
            request,
            f'Пороги из «{source.scope_label}» применены к {affected} торговым точкам.',
            level=messages.SUCCESS,
        )

    @admin.action(description='Скопировать пороги из «Все точки» в выбранные записи')
    def apply_to_selected_action(self, request, queryset):
        global_obj = RFSettings.get_global()
        if not global_obj:
            self.message_user(
                request,
                'Запись «Все точки» (с пустым полем «Торговая точка») ещё не создана. '
                'Сначала создайте её на этой странице.',
                level=messages.WARNING,
            )
            return

        # Применяем только к записям с непустой точкой (саму глобальную не трогаем).
        target_ids = list(
            queryset.exclude(branch__isnull=True).values_list('branch_id', flat=True)
        )
        if not target_ids:
            self.message_user(
                request,
                'В выборке нет записей конкретных торговых точек.',
                level=messages.WARNING,
            )
            return
        affected = global_obj.apply_thresholds_to_branches(target_ids)
        self.message_user(
            request,
            f'Пороги из «Все точки» скопированы в {affected} записей.',
            level=messages.SUCCESS,
        )


# ── BranchSegmentSnapshot admin ───────────────────────────────────────────────

@admin.register(BranchSegmentSnapshot, site=tenant_admin)
class BranchSegmentSnapshotAdmin(admin.ModelAdmin):
    list_display = ('date', 'branch', 'segment_badge_col', 'guests_count', 'updated_at')
    list_filter = ('branch', 'segment')
    date_hierarchy = 'date'
    readonly_fields = ('created_at', 'updated_at')

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('branch', 'segment')

    @admin.display(description='Сегмент', ordering='segment__name')
    def segment_badge_col(self, obj):
        return _segment_badge(obj.segment)


# ── KnowledgeBaseDocument admin ───────────────────────────────────────────────

@admin.register(KnowledgeBaseDocument, site=tenant_admin)
class KnowledgeBaseDocumentAdmin(admin.ModelAdmin):
    list_display = ('title', 'is_active', 'created_at', 'updated_at', 'has_text_col')
    list_filter = ('is_active',)
    search_fields = ('title',)
    readonly_fields = ('extracted_text', 'created_at', 'updated_at')

    fieldsets = (
        (None, {
            'fields': ('title', 'file', 'is_active'),
            'description': (
                'Загрузите Word (.docx) или текстовый (.txt) файл с инструкциями для ИИ-анализа отзывов. '
                'Текст извлекается автоматически при сохранении.'
            ),
        }),
        ('Извлечённый текст', {
            'fields': ('extracted_text',),
            'classes': ('collapse',),
            'description': 'Заполняется автоматически. Используется как дополнительный контекст для ИИ.',
        }),
        ('Служебное', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Текст извлечён', boolean=True)
    def has_text_col(self, obj):
        return bool(obj.extracted_text)