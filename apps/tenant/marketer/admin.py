from django.contrib import admin, messages
from django.utils.html import format_html

from apps.shared.config.admin_sites import tenant_admin

from .models import MarketerPost, MarketerPostStatus, MarketerSettings


@admin.register(MarketerSettings, site=tenant_admin)
class MarketerSettingsAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'autopost_enabled', 'digest_enabled', 'digest_weekday', 'digest_hour', 'last_digest_at')
    readonly_fields = ('last_digest_at', 'created_at', 'updated_at')
    actions = ['generate_digest_now']

    fieldsets = (
        (None, {'fields': ('is_enabled', 'autopost_enabled')}),
        ('VK', {'fields': ('vk_group_id', 'vk_wall_token')}),
        ('Дайджест «Что нового?»', {'fields': ('digest_enabled', 'digest_weekday', 'digest_hour', 'last_digest_at')}),
        ('Знания', {'fields': ('brand_voice', 'extra_facts')}),
        ('Служебное', {'fields': ('created_at', 'updated_at')}),
    )

    def has_add_permission(self, request):
        # Одна запись на тенанта
        return not MarketerSettings.objects.exists()

    @admin.action(description='📝 Сгенерировать дайджест сейчас')
    def generate_digest_now(self, request, queryset):
        cfg = queryset.first()
        if not cfg or not cfg.is_enabled or not cfg.digest_enabled:
            self.message_user(
                request,
                'Сначала включите маркетолога и дайджест в настройках.',
                level=messages.WARNING,
            )
            return
        from apps.tenant.marketer.tasks import run_marketer_digest_for_tenant_task
        run_marketer_digest_for_tenant_task.delay(request.tenant.schema_name)
        self.message_user(
            request,
            'Генерация запущена — черновик появится в «Постах маркетолога» в течение минуты.',
            level=messages.SUCCESS,
        )


@admin.register(MarketerPost, site=tenant_admin)
class MarketerPostAdmin(admin.ModelAdmin):
    list_display = ('id', 'post_type', 'status_badge', 'text_preview', 'model_used', 'created_at', 'published_at', 'vk_link')
    list_filter = ('status', 'post_type')
    search_fields = ('text',)
    readonly_fields = ('context_snapshot', 'model_used', 'created_by', 'published_at', 'vk_post_id', 'error', 'created_at', 'updated_at')
    actions = ['publish_selected', 'reject_selected']

    @admin.display(description='Статус', ordering='status')
    def status_badge(self, obj):
        colors = {
            MarketerPostStatus.DRAFT: '#f0ad4e',
            MarketerPostStatus.PUBLISHED: '#5cb85c',
            MarketerPostStatus.REJECTED: '#999999',
            MarketerPostStatus.FAILED: '#d9534f',
        }
        return format_html(
            '<span style="color:#fff;background:{};padding:2px 8px;border-radius:8px;font-size:11px;">{}</span>',
            colors.get(obj.status, '#777'), obj.get_status_display(),
        )

    @admin.display(description='Текст')
    def text_preview(self, obj):
        text = obj.text[:120] + ('…' if len(obj.text) > 120 else '')
        return format_html('<span title="{}">{}</span>', obj.text, text)

    @admin.display(description='VK')
    def vk_link(self, obj):
        if not obj.vk_post_id:
            return '—'
        from .publisher import _resolve_group_id
        from .models import MarketerSettings as MS
        cfg = MS.objects.first()
        gid = _resolve_group_id(cfg) if cfg else None
        if not gid:
            return obj.vk_post_id
        return format_html(
            '<a href="https://vk.com/wall-{}_{}" target="_blank">открыть</a>',
            gid, obj.vk_post_id,
        )

    @admin.action(description='🚀 Опубликовать на стену')
    def publish_selected(self, request, queryset):
        from .publisher import publish_post
        ok = failed = 0
        for post in queryset:
            if publish_post(post):
                ok += 1
            else:
                failed += 1
        if ok:
            self.message_user(request, f'Опубликовано: {ok}.', level=messages.SUCCESS)
        if failed:
            self.message_user(
                request,
                f'Не опубликовано: {failed} — причина в поле «Ошибка» поста.',
                level=messages.WARNING,
            )

    @admin.action(description='❌ Отклонить')
    def reject_selected(self, request, queryset):
        n = queryset.filter(status=MarketerPostStatus.DRAFT).update(status=MarketerPostStatus.REJECTED)
        self.message_user(request, f'Отклонено черновиков: {n}.', level=messages.SUCCESS)
