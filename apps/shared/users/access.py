"""
Per-tenant + per-branch RBAC helpers.

Модель: User.companies (M2M к Company/тенанту) — даёт доступ к тенанту.
       User.branch_access (JSONField) — опционально ограничивает по точкам внутри тенанта.

Helper'ы возвращают:
  has_tenant_access(user, schema_name) -> bool       — есть ли вообще доступ к тенанту
  user_allowed_branches(user, schema_name) -> set[int] | None
      → None  = нет ограничений (доступ ко всем точкам)
      → set() = совсем нет доступа (либо тенант не в companies, либо явный пустой список)
      → set(ids) = ограничен этим списком branch_id

SU (is_superuser=True) ВСЕГДА имеет полный доступ (returns None и True).
"""
from __future__ import annotations


def has_tenant_access(user, schema_name: str) -> bool:
    """Имеет ли user доступ к указанному тенанту в принципе."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.companies.filter(schema_name=schema_name).exists()


def user_allowed_branches(user, schema_name: str) -> set[int] | None:
    """
    Какие branch_id видны user'у в указанном тенанте.

    Возвращает:
      None       — нет ограничений (видит ВСЕ точки тенанта)
      set()      — нет доступа (тенант не в companies или явное "[]")
      set(ids)   — ограничен этим списком

    Источники (в порядке приоритета):
      1) User.branch_access[schema] (JSON, public schema) — из superadmin UI.
      2) StaffProfile.branch_access (M2M в tenant schema) — fallback, задаётся
         через мобильный StaffScreen (Ещё → Сотрудники).
      3) Без ограничений (None) — backward-compat.

    Использование в queryset:
        allowed = user_allowed_branches(request.user, schema_name)
        if allowed is None:
            qs = Branch.objects.all()
        else:
            qs = Branch.objects.filter(id__in=allowed)
    """
    if not user or not user.is_authenticated:
        return set()
    if user.is_superuser:
        return None  # SU видит всё

    if not user.companies.filter(schema_name=schema_name).exists():
        return set()  # нет доступа к тенанту вообще

    # 1) User.branch_access (JSON, public schema) — приоритет.
    access = getattr(user, 'branch_access', None) or {}
    if isinstance(access, dict):
        val = access.get(schema_name)
        if val == 'all':
            return None
        if isinstance(val, list):
            try:
                return {int(x) for x in val}
            except (ValueError, TypeError):
                return set()
        # val is None — переходим на fallback

    # 2) Fallback: StaffProfile.branch_access (M2M в tenant schema).
    # Так RBAC, выставленный владельцем через мобильный StaffScreen
    # («Сотрудники»), тоже применяется без дублирования настройки.
    try:
        from django_tenants.utils import schema_context
        with schema_context(schema_name):
            from apps.tenant.branch.models import StaffProfile
            sp = StaffProfile.objects.filter(user=user).first()
            if sp:
                ids = set(sp.branch_access.values_list('pk', flat=True))
                if ids:
                    return ids
    except Exception:
        pass

    # 3) Никаких ограничений
    return None


def filter_branches_qs(qs, user, schema_name: str):
    """
    Удобный helper для применения branch-фильтра к queryset Branch / любому
    с полем branch_id.

    Использование:
        qs = filter_branches_qs(Branch.objects.all(), request.user, schema)
        qs = filter_branches_qs(Review.objects.all(), request.user, schema)  # фильтр по branch_id
    """
    allowed = user_allowed_branches(user, schema_name)
    if allowed is None:
        return qs
    if not allowed:
        return qs.none()
    # Detect Branch vs other (с FK branch_id)
    model = qs.model
    if hasattr(model, 'branch_id') or any(f.name == 'branch' for f in model._meta.get_fields() if hasattr(f, 'name')):
        return qs.filter(branch_id__in=allowed)
    # Branch модель сама — фильтруем по pk
    return qs.filter(pk__in=allowed)


def current_schema_name() -> str:
    """
    Текущая schema из django_tenants connection. Если public — пустая строка.
    """
    from django.db import connection
    tenant = getattr(connection, 'tenant', None)
    if tenant is None:
        return ''
    return tenant.schema_name or ''
