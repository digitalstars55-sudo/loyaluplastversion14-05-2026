"""
Сериализаторы для мобильной авторизации. Аддитивно — никакие существующие
сериализаторы не затронуты.

Соответствие полей с типом `Profile` из мобильного `src/types.ts`:
    id, full_name, role, role_label, email, phone, branch_ids
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from rest_framework import serializers

User = get_user_model()


class LoginSerializer(serializers.Serializer):
    login = serializers.CharField(required=True, allow_blank=False, max_length=150)
    password = serializers.CharField(required=True, allow_blank=False, write_only=True)


class ProfileSerializer(serializers.ModelSerializer):
    """
    Совместимо с мобильным Profile interface.
    branch_ids зависят от тенанта — здесь возвращаем пустой список,
    мобайл сам подтянет через /api/v1/analytics/branches/.

    tenant_domain — primary domain первой компании пользователя. Мобайл
    после логина переключает API_BASE на этот домен.
    """
    full_name = serializers.SerializerMethodField()
    role_label = serializers.SerializerMethodField()
    branch_ids = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    tenant_domain = serializers.SerializerMethodField()
    tenant_name = serializers.SerializerMethodField()
    companies = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'username', 'full_name', 'role', 'role_label',
            'email', 'phone', 'city', 'birthday', 'birthday_set_at',
            'avatar_url', 'branch_ids',
            'tenant_domain', 'tenant_name', 'companies',
            'push_prefs',
        ]
        read_only_fields = fields

    def get_full_name(self, obj) -> str:
        full = (obj.get_full_name() or '').strip()
        return full or obj.username

    def get_role_label(self, obj) -> str:
        # Маппим Django-роли в человекочитаемые лейблы мобайла
        role = getattr(obj, 'role', None)
        if obj.is_superuser:
            return 'Владелец'
        if role == 'network_admin':
            return 'Управляющий'
        if role == 'client':
            return 'Просмотр'
        return getattr(obj, 'get_role_display', lambda: 'Пользователь')() or 'Пользователь'

    def get_branch_ids(self, obj) -> list[int]:
        # На public-схеме branches не существуют — отдаём пусто.
        # Мобайл подтянет их отдельным запросом по тенантскому домену.
        return []

    def get_avatar_url(self, obj) -> str | None:
        return None

    def _primary_company(self, obj):
        """Первая активная компания пользователя. Кэш на инстансе.
        Суперюзер без M2M-компаний → первая активная компания (он видит все)."""
        cached = getattr(obj, '_primary_company_cache', 'sentinel')
        if cached != 'sentinel':
            return cached
        try:
            company = obj.companies.filter(is_active=True).first() or obj.companies.first()
            if company is None and obj.is_superuser:
                from apps.shared.clients.models import Company
                company = (
                    Company.objects.filter(is_active=True)
                    .exclude(schema_name='public').order_by('id').first()
                )
        except Exception:
            company = None
        obj._primary_company_cache = company
        return company

    def get_tenant_domain(self, obj) -> str | None:
        """
        Полный домен primary тенанта пользователя — например 'demo.levone.ru'.
        Мобайл переключает API_BASE на этот домен после логина.
        """
        company = self._primary_company(obj)
        if not company:
            return None
        try:
            from apps.shared.clients.models import Domain
            d = Domain.objects.filter(tenant=company, is_primary=True).first() \
                or Domain.objects.filter(tenant=company).first()
            return d.domain if d else None
        except Exception:
            return None

    def get_tenant_name(self, obj) -> str | None:
        company = self._primary_company(obj)
        return company.name if company else None

    def get_companies(self, obj) -> list[dict]:
        """
        Все сети (тенанты), доступные пользователю — для переключателя в мобилке.
        Суперюзер видит все активные тенанты (как в вебе), остальные — свои из M2M.
        Каждый элемент: {id, name, domain}. Без домена — пропускаем.
        """
        try:
            from apps.shared.clients.models import Company, Domain
        except Exception:
            return []
        try:
            if obj.is_superuser:
                comps = list(
                    Company.objects.filter(is_active=True)
                    .exclude(schema_name='public').order_by('name')
                )
            else:
                comps = list(obj.companies.filter(is_active=True)) or list(obj.companies.all())
        except Exception:
            comps = []
        out: list[dict] = []
        for c in comps:
            try:
                d = (Domain.objects.filter(tenant=c, is_primary=True).first()
                     or Domain.objects.filter(tenant=c).first())
            except Exception:
                d = None
            if d:
                out.append({'id': c.pk, 'name': c.name, 'domain': d.domain})
        return out


class LoginResponseSerializer(serializers.Serializer):
    token = serializers.CharField()
    refresh = serializers.CharField()
    expires_at = serializers.DateTimeField()
    profile = ProfileSerializer()


class RefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField(required=True, allow_blank=False)


class PushTokenSerializer(serializers.Serializer):
    token = serializers.CharField(required=True, allow_blank=False, max_length=255)
    platform = serializers.ChoiceField(choices=['ios', 'android', 'web'])
