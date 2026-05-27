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

    fieldsets = (
        (None, {
            'fields': ('company',),
        }),
        ('Брендинг', {
            'fields': ('logotype_image', 'logotype_preview', 'coin_image', 'coin_preview'),
            'description': 'Опционально. Загружайте только при подключении платного брендинга.',
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
