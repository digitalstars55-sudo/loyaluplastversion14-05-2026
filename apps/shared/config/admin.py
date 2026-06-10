from django import forms
from django.contrib import admin
from django.utils.html import format_html

from apps.shared.config.admin_sites import public_admin
from .models import ClientConfig, POSType


class ClientConfigForm(forms.ModelForm):
    iiko_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=True),
        label='IIKO Пароль',
    )
    dooglys_api_token = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=True),
        label='Dooglys API Token',
    )

    class Meta:
        model = ClientConfig
        fields = '__all__'

    def clean_logotype_image(self):
        """Валидируем логотип только при НОВОЙ загрузке (не трогаем существующий)."""
        from django.core.files.uploadedfile import UploadedFile
        image = self.cleaned_data.get('logotype_image')
        if not isinstance(image, UploadedFile):
            return image  # файл не менялся — пропускаем
        if image.size > 1024 * 1024:
            raise forms.ValidationError('Логотип должен быть не больше 1 МБ.')
        name = (getattr(image, 'name', '') or '').lower()
        if not name.endswith('.png'):
            raise forms.ValidationError('Логотип должен быть в формате PNG (с прозрачным фоном).')
        try:
            from PIL import Image
            image.seek(0)
            w, h = Image.open(image).size
            image.seek(0)
        except Exception:
            return image  # не смогли прочитать размеры — не блокируем
        if max(w, h) and abs(w - h) > max(w, h) * 0.05:
            raise forms.ValidationError(f'Логотип должен быть квадратным (загружено {w}×{h}, рекомендуется 512×512).')
        if w < 256 or w > 2048:
            raise forms.ValidationError(f'Сторона логотипа — 256–2048 px (загружено {w}×{h}, рекомендуется 512×512).')
        return image

    def clean(self):
        cleaned_data = super().clean()
        pos_type = cleaned_data.get('pos_type')

        if pos_type == POSType.IIKO:
            for field in ('iiko_api_url', 'iiko_login', 'iiko_password'):
                if not cleaned_data.get(field):
                    self.add_error(field, 'Обязательное поле для iiko.')

        elif pos_type == POSType.DOOGLYS:
            for field in ('dooglys_api_url', 'dooglys_api_token'):
                if not cleaned_data.get(field):
                    self.add_error(field, 'Обязательное поле для Dooglys.')

        return cleaned_data


@admin.register(ClientConfig, site=public_admin)
class ClientConfigAdmin(admin.ModelAdmin):
    form = ClientConfigForm

    list_display = ('company', 'vk_group_name', 'pos_type', 'has_branding')
    list_filter = ('pos_type',)
    search_fields = ('company__name', 'vk_group_name')
    readonly_fields = ('logotype_preview', 'coin_preview')
    actions = ('apply_birthday_window_to_branches',)

    @admin.action(description='Применить окно ДР ко всем точкам сети')
    def apply_birthday_window_to_branches(self, request, queryset):
        from django.contrib import messages
        from django_tenants.utils import schema_context
        total_branches = 0
        for cfg in queryset:
            company = cfg.company
            schema = getattr(company, 'schema_name', None)
            if not schema:
                continue
            try:
                with schema_context(schema):
                    from apps.tenant.branch.models import BranchConfig
                    n = BranchConfig.objects.update(birthday_window_days=cfg.birthday_window_days)
                    total_branches += n
            except Exception as e:
                self.message_user(
                    request, f'{company.name}: ошибка — {e}', level=messages.ERROR,
                )
        self.message_user(
            request,
            f'Окно ДР ({", ".join(str(c.birthday_window_days) for c in queryset)} дн.) '
            f'применено к {total_branches} точкам.',
            level=messages.SUCCESS,
        )

    fieldsets = (
        (None, {
            'fields': ('company',),
        }),
        ('Брендинг', {
            'fields': ('logotype_image', 'logotype_preview', 'coin_image', 'coin_preview', 'brand_color', 'brand_color_secondary'),
            'description': (
                'Опционально. Логотип — PNG с прозрачным фоном, квадрат '
                '(512×512), до 1 МБ. Главный и акцентный цвета (#RRGGBB) '
                'перекрашивают весь VK мини-апп — производные оттенки '
                'генерируются автоматически.'
            ),
        }),
        ('ВКонтакте', {
            'fields': ('vk_group_id', 'vk_group_name'),
            'description': 'Используется для отображения кнопки «Подписаться» в приложении.',
        }),
        ('Сообщения для гостей', {
            'fields': ('code_prompt_message', 'quest_show_message'),
            'description': (
                'Тексты-подсказки, которые гость видит в приложении. '
                'Точка может переопределить их в своих настройках.'
            ),
        }),
        ('Подарок на день рождения', {
            'fields': ('birthday_window_days',),
            'description': (
                'Окно подарка ДР на уровне всей сети (±дней). Точка может '
                'переопределить в своих настройках. Чтобы проставить это значение '
                'сразу всем точкам — выделите эту запись в списке и выберите действие '
                '«Применить окно ДР ко всем точкам сети».'
            ),
        }),
        ('Игра через сториз (внешние пользователи)', {
            'fields': (
                'story_game_enabled',
                'story_min_order_amount', 'story_activation_minutes',
                'story_require_cafe_visit',
                'story_cafe_address',
                'story_activation_text', 'story_saved_text',
                'story_campaign_start', 'story_campaign_end',
            ),
            'description': (
                'Механика привлечения внешних пользователей через сториз. '
                'Подарки для сториз настраиваются у товаров (флаг «Подарок для игры '
                'через сториз») и подключаются к точкам. Активация подарка возможна '
                'только после ввода кода дня в кафе. Точка может переопределить '
                'часть полей в своих настройках.'
            ),
            'classes': ('collapse',),
        }),
        ('Кассовая система', {
            'fields': ('pos_type',),
            'description': 'Выберите систему — нужные поля появятся автоматически.',
        }),
        ('iiko', {
            'fields': ('iiko_api_url', 'iiko_login', 'iiko_password'),
            'classes': ('pos-section', 'pos-iiko'),
        }),
        ('Dooglys', {
            'fields': ('dooglys_api_url', 'dooglys_api_token'),
            'classes': ('pos-section', 'pos-dooglys'),
        }),
    )

    class Media:
        js = ('admin/config/js/pos_toggle.js',)

    # --- readonly previews ---

    @admin.display(description='Превью логотипа')
    def logotype_preview(self, obj):
        if obj.logotype_image:
            return format_html(
                '<img src="{}" style="max-height:80px; border-radius:8px; margin-top:4px;" />',
                obj.logotype_image.url,
            )
        return '—'

    @admin.display(description='Превью монеты')
    def coin_preview(self, obj):
        if obj.coin_image:
            return format_html(
                '<img src="{}" style="max-height:60px; border-radius:50%; margin-top:4px;" />',
                obj.coin_image.url,
            )
        return '—'

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if getattr(request.user, 'role', None) == 'network_admin':
            return qs.filter(company__in=request.user.companies.all())
        return qs

    def has_add_permission(self, request):
        if getattr(request.user, 'role', None) == 'network_admin':
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        if getattr(request.user, 'role', None) == 'network_admin':
            return False
        return super().has_delete_permission(request, obj)

    # --- list_display helpers ---

    @admin.display(boolean=True, description='Брендинг')
    def has_branding(self, obj):
        return bool(obj.logotype_image or obj.coin_image)
