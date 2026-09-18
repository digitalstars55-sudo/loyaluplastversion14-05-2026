"""
Обмен токена CheckUp → LoyalUP (контракт платформы, раздел 2.1).

Сотрудник залогинен в CheckUp. Когда он открывает раздел лояльности, BFF
CheckUp приходит сюда с секретом и говорит: «пользователь такой-то, сеть
такая-то, роль такая-то, вот его точки». Мы заводим (или находим) ему
User LoyalUP, синхронизируем права и отдаём JWT на час. Дальше CheckUp ходит
в обычные ручки LoyalUP с этим JWT, как мобилка.

Что здесь решено и почему:
  * роль приходит УЖЕ выведенной CheckUp из его кодов прав (loyalty.*):
    у CheckUp нет owner/admin/manager/staff, есть права per-клиент. Мы
    принимаем только 'network_admin' и 'client', остальное — 403;
  * точки приходят ПУБЛИЧНЫМИ branch_id (те же, что в QR и жалобах) — один
    идентификатор на всю границу; во внутренний Branch.id переводим сами,
    потому что User.branch_access хранит именно Branch.id;
  * одна личность на пару (пользователь CheckUp, сеть) — см. models.py;
  * email в User не пишем (сломал бы вход по email «родному» сотруднику);
  * выключенного оператором User обмен не оживляет (403 user_disabled).
"""
from __future__ import annotations

import datetime
import hmac
import logging
import re

import jwt
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django_tenants.utils import get_tenant_model, schema_context

from apps.shared.users.auth import JWT_ALGORITHM, JWT_ISSUER, _jwt_secret
from apps.shared.users.models import User

from .models import CheckUpIdentity

log = logging.getLogger(__name__)

ROLE_NETWORK_ADMIN = 'network_admin'
ROLE_CLIENT = 'client'
ALLOWED_ROLES = (ROLE_NETWORK_ADMIN, ROLE_CLIENT)

DEFAULT_TOKEN_MINUTES = 60
USERNAME_PREFIX = 'checkup-'
MAX_BRANCHES = 200
MAX_NAME = 150
MAX_EMAIL = 254

# schema_name у нас бывает и с дефисом (asap-arzamas), и с подчёркиванием (asap_orel).
_SCHEMA_RE = re.compile(r'^[a-z][a-z0-9_-]{0,62}$')
# Только то, что пройдёт валидатор username Django (буквы, цифры, . _ -).
_CUID_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')


class ExchangeError(Exception):
    """Ошибка обмена, которую можно сразу отдать наружу: статус + код + слова."""

    def __init__(self, status: int, code: str, detail: str = '', **extra):
        super().__init__(f'{status} {code}: {detail}')
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra

    def as_dict(self) -> dict:
        body = {'code': self.code, 'detail': self.detail}
        body.update(self.extra)
        return body


# ── настройки ────────────────────────────────────────────────────────────────

def exchange_secret() -> str:
    return str(getattr(settings, 'CHECKUP_TOKEN_EXCHANGE_SECRET', '') or '')


def exchange_enabled() -> bool:
    """Пустой секрет = ручка выключена, прод как раньше."""
    return bool(exchange_secret())


def secret_matches(provided: str) -> bool:
    expected = exchange_secret()
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(provided), expected)


def allowed_tenants() -> list[str]:
    """Белый список сетей для обмена; пусто = любая живая сеть. На проде сначала только dev."""
    raw = getattr(settings, 'CHECKUP_TOKEN_EXCHANGE_TENANTS', None) or []
    if isinstance(raw, str):
        raw = raw.split(',')
    return [s.strip().lower() for s in raw if s and s.strip()]


def platform_admins() -> set[str]:
    """checkup_user_id с платформенным доступом (CHECKUP_PLATFORM_ADMINS, через запятую)."""
    raw = getattr(settings, 'CHECKUP_PLATFORM_ADMINS', None) or ''
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(',')
    return {str(x).strip() for x in items if str(x).strip()}


def is_platform_admin(checkup_user_id) -> bool:
    return str(checkup_user_id).strip() in platform_admins()


def tenant_open(schema: str) -> bool:
    """Открыта ли сеть обычным пользователям CheckUp (список пуст = все)."""
    allowed = allowed_tenants()
    return not allowed or schema in allowed


def list_platform_tenants() -> list[dict]:
    """
    GET /api/v1/internal/tenants/ (контракт 3в.2): живые сети платформы для
    переключателя платформенного пользователя. Без секретов и настроек.
    """
    Tenant = get_tenant_model()
    allowed = allowed_tenants()
    out = []
    qs = (Tenant.objects.exclude(schema_name='public').filter(is_active=True)
          .prefetch_related('domains').order_by('name'))
    for t in qs:
        domains = list(t.domains.all())
        primary = next((d for d in domains if d.is_primary), None) or (domains[0] if domains else None)
        paid_until = getattr(t, 'paid_until', None)
        out.append({
            'schema': t.schema_name,
            'name': t.name,
            'client_id': getattr(t, 'client_id', None),
            'domain': primary.domain if primary else '',
            'is_active': bool(t.is_active),
            'paid_until': paid_until.isoformat() if paid_until else None,
            'exchange_open': not allowed or t.schema_name in allowed,
        })
    return out


def token_minutes() -> int:
    try:
        minutes = int(getattr(settings, 'CHECKUP_TOKEN_EXCHANGE_MINUTES', DEFAULT_TOKEN_MINUTES) or DEFAULT_TOKEN_MINUTES)
    except (TypeError, ValueError):
        minutes = DEFAULT_TOKEN_MINUTES
    return max(1, min(minutes, 24 * 60))


# ── валидация тела ───────────────────────────────────────────────────────────

def _as_int_list(value) -> list[int]:
    if not isinstance(value, list):
        raise ExchangeError(422, 'invalid_payload', 'branch_ids должен быть списком публичных branch_id')
    out: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise ExchangeError(422, 'invalid_payload', 'branch_ids: только положительные целые')
        if item not in out:
            out.append(item)
    if len(out) > MAX_BRANCHES:
        raise ExchangeError(422, 'invalid_payload', f'branch_ids: не больше {MAX_BRANCHES} точек')
    return out


def validate_payload(data) -> dict:
    """
    Тело запроса → чистый словарь. Любая беда — ExchangeError с 422
    (форма) или 403 (роль не из разрешённых).
    """
    if not isinstance(data, dict):
        raise ExchangeError(422, 'invalid_payload', 'тело должно быть JSON-объектом')

    cuid = data.get('checkup_user_id')
    if isinstance(cuid, bool) or cuid is None:
        raise ExchangeError(422, 'invalid_payload', 'checkup_user_id обязателен')
    if isinstance(cuid, int):
        cuid = str(cuid)
    if not isinstance(cuid, str) or not _CUID_RE.match(cuid):
        raise ExchangeError(422, 'invalid_payload', 'checkup_user_id: буквы, цифры, . _ -, до 64 символов')

    schema = str(data.get('tenant_schema') or '').strip().lower()
    if not _SCHEMA_RE.match(schema) or schema == 'public':
        raise ExchangeError(422, 'invalid_payload', 'tenant_schema: имя сети LoyalUP (schema_name)')

    role = str(data.get('role') or '').strip().lower()
    if role not in ALLOWED_ROLES:
        raise ExchangeError(403, 'role_not_allowed',
                            'разрешены только network_admin и client — роль выводит CheckUp из кодов loyalty.*')

    raw_branches = data.get('branch_ids')
    if role == ROLE_CLIENT:
        branch_ids = _as_int_list(raw_branches if raw_branches is not None else [])
        if not branch_ids:
            raise ExchangeError(422, 'invalid_payload', 'client без точек не бывает: передайте branch_ids')
    else:
        # Админ сети видит все точки; список от CheckUp тут не ограничивает, только логируется.
        branch_ids = []
        if isinstance(raw_branches, list) and raw_branches:
            log.info('exchange: network_admin пришёл со списком точек %s — игнорируем, даём все', raw_branches[:10])

    display_name = str(data.get('display_name') or data.get('full_name') or '').strip()[:MAX_NAME]
    email = str(data.get('email') or '').strip()[:MAX_EMAIL]

    return {
        'checkup_user_id': cuid,
        'tenant_schema': schema,
        'role': role,
        'branch_ids': branch_ids,
        'display_name': display_name,
        'email': email,
    }


# ── разрешение сети и точек ──────────────────────────────────────────────────

def resolve_tenant(schema: str, platform: bool = False):
    Tenant = get_tenant_model()
    tenant = Tenant.objects.filter(schema_name=schema).first()
    if tenant is None:
        raise ExchangeError(404, 'tenant_not_found', f'сети {schema} нет')
    if not tenant.is_active:
        raise ExchangeError(404, 'tenant_inactive', f'сеть {schema} выключена')
    # Платформенный пользователь (белый список) видит любую живую сеть — как
    # суперадминка LoyalUP; остальным — только открытые (контракт 3в.1).
    allowed = allowed_tenants()
    if not platform and allowed and schema not in allowed:
        raise ExchangeError(403, 'tenant_not_allowed', f'обмен для сети {schema} ещё не включён')
    return tenant


def resolve_branch_pks(schema: str, public_ids: list[int]) -> list[int]:
    """Публичные branch_id (из QR/жалоб) → внутренние Branch.id, которые хранит branch_access."""
    from apps.tenant.branch.models import Branch

    with schema_context(schema):
        found = dict(Branch.objects.filter(branch_id__in=public_ids).values_list('branch_id', 'id'))
    missing = [b for b in public_ids if b not in found]
    if missing:
        raise ExchangeError(422, 'unknown_branch', 'точек с такими branch_id в сети нет', unknown_branch_ids=missing)
    return [found[b] for b in public_ids]


def branches_payload(public_ids: list[int], branch_pks: list[int] | None) -> list[dict] | None:
    """Пары «публичный branch_id → внутренний id» для точек сотрудника.

    CheckUp знает точку только по публичному `branch_id` (он в `LoyalupBranchMap`,
    в QR и в жалобах), а фильтры ручек раздела 3 принимают внутренний `id`. Без
    этой пары BFF пришлось бы держать свой словарь и угадывать соответствие.

    Запроса в базу тут нет: `resolve_branch_pks` уже вернул внутренние id РОВНО
    в порядке присланных публичных, остаётся сложить их вместе.

    `None` (а не пустой список) — это `network_admin`: у него доступны ВСЕ точки
    сети, их список CheckUp берёт из `GET /api/v1/analytics/branches/`, где с
    18.09.2026 рядом с `id` лежит `branch_id`. Пустой список означал бы «точек
    нет», это другое.
    """
    if branch_pks is None:
        return None
    return [{'branch_id': public_id, 'id': pk} for public_id, pk in zip(public_ids, branch_pks)]


def username_for(checkup_user_id: str, schema: str) -> str:
    return f'{USERNAME_PREFIX}{checkup_user_id}-{schema}'[:150]


# ── токен ────────────────────────────────────────────────────────────────────

def issue_exchange_token(user, schema: str, platform: bool = False) -> tuple[str, datetime.datetime]:
    """
    Тот же формат, что access-токен мобилки (его принимает JWTAuthentication),
    но живёт token_minutes() и помечен via=checkup / tenant=<schema>.
    Refresh не выдаём: истёк — BFF делает новый обмен.
    """
    now = timezone.now()
    expires_at = now + datetime.timedelta(minutes=token_minutes())
    payload = {
        'sub': str(user.pk),
        'username': user.username,
        'role': getattr(user, 'role', ROLE_CLIENT),
        'iat': int(now.timestamp()),
        'exp': int(expires_at.timestamp()),
        'iss': JWT_ISSUER,
        'typ': 'access',
        'via': 'checkup',
        'tenant': schema,
    }
    if platform:
        payload['platform'] = True   # сводная по всем клиентам (контракт 3в.3)
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM), expires_at


# ── сам обмен ────────────────────────────────────────────────────────────────

def perform_exchange(data) -> dict:
    """
    Полный обмен: валидация → сеть → точки → личность → права → токен.
    Возвращает {'user','identity','tenant','token','expires_at','created'}.
    Всё, что не так, — ExchangeError со статусом для ответа.
    """
    p = validate_payload(data)
    platform = is_platform_admin(p['checkup_user_id'])
    tenant = resolve_tenant(p['tenant_schema'], platform=platform)
    schema = p['tenant_schema']
    branch_pks = resolve_branch_pks(schema, p['branch_ids']) if p['role'] == ROLE_CLIENT else None

    with transaction.atomic():
        identity = (CheckUpIdentity.objects.select_related('user')
                    .filter(checkup_user_id=p['checkup_user_id'], tenant_schema=schema).first())
        created = False
        if identity is None:
            username = username_for(p['checkup_user_id'], schema)
            if User.objects.filter(username=username).exists():
                raise ExchangeError(409, 'identity_conflict',
                                    f'пользователь {username} уже существует и не привязан к CheckUp')
            user = User(username=username, role=p['role'], is_active=True,
                        first_name=p['display_name'][:150])
            user.set_unusable_password()
            try:
                user.save()
                identity = CheckUpIdentity.objects.create(
                    checkup_user_id=p['checkup_user_id'], tenant_schema=schema, user=user,
                )
            except IntegrityError:
                raise ExchangeError(409, 'identity_conflict', 'личность заводится параллельным запросом, повторите')
            created = True
        else:
            user = identity.user
            if user.is_superuser:
                raise ExchangeError(409, 'identity_conflict', 'личность привязана к суперпользователю')
            if not user.is_active:
                raise ExchangeError(403, 'user_disabled', 'пользователь выключен оператором LoyalUP')

        # Права — ровно то, что сказал CheckUp в ЭТОМ обмене, только для этой сети.
        user.role = p['role']
        if p['display_name']:
            user.first_name = p['display_name'][:150]
        access = dict(user.branch_access or {})
        access[schema] = 'all' if p['role'] == ROLE_NETWORK_ADMIN else list(branch_pks or [])
        user.branch_access = access
        user.save(update_fields=['role', 'first_name', 'branch_access'])
        if not user.companies.filter(pk=tenant.pk).exists():
            user.companies.add(tenant)

        identity.display_name = p['display_name']
        identity.email = p['email']
        identity.last_role = p['role']
        identity.last_branch_ids = p['branch_ids']
        identity.exchanges_count = (identity.exchanges_count or 0) + 1
        identity.last_exchanged_at = timezone.now()
        identity.save()

    token, expires_at = issue_exchange_token(user, schema, platform=platform)
    log.info('exchange: %s checkup=%s tenant=%s role=%s branches=%s platform=%s',
             'создан' if created else 'обновлён', p['checkup_user_id'], schema, p['role'], p['branch_ids'], platform)
    return {'user': user, 'identity': identity, 'tenant': tenant,
            'token': token, 'expires_at': expires_at, 'created': created,
            'branches': branches_payload(p['branch_ids'], branch_pks),
            'platform': platform, 'tenant_open': tenant_open(schema)}
