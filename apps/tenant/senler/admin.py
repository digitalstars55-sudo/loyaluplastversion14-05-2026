from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from apps.shared.config.admin_sites import tenant_admin

from .models import (
    AudienceType, AutoBroadcastTemplate, AutoBroadcastType,
    Broadcast, BroadcastRecipient, BroadcastSend,
    RecipientStatus, SendStatus, SenlerConfig,
)
from .services import create_send, resolve_recipients


# ── SenlerConfig ──────────────────────────────────────────────────────────────

@admin.register(SenlerConfig, site=tenant_admin)
class SenlerConfigAdmin(admin.ModelAdmin):
    list_display  = ['branch', 'vk_group_id', 'is_active', 'updated_at']
    list_filter   = ['is_active']
    search_fields = ['branch__name']

    fieldsets = [
        ('Торговая точка', {
            'fields': ['branch', 'is_active'],
        }),
        ('VK API', {
            'fields': ['vk_group_id', 'vk_community_token'],
            'description': (
                'Community access token выдаётся в разделе «Управление → Настройки → '
                'Работа с API» вашего VK-сообщества. Убедитесь, что включены права '
                '<b>messages</b>.'
            ),
        }),
        ('Callback API', {
            'fields': ['_callback_url', 'vk_callback_confirmation', 'vk_callback_secret'],
            'description': (
                'Настройте в VK: Управление → Работа с API → Callback API. '
                'Скопируйте URL ниже в поле «Адрес» в настройках Callback API вашей группы. '
                'Секретный ключ — произвольная строка, одинаковая здесь и в VK. '
                '<b>Обязательно заполните секрет</b> — иначе эндпоинт принимает запросы от кого угодно.'
            ),
        }),
        ('Служебное', {
            'fields': ['longpoll_ts'],
            'classes': ['collapse'],
            'description': 'Автоматически обновляется системой. Не редактировать вручную.',
        }),
        ('Заметки', {
            'fields': ['notes'],
            'classes': ['collapse'],
        }),
    ]

    readonly_fields = ['_callback_url']

    def _callback_url(self, obj):
        from django.db import connection
        domain = connection.tenant.domains.filter(is_primary=True).first()
        if domain:
            url = f'https://{domain.domain}/api/v1/vk/callback/'
        else:
            url = '/api/v1/vk/callback/'
        return format_html(
            '<input type="text" value="{}" readonly '
            'style="font-family:monospace;width:420px;background:#f5f5f5;border:1px solid #ccc;'
            'padding:6px 10px;border-radius:4px;cursor:text;" '
            'onclick="this.select()" />',
            url,
        )
    _callback_url.short_description = 'URL для Callback API'

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.base_fields['vk_community_token'].widget.attrs.update({
            'autocomplete': 'off',
            'style': 'font-family: monospace; width: 420px;',
        })
        return form


# ── BroadcastSend inline (inside Broadcast change view) ───────────────────────

class BroadcastSendInline(admin.TabularInline):
    model           = BroadcastSend
    extra           = 0
    can_delete      = False
    max_num         = 0
    show_change_link = True
    verbose_name         = 'Отправка'
    verbose_name_plural  = 'История отправок'
    ordering = ['-created_at']

    fields = [
        'status', 'trigger_type', 'triggered_by',
        'recipients_count', 'sent_count', 'failed_count', 'skipped_count',
        'started_at', 'finished_at',
    ]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return request.user.is_superuser


# ── BroadcastRecipient inline (inside BroadcastSend change view) ──────────────

class BroadcastRecipientInline(admin.TabularInline):
    model           = BroadcastRecipient
    extra           = 0
    can_delete      = False
    max_num         = 0
    verbose_name         = 'Получатель'
    verbose_name_plural  = 'Получатели'

    fields         = ['vk_id', 'client_branch', 'status', 'sent_at', 'error']
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return request.user.is_superuser


# ── Broadcast ─────────────────────────────────────────────────────────────────

_AUDIENCE_JS = """
<script>
(function () {
  function sync() {
    var sel = document.querySelector('[name="audience_type"]');
    if (!sel) return;
    var isSpec = sel.value === 'specific';
    document.querySelectorAll('.field-gender_filter, .field-rf_segments, .fieldset-all-filters')
      .forEach(function (el) { el.style.display = isSpec ? 'none' : ''; });
    document.querySelectorAll('.field-specific_clients, .fieldset-specific-filters')
      .forEach(function (el) { el.style.display = isSpec ? '' : 'none'; });
  }
  document.addEventListener('DOMContentLoaded', function () {
    sync();
    var sel = document.querySelector('[name="audience_type"]');
    if (sel) sel.addEventListener('change', sync);
  });
})();
</script>
"""


@admin.register(Broadcast, site=tenant_admin)
class BroadcastAdmin(admin.ModelAdmin):
    list_display  = [
        'name', 'branch', 'audience_label',
        'send_count_display', 'last_sent_display', 'send_button',
    ]
    list_filter   = ['branch', 'audience_type', 'gender_filter']
    search_fields = ['name', 'message_text']

    filter_horizontal    = ['rf_segments']
    autocomplete_fields  = ['specific_clients']
    # NOTE: ClientBranchAdmin must define search_fields for autocomplete to work.

    readonly_fields = ['_js_hook', '_ai_btn', 'recipient_count_preview', 'send_button_detail']

    fieldsets = [
        ('Основная информация', {
            'fields': ['branch', 'name'],
        }),
        ('Сообщение', {
            'fields': ['message_text', '_ai_btn', 'image'],
        }),
        ('Аудитория', {
            'fields': ['_js_hook', 'audience_type'],
            'description': (
                '<b>Все оцифрованные</b> — все гости с VK ID.'
                ' Допполнительные фильтры появятся ниже.<br>'
                '<b>Конкретные пользователи</b> — точечная рассылка.'
                ' Остальные фильтры игнорируются.'
            ),
        }),
        ('Фильтры (только для «Все оцифрованные»)', {
            'fields': ['gender_filter', 'rf_segments'],
            'classes': ['collapse', 'fieldset-all-filters'],
            'description': (
                'Фильтры применяются одновременно (AND).'
                ' Несколько сегментов — OR между ними (в хотя бы одном).'
            ),
        }),
        ('Конкретные гости (только для «Конкретные пользователи»)', {
            'fields': ['specific_clients'],
            'classes': ['collapse', 'fieldset-specific-filters'],
        }),
        ('Охват', {
            'fields': ['recipient_count_preview', 'send_button_detail'],
            'classes': ['collapse'],
        }),
    ]

    inlines = [BroadcastSendInline]

    # ── Custom URL: "Send Now" ─────────────────────────────────────────────────

    def get_urls(self):
        urls = super().get_urls()
        return [
            path(
                '<int:pk>/send/',
                self.admin_site.admin_view(self._send_view),
                name='senler_broadcast_send',
            ),
        ] + urls

    def _send_view(self, request, pk):
        # Рассылка ВСЕГДА уходит в celery, даже маленькая.
        # Раньше run_broadcast вызывался синхронно в HTTP-запросе —
        # gunicorn убивал воркера по 30-сек таймауту на больших сегментах,
        # рассылка зависала в RUNNING/PENDING, юзер кликал повторно
        # и плодил дубли. Теперь HTTP отвечает мгновенно, прогресс
        # видно в инлайне «История рассылок» ниже.
        from django.db import connection
        from apps.tenant.senler.tasks import run_broadcast_task

        broadcast = Broadcast.objects.select_related('branch').get(pk=pk)
        send = create_send(
            broadcast,
            triggered_by=request.user.username,
            trigger_type='manual',
        )
        try:
            run_broadcast_task.delay(connection.schema_name, [send.id])
            self.message_user(
                request,
                f'Рассылка «{broadcast.name}» поставлена в очередь. '
                f'Прогресс — в разделе «История рассылок» ниже. '
                f'Обновите страницу через минуту, чтобы увидеть результат.',
            )
        except Exception as exc:
            send.status = SendStatus.FAILED
            send.error_message = f'Не удалось поставить в очередь: {exc}'
            send.save(update_fields=['status', 'error_message'])
            self.message_user(
                request,
                f'Не удалось запустить рассылку: {exc}',
                level=messages.ERROR,
            )

        return HttpResponseRedirect(
            reverse(f'{self.admin_site.name}:senler_broadcast_change', args=[pk])
        )

    # ── Readonly fields ────────────────────────────────────────────────────────

    def _js_hook(self, obj):
        """Invisible field that injects the audience toggle JavaScript."""
        return mark_safe(_AUDIENCE_JS)
    _js_hook.short_description = ''

    def recipient_count_preview(self, obj):
        if not obj or not obj.pk:
            return '—'
        try:
            count = resolve_recipients(obj).count()
            return f'~{count} получателей при текущих настройках'
        except Exception as exc:
            return f'Ошибка подсчёта: {exc}'
    recipient_count_preview.short_description = 'Охват аудитории'

    def send_button_detail(self, obj):
        if not obj or not obj.pk:
            return '—'
        url = reverse(f'{self.admin_site.name}:senler_broadcast_send', args=[obj.pk])
        return format_html(
            '<a class="button" href="{}" '
            'onclick="return confirm(\'Запустить рассылку прямо сейчас?\');">'
            '▶ Запустить рассылку</a>',
            url,
        )
    send_button_detail.short_description = 'Действие'

    # ── List display helpers ───────────────────────────────────────────────────

    def audience_label(self, obj):
        parts = [obj.get_audience_type_display()]
        if obj.audience_type == AudienceType.ALL:
            if obj.gender_filter != 'all':
                parts.append(obj.get_gender_filter_display())
            segs = obj.rf_segments.all()
            if segs.exists():
                parts.append(', '.join(str(s) for s in segs))
        return ' · '.join(parts)
    audience_label.short_description = 'Аудитория'

    def send_count_display(self, obj):
        return obj.sends.count()
    send_count_display.short_description = 'Отправок'

    def last_sent_display(self, obj):
        last = obj.sends.order_by('-created_at').first()
        return last.created_at.strftime('%d.%m.%Y %H:%M') if last else '—'
    last_sent_display.short_description = 'Последняя отправка'

    def send_button(self, obj):
        url = reverse(f'{self.admin_site.name}:senler_broadcast_send', args=[obj.pk])
        return format_html(
            '<a class="button" href="{}" '
            'onclick="return confirm(\'Запустить рассылку?\');">'
            '▶ Отправить</a>',
            url,
        )
    send_button.short_description = ''

    def _ai_btn(self, obj):
        return _ai_btn_html('id_message_text', 'broadcast')
    _ai_btn.short_description = ''


# ── BroadcastSend ─────────────────────────────────────────────────────────────

@admin.register(BroadcastSend, site=tenant_admin)
class BroadcastSendAdmin(admin.ModelAdmin):
    list_display  = [
        'send_label', 'status_badge', 'trigger_type', 'triggered_by',
        'recipients_count', 'sent_count', 'failed_count',
        'progress_display', 'created_at', 'finished_at',
    ]
    list_filter   = ['status', 'trigger_type']
    search_fields = ['broadcast__name', 'auto_broadcast_template__type', 'triggered_by']
    date_hierarchy = 'created_at'

    readonly_fields = [
        'broadcast', 'auto_broadcast_template', 'status', 'trigger_type', 'triggered_by',
        'created_at', 'started_at', 'finished_at',
        'recipients_count', 'sent_count', 'failed_count', 'skipped_count',
        'progress_bar', 'error_message', 'manage_actions',
    ]

    fieldsets = [
        ('Управление в ВК (24ч окно)', {
            'fields': ['manage_actions'],
            'description': (
                'VK API позволяет редактировать и удалять исходящие сообщения '
                'сообщества только в течение 24 часов после отправки. После '
                'этого окна VK вернёт ошибку — действие применится только к '
                'свежим получателям.'
            ),
        }),
        ('Рассылка', {
            'fields': ['broadcast', 'auto_broadcast_template', 'status', 'trigger_type', 'triggered_by'],
        }),
        ('Время', {
            'fields': ['created_at', 'started_at', 'finished_at'],
        }),
        ('Статистика', {
            'fields': [
                'recipients_count', 'sent_count', 'failed_count',
                'skipped_count', 'progress_bar',
            ],
        }),
        ('Ошибки', {
            'fields': ['error_message'],
            'classes': ['collapse'],
        }),
    ]

    inlines = [BroadcastRecipientInline]

    def has_add_permission(self, request):
        return request.user.is_superuser

    # Разрешаем удалять локальную запись для CANCELLED/FAILED/DONE (история чистится).
    def has_delete_permission(self, request, obj=None):
        if obj is None:
            return True
        return obj.status in (SendStatus.CANCELLED, SendStatus.FAILED, SendStatus.DONE)

    # ── Custom URLs: edit/delete/cancel ─────────────────────────────────────────

    def get_urls(self):
        urls = super().get_urls()
        return [
            path('<int:pk>/edit-in-vk/',   self.admin_site.admin_view(self._edit_in_vk_view),   name='senler_broadcastsend_edit_in_vk'),
            path('<int:pk>/delete-in-vk/', self.admin_site.admin_view(self._delete_in_vk_view), name='senler_broadcastsend_delete_in_vk'),
            path('<int:pk>/cancel/',       self.admin_site.admin_view(self._cancel_view),       name='senler_broadcastsend_cancel'),
        ] + urls

    def _edit_in_vk_view(self, request, pk):
        from apps.tenant.senler.services import edit_broadcast_send_in_vk
        send = BroadcastSend.objects.get(pk=pk)
        if request.method == 'POST':
            new_text = (request.POST.get('new_text') or '').strip()
            if not new_text:
                self.message_user(request, 'Текст не может быть пустым', level=messages.ERROR)
                return HttpResponseRedirect(
                    reverse(f'{self.admin_site.name}:senler_broadcastsend_change', args=[pk])
                )
            result = edit_broadcast_send_in_vk(send, new_text)
            msg = f'Изменено в ВК: {result["updated"]}; пропущено: {len(result["skipped"])}; ошибок: {len(result["errors"])}'
            if result['errors']:
                msg += '. Примеры ошибок: ' + '; '.join(result['errors'][:3])
            level = messages.WARNING if result['errors'] else messages.SUCCESS
            self.message_user(request, msg, level=level)
            return HttpResponseRedirect(
                reverse(f'{self.admin_site.name}:senler_broadcastsend_change', args=[pk])
            )
        # GET — render simple form
        from django.template.response import TemplateResponse
        return TemplateResponse(request, 'admin/senler/broadcastsend_edit_in_vk.html', {
            'send': send,
            'title': f'Изменить текст рассылки в ВК — {send}',
            'opts': self.model._meta,
            'back_url': reverse(f'{self.admin_site.name}:senler_broadcastsend_change', args=[pk]),
            'current_text': send.broadcast.message_text if send.broadcast_id else '',
        })

    def _delete_in_vk_view(self, request, pk):
        from apps.tenant.senler.services import delete_broadcast_send_in_vk
        from django.template.response import TemplateResponse
        send = BroadcastSend.objects.get(pk=pk)
        back_url = reverse(f'{self.admin_site.name}:senler_broadcastsend_change', args=[pk])
        if request.method != 'POST':
            # GET → страница подтверждения с настоящей {% csrf_token %} формой
            # (как у редактирования — LU-12, чтобы не падать на CSRF 403).
            return TemplateResponse(request, 'admin/senler/broadcastsend_confirm_action.html', {
                'send': send,
                'title': f'Удалить рассылку из ВК — {send}',
                'action_title': 'Удалить сообщения из ВК',
                'warning': 'Сообщения будут удалены у всех получателей (в пределах 24ч окна VK API). Действие необратимо.',
                'btn_label': '🗑️ Удалить из ВК',
                'btn_color': '#dc3545',
                'confirm_text': 'Удалить сообщения из ВК у всех получателей? Действие необратимо.',
                'opts': self.model._meta,
                'back_url': back_url,
            })
        result = delete_broadcast_send_in_vk(send)
        msg = f'Удалено в ВК: {result["deleted"]}; пропущено: {len(result["skipped"])}; ошибок: {len(result["errors"])}'
        if result['errors']:
            msg += '. Примеры ошибок: ' + '; '.join(result['errors'][:3])
        if result['deleted'] > 0 and not result['errors']:
            send.status = SendStatus.CANCELLED
            send.error_message = (send.error_message or '') + '\n[Удалено из ВК через админку]'
            send.save(update_fields=['status', 'error_message'])
        level = messages.WARNING if result['errors'] else messages.SUCCESS
        self.message_user(request, msg, level=level)
        return HttpResponseRedirect(
            reverse(f'{self.admin_site.name}:senler_broadcastsend_change', args=[pk])
        )

    def _cancel_view(self, request, pk):
        from django.template.response import TemplateResponse
        send = BroadcastSend.objects.get(pk=pk)
        back_url = reverse(f'{self.admin_site.name}:senler_broadcastsend_change', args=[pk])
        if request.method != 'POST':
            return TemplateResponse(request, 'admin/senler/broadcastsend_confirm_action.html', {
                'send': send,
                'title': f'Отменить рассылку — {send}',
                'action_title': 'Отменить рассылку',
                'warning': 'Рассылка ещё не была отправлена. Отмена остановит её запуск.',
                'btn_label': '⛔ Отменить рассылку',
                'btn_color': '#d97706',
                'confirm_text': 'Отменить эту рассылку? Она ещё не была отправлена.',
                'opts': self.model._meta,
                'back_url': back_url,
            })
        if send.status not in (SendStatus.PENDING, SendStatus.RUNNING):
            self.message_user(request, f'Нельзя отменить — статус «{send.get_status_display()}»', level=messages.WARNING)
        else:
            send.status = SendStatus.CANCELLED
            send.error_message = (send.error_message or '') + '\n[Отменено через админку]'
            send.save(update_fields=['status', 'error_message'])
            self.message_user(request, f'Запуск #{pk} отменён', level=messages.SUCCESS)
        return HttpResponseRedirect(back_url)

    def manage_actions(self, obj):
        if not obj or not obj.pk:
            return '—'
        edit_url   = reverse(f'{self.admin_site.name}:senler_broadcastsend_edit_in_vk',   args=[obj.pk])
        delete_url = reverse(f'{self.admin_site.name}:senler_broadcastsend_delete_in_vk', args=[obj.pk])
        cancel_url = reverse(f'{self.admin_site.name}:senler_broadcastsend_cancel',       args=[obj.pk])
        # LU-12: все действия — GET-ссылки на страницы подтверждения с настоящей
        # Django-формой {% csrf_token %} (как у редактирования). Раньше delete/cancel
        # были inline-формами с CSRF-токеном через JS-инъекцию → падали на 403.
        buttons = []
        if obj.status in (SendStatus.PENDING, SendStatus.RUNNING):
            buttons.append(
                f'<a class="button" href="{cancel_url}" style="margin-right:8px;">⛔ Отменить рассылку</a>'
            )
        if obj.status == SendStatus.DONE and obj.sent_count > 0:
            buttons.append(f'<a class="button" href="{edit_url}" style="margin-right:8px;">✏️ Изменить текст в ВК</a>')
            buttons.append(
                f'<a class="button" href="{delete_url}" '
                f'style="margin-right:8px;background:#dc3545;color:#fff;border-color:#dc3545;">'
                f'🗑️ Удалить из ВК</a>'
            )
        if not buttons:
            return mark_safe('<span style="color:#888;">Нет доступных действий для текущего статуса.</span>')
        return mark_safe(''.join(buttons))
    manage_actions.short_description = 'Действия'

    # ── Display helpers ────────────────────────────────────────────────────────

    def send_label(self, obj):
        if obj.broadcast_id:
            return obj.broadcast.name
        if obj.auto_broadcast_template_id:
            return str(obj.auto_broadcast_template)
        return '—'
    send_label.short_description = 'Рассылка'

    _STATUS_COLORS = {
        SendStatus.PENDING:   ('#6c757d', '⏳'),
        SendStatus.RUNNING:   ('#007bff', '🔄'),
        SendStatus.DONE:      ('#28a745', '✅'),
        SendStatus.FAILED:    ('#dc3545', '❌'),
        SendStatus.CANCELLED: ('#ffc107', '⛔'),
    }

    def status_badge(self, obj):
        color, icon = self._STATUS_COLORS.get(obj.status, ('#6c757d', '•'))
        return format_html(
            '<span style="color:{};font-weight:bold;">{} {}</span>',
            color, icon, obj.get_status_display(),
        )
    status_badge.short_description = 'Статус'

    def progress_display(self, obj):
        if not obj.recipients_count:
            return '—'
        pct = int(obj.sent_count / obj.recipients_count * 100)
        return f'{pct}% ({obj.sent_count}/{obj.recipients_count})'
    progress_display.short_description = 'Прогресс'

    def progress_bar(self, obj):
        if not obj.recipients_count:
            return '—'
        pct = int(obj.sent_count / obj.recipients_count * 100)
        fail_pct = int(obj.failed_count / obj.recipients_count * 100)
        return format_html(
            '<div style="width:300px;background:#e9ecef;border-radius:4px;overflow:hidden;">'
            '  <div style="width:{pct}%;background:#28a745;height:18px;display:inline-block;"></div>'
            '  <div style="width:{fp}%;background:#dc3545;height:18px;display:inline-block;"></div>'
            '</div>'
            '<br><small style="color:#6c757d;">'
            '  ✅ {sent} отправлено &nbsp; ❌ {fail} ошибок &nbsp; ⏭ {skip} пропущено'
            '  &nbsp;/ {total} всего ({pct}%)'
            '</small>',
            pct=pct, fp=fail_pct,
            sent=obj.sent_count, fail=obj.failed_count,
            skip=obj.skipped_count, total=obj.recipients_count,
        )
    progress_bar.short_description = 'Прогресс'


# ── Shared AI-generate button ─────────────────────────────────────────────────

_AI_BTN_JS = """
<script>
(function(){
  if (window._levoneAiGenerate) return;
  window._levoneAiGenerate = function(btn, textareaId, type, extraData) {
    var ta = document.getElementById(textareaId);
    var status = btn.nextElementSibling;
    btn.disabled = true;
    if (status) status.textContent = '⏳ Генерирую…';
    var body = Object.assign({draft: ta ? ta.value : '', type: type}, extraData || {});
    fetch('/admin/ai/generate/', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': (document.cookie.match('(^|;)\\s*csrftoken=([^;]+)') || [])[2] || ''
      },
      body: JSON.stringify(body)
    })
    .then(function(r){ return r.json(); })
    .then(function(data){
      btn.disabled = false;
      if (data.text) {
        if (ta) ta.value = data.text;
        if (status) status.textContent = '✓ Готово';
      } else {
        if (status) status.textContent = '✗ ' + (data.error || 'Ошибка');
      }
    })
    .catch(function(){ btn.disabled = false; if(status) status.textContent = '✗ Ошибка сети'; });
  };
})();
</script>
"""

_AI_BTN_STYLE = (
    'background:#4a76a8;color:#fff;border:none;border-radius:6px;'
    'padding:6px 14px;font-size:12px;font-weight:600;cursor:pointer;'
)


def _ai_btn_html(textarea_id, gen_type, extra_js=''):
    """Return safe HTML for the AI-generate button + injected JS."""
    return mark_safe(
        f'{_AI_BTN_JS}'
        f'<button type="button" style="{_AI_BTN_STYLE}" '
        f'onclick="_levoneAiGenerate(this, \'{textarea_id}\', \'{gen_type}\', {{{extra_js}}})">'
        f'🤖 Сгенерировать ИИ</button>'
        f'<span style="margin-left:10px;font-size:11px;color:#888;"></span>'
    )


# ── AutoBroadcastTemplate ─────────────────────────────────────────────────────

_TYPE_ICONS = {
    AutoBroadcastType.BIRTHDAY_7_DAYS: '🎂',
    AutoBroadcastType.BIRTHDAY_1_DAY:  '🎂',
    AutoBroadcastType.BIRTHDAY:        '🎉',
    AutoBroadcastType.AFTER_GAME_3H:   '🎮',
}

_BADGE = (
    'display:inline-block;padding:2px 8px;border-radius:10px;'
    'font-size:11px;font-weight:600;white-space:nowrap;'
)
_ACTIVE_BADGE   = _BADGE + 'background:#e8f5e9;color:#1b5e20;border:1px solid #a5d6a7;'
_INACTIVE_BADGE = _BADGE + 'background:#f5f5f5;color:#9e9e9e;border:1px solid #e0e0e0;'


@admin.register(AutoBroadcastTemplate, site=tenant_admin)
class AutoBroadcastTemplateAdmin(admin.ModelAdmin):
    list_display  = ['type_display', 'is_active', 'message_preview', 'updated_at']
    list_filter   = ['is_active']
    list_editable = ['is_active']
    readonly_fields = ['_ai_btn']

    fieldsets = [
        ('Триггер', {
            'fields': ['type', 'is_active'],
            'description': (
                'Если шаблон отсутствует или отключён — '
                'автоматическая рассылка для этого триггера не отправляется.'
            ),
        }),
        ('Сообщение', {
            'fields': ['message_text', '_ai_btn', 'image'],
        }),
    ]

    def _ai_btn(self, obj):
        # Pass the current template type so the AI knows what kind of message to write.
        # The type is read from the #id_type select at click-time via JS.
        return mark_safe(
            f'{_AI_BTN_JS}'
            f'<button type="button" style="{_AI_BTN_STYLE}" onclick="(function(){{'
            f'var t=document.getElementById(\'id_type\');'
            f'_levoneAiGenerate(this,\'id_message_text\',\'broadcast\','
            f'{{broadcast_type:t?t.value:\'\'}});'
            f'}})()">🤖 Сгенерировать ИИ</button>'
            f'<span style="margin-left:10px;font-size:11px;color:#888;"></span>'
        )
    _ai_btn.short_description = ''

    @admin.display(description='Триггер', ordering='type')
    def type_display(self, obj):
        icon = _TYPE_ICONS.get(obj.type, '📨')
        return format_html('{} {}', icon, obj.get_type_display())

    @admin.display(description='Статус', ordering='is_active')
    def is_active_badge(self, obj):
        if obj.is_active:
            return format_html('<span style="{}">✓ Активен</span>', _ACTIVE_BADGE)
        return format_html('<span style="{}">✗ Выключен</span>', _INACTIVE_BADGE)

    @admin.display(description='Текст')
    def message_preview(self, obj):
        preview = obj.message_text[:80]
        if len(obj.message_text) > 80:
            preview += '…'
        return preview
