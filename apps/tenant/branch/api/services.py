from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce

from apps.shared.guest.models import Client
from ..models import (
    Branch, ClientBranch, ClientBranchVisit, ClientVKStatus, CoinTransaction, Promotions,
    TestimonialConversation, TestimonialMessage,
)



# ── Push notifications: new-review hook ───────────────────────────────────────

def _safe_push_review_new(*, conv, source: str) -> None:
    """
    Best-effort push 'review_new' to tenant admins on the FIRST message of a
    conversation. Never raises — logs on failure, ingest is sacred.
    """
    import logging
    from django.db import connection

    log = logging.getLogger(__name__)
    try:
        from apps.tenant.analytics.auto_reply import push_review_new

        tenant = getattr(connection, "tenant", None)
        if not tenant:
            log.warning("_safe_push_review_new: no tenant on connection — skip conv=%s", conv.pk)
            return
        push_review_new(
            schema_name=tenant.schema_name,
            tenant_name=getattr(tenant, "name", tenant.schema_name),
            conversation_id=conv.pk,
            source=source,
        )
    except Exception:
        log.exception("_safe_push_review_new failed conv=%s source=%s", conv.pk, source)


# ── VK ID OAuth2 (веб-приложение) ─────────────────────────────────────────────

class VKAuthError(Exception):
    pass


def vk_oauth_exchange(
    code: str,
    device_id: str,
    code_verifier: str,
    redirect_uri: str,
    state: str,
) -> dict:
    """
    VK ID OAuth2 Authorization Code + PKCE — server-side code exchange.

    Flow (вызывается после того как фронт получил code от VK):
      1. POST https://id.vk.ru/oauth2/auth  → access_token + user_id
      2. POST https://id.vk.ru/oauth2/user_info → first_name, last_name, avatar

    Returns:
        {'user_id': int, 'first_name': str, 'last_name': str, 'photo_url': str}

    Raises:
        VKAuthError — любая ошибка VK API или сетевая ошибка.
    """
    import json
    import urllib.parse
    import urllib.request

    app_id = getattr(settings, 'VK_WEB_APP_ID', None)
    if not app_id:
        raise VKAuthError('VK_WEB_APP_ID не настроен')

    # ── Шаг 1: обмен кода на токен ───────────────────────────────────────────
    token_body = urllib.parse.urlencode({
        'grant_type':    'authorization_code',
        'code':          code,
        'device_id':     device_id,
        'code_verifier': code_verifier,
        'redirect_uri':  redirect_uri,
        'client_id':     app_id,
        'state':         state,
    }).encode()

    try:
        req = urllib.request.Request(
            'https://id.vk.ru/oauth2/auth',
            data=token_body,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            token_data = json.loads(resp.read())
    except Exception as e:
        raise VKAuthError(f'Ошибка обмена кода VK: {e}')

    if 'error' in token_data:
        raise VKAuthError(
            token_data.get('error_description') or token_data['error']
        )

    access_token = token_data.get('access_token')
    user_id      = token_data.get('user_id')
    if not access_token or not user_id:
        raise VKAuthError('VK ID не вернул access_token или user_id')

    # ── Шаг 2: получение профиля ─────────────────────────────────────────────
    info_body = urllib.parse.urlencode({
        'access_token': access_token,
        'client_id':    app_id,
    }).encode()

    try:
        req = urllib.request.Request(
            'https://id.vk.ru/oauth2/user_info',
            data=info_body,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            info_data = json.loads(resp.read())
    except Exception as e:
        raise VKAuthError(f'Ошибка получения профиля VK: {e}')

    user = info_data.get('user', {})

    import logging
    logger = logging.getLogger(__name__)

    birthday_from_vkid = user.get('birthday') or user.get('bdate') or None

    logger.info(
        'VK OAuth user_info: user_id=%s, birthday=%s, bdate=%s, keys=%s',
        user_id,
        user.get('birthday'),
        user.get('bdate'),
        list(user.keys()),
    )

    # ── Шаг 3: fallback — legacy VK API для получения bdate ────────────────
    # VK ID /oauth2/user_info НЕ отдаёт birthday для частичных дат
    # (когда пользователь скрыл год, но оставил день+месяц).
    # Legacy VK API users.get отдаёт bdate в формате "DD.MM" даже без года.
    # Вызываем ТОЛЬКО если user_info не вернул birthday.
    birthday_from_vk_api = None
    if not birthday_from_vkid:
        try:
            vk_api_url = (
                f'https://api.vk.com/method/users.get'
                f'?user_ids={user_id}'
                f'&fields=bdate'
                f'&access_token={access_token}'
                f'&v=5.199'
            )
            req = urllib.request.Request(vk_api_url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                vk_api_data = json.loads(resp.read())

            vk_api_users = vk_api_data.get('response', [])
            if vk_api_users:
                birthday_from_vk_api = vk_api_users[0].get('bdate') or None
                logger.info(
                    'VK API users.get fallback: user_id=%s, bdate=%s',
                    user_id,
                    birthday_from_vk_api,
                )
        except Exception as e:
            # Не фатально — если VK API недоступен, просто не получим bdate
            logger.warning(
                'VK API users.get fallback failed for user_id=%s: %s',
                user_id, e,
            )

    final_birthday = birthday_from_vkid or birthday_from_vk_api

    logger.info(
        'VK OAuth final birthday: user_id=%s, from_vkid=%s, from_vk_api=%s, final=%s',
        user_id, birthday_from_vkid, birthday_from_vk_api, final_birthday,
    )

    return {
        'user_id':    int(user_id),
        'first_name': user.get('first_name', ''),
        'last_name':  user.get('last_name', ''),
        'photo_url':  user.get('avatar', ''),
        'birthday':   final_birthday,
    }


def parse_vk_bdate(bdate: str):
    """
    Парсит дату рождения из ВК в формат date.

    Входные форматы:
    - "15.3"       → date(1900, 3, 15)   (ВК, год скрыт пользователем)
    - "15.3.1990"  → date(1990, 3, 15)   (ВК, год открыт)
    - "1990-03-15" → date(1990, 3, 15)   (VK ID /oauth2/user_info, ISO)

    Returns date or None on parse error.
    """
    import datetime

    if not bdate or not isinstance(bdate, str):
        return None

    bdate = bdate.strip()

    # ISO формат: "YYYY-MM-DD" (VK ID endpoint)
    if '-' in bdate:
        try:
            return datetime.date.fromisoformat(bdate)
        except (ValueError, TypeError):
            return None

    # ВК формат: "DD.MM" или "DD.MM.YYYY"
    parts = bdate.split('.')
    if len(parts) < 2:
        return None

    try:
        day = int(parts[0])
        month = int(parts[1])
        year = int(parts[2]) if len(parts) >= 3 and len(parts[2]) == 4 else 1900
        return datetime.date(year, month, day)
    except (ValueError, TypeError, IndexError):
        return None


def vk_web_auth(
    code: str,
    device_id: str,
    code_verifier: str,
    redirect_uri: str,
    state: str,
    branch_id: int,
    birth_date=None,
) -> tuple:
    """
    Full VK ID OAuth2 PKCE auth + registration in one atomic call.

    Combines vk_oauth_exchange + register_or_get_client.
    Auto-saves birth_date from VK profile if the client doesn't have one.

    Returns:
        (ClientBranch, created: bool)
        NOTE: returned ClientBranch has extra attribute `_vk_bdate` with raw VK birthday string.

    Raises:
        VKAuthError     — VK API or network error
        BranchNotFound  — branch_id not found
        BranchInactive  — branch is disabled
        ClientBlocked   — client.is_active=False
    """
    vk_user = vk_oauth_exchange(
        code=code,
        device_id=device_id,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        state=state,
    )

    # Если фронтенд не передал birth_date, но ВК отдаёт birthday — парсим и сохраняем.
    # Это решает проблему: у гостя в ВК стоит дата (день+месяц без года),
    # но фронтенд не может её получить через OAuth, а модалка «Укажи ДР» лишняя.
    vk_bdate = vk_user.get('birthday')

    # ── Fallback #2: community token ──────────────────────────────────────
    # Если ни user_info, ни users.get с user-токеном не вернули bdate,
    # пробуем через community-токен (из SenlerConfig), который имеет
    # право читать публичные поля профиля даже без user-scope.
    if not vk_bdate:
        vk_bdate = _fetch_bdate_via_community_token(
            vk_id=vk_user['user_id'],
            branch_id=branch_id,
        )

    effective_birth_date = birth_date
    if not effective_birth_date and vk_bdate:
        effective_birth_date = parse_vk_bdate(vk_bdate)

    profile, created = register_or_get_client(
        vk_id=vk_user['user_id'],
        branch_id=branch_id,
        first_name=vk_user['first_name'],
        last_name=vk_user['last_name'],
        photo_url=vk_user['photo_url'],
        birth_date=effective_birth_date,
    )

    # Если клиент уже существовал (created=False) и birth_date ещё не заполнен —
    # дозаписываем из ВК при каждом логине (на случай если раньше дата была скрыта,
    # а теперь пользователь её открыл).
    if not created and not profile.birth_date and effective_birth_date:
        profile.birth_date = effective_birth_date
        profile.birth_date_set_at = timezone.localdate()
        profile.save(update_fields=['birth_date', 'birth_date_set_at'])
        # Re-fetch to get fresh annotated queryset
        profile = _profile_qs().get(pk=profile.pk)

    # Сохраняем сырую дату ВК как атрибут — для передачи фронтенду через serializer
    profile._vk_bdate = vk_bdate
    return profile, created


def handle_vk_callback(data: dict) -> None:
    """
    Processes a VK Callback API event.

    Raises:
        VKCallbackConfirmation(code) — confirmation handshake; view returns the code as plain text
        VKCallbackForbidden          — secret mismatch; view returns 403

    Returns normally for all handled events (view returns 'ok').
    """
    from apps.tenant.senler.models import SenlerConfig

    event    = data.get('type')
    group_id = data.get('group_id')
    secret   = data.get('secret', '')

    if not group_id:
        return

    if event == 'confirmation':
        config = SenlerConfig.objects.filter(
            vk_group_id=group_id, vk_callback_confirmation__gt=''
        ).first()
        code = config.vk_callback_confirmation if config else 'ok'
        raise VKCallbackConfirmation(code)

    config = SenlerConfig.objects.filter(vk_group_id=group_id).first()
    if not config:
        return

    if config.vk_callback_secret and secret != config.vk_callback_secret:
        raise VKCallbackForbidden

    if event == 'message_new':
        msg_obj    = data.get('object', {})
        message    = msg_obj.get('message', msg_obj)
        from_id    = message.get('from_id')
        message_id = message.get('id')
        text       = (message.get('text') or '').strip()
        if from_id and from_id > 0 and message_id and text:
            handle_vk_incoming_message(
                group_id=group_id,
                from_id=from_id,
                message_id=message_id,
                text=text,
            )

    elif event in ('group_join', 'group_leave', 'message_allow', 'message_deny'):
        obj        = data.get('object', {})
        vk_user_id = obj.get('user_id')
        if vk_user_id and vk_user_id > 0:
            apply_vk_membership_event(
                group_id=group_id,
                vk_user_id=vk_user_id,
                event_type=event,
            )


# ── VK Callback exceptions ────────────────────────────────────────────────────

class VKCallbackConfirmation(Exception):
    """Raised when VK sends a confirmation handshake — view returns the code as plain text."""
    def __init__(self, code: str):
        self.code = code


class VKCallbackForbidden(Exception):
    """Raised when the callback secret doesn't match — view returns 403."""


# ── Exceptions ────────────────────────────────────────────────────────────────

class BranchNotFound(Exception):
    pass


class BranchInactive(Exception):
    pass


class ClientNotFound(Exception):
    pass


class ClientBlocked(Exception):
    pass


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fetch_bdate_via_community_token(vk_id: int, branch_id: int) -> str | None:
    """
    Пробует получить bdate через community-токен (из SenlerConfig для branch).

    VK API users.get с community-токеном возвращает bdate в формате:
    - "15.3"       — день+месяц, год скрыт
    - "15.3.1990"  — полная дата
    - отсутствует  — полностью скрыто

    Returns raw bdate string or None.
    """
    import json
    import logging
    import urllib.parse
    import urllib.request

    from apps.tenant.senler.models import SenlerConfig

    logger = logging.getLogger(__name__)

    try:
        config = SenlerConfig.objects.filter(
            branch__branch_id=branch_id,
        ).first()
        if not config or not config.vk_community_token:
            logger.info(
                'No community token for branch_id=%s — cannot fetch bdate fallback',
                branch_id,
            )
            return None

        url = (
            f'https://api.vk.com/method/users.get'
            f'?user_ids={vk_id}'
            f'&fields=bdate'
            f'&access_token={config.vk_community_token}'
            f'&v=5.131'
        )
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        if 'error' in data:
            logger.warning(
                'VK API users.get (community token) error for vk_id=%s: %s',
                vk_id, data['error'].get('error_msg', ''),
            )
            return None

        users = data.get('response', [])
        if users:
            bdate = users[0].get('bdate') or None
            logger.info(
                'VK API users.get (community token): vk_id=%s, bdate=%s',
                vk_id, bdate,
            )
            return bdate

    except Exception as e:
        logger.warning(
            'Community token bdate fallback failed for vk_id=%s, branch_id=%s: %s',
            vk_id, branch_id, e,
        )

    return None


def _profile_qs():
    """ClientBranch queryset with pre-loaded relations and balance annotation."""
    return (
        ClientBranch.objects
        .select_related('client', 'vk_status')
        .annotate(
            _coins_balance=(
                Coalesce(Sum('transactions__amount', filter=Q(transactions__type='income')), 0)
                - Coalesce(Sum('transactions__amount', filter=Q(transactions__type='expense')), 0)
            )
        )
    )


def _image_url(field) -> str | None:
    """Returns relative image URL or None if no file is associated."""
    return field.url if (field and field.name) else None


# ── Public service functions ──────────────────────────────────────────────────

def get_branch_info(branch_id: int, *, tenant=None) -> dict:
    """
    Returns branch data merged with tenant config for the given branch_id.

    branch_id: the QR-code integer ID (Branch.branch_id field), not DB PK.
    tenant:    django-tenants Company instance (from request.tenant).

    Raises:
        BranchNotFound  — branch_id doesn't exist
        BranchInactive  — branch is disabled
    """
    try:
        branch = Branch.objects.select_related('config').get(branch_id=branch_id)
    except Branch.DoesNotExist:
        raise BranchNotFound

    if not branch.is_active:
        raise BranchInactive

    branch_config = getattr(branch, 'config', None)

    config = None
    if tenant is not None:
        from apps.shared.config.models import ClientConfig
        try:
            config = ClientConfig.objects.get(company=tenant)
        except ClientConfig.DoesNotExist:
            pass

    # Резолв кастомных сообщений: branch override → tenant default → hardcoded fallback
    _CODE_PROMPT_FALLBACK = 'ЧТОБЫ ЗАБРАТЬ МОНЕТЫ, ПОПРОСИТЕ КОД ДНЯ У СОТРУДНИКА'
    _QUEST_SHOW_FALLBACK  = 'У ВАС ЕСТЬ 30 МИНУТ, ЧТОБЫ ВЫПОЛНИТЬ ЗАДАНИЕ И ПОКАЗАТЬ РЕЗУЛЬТАТ СОТРУДНИКУ.'
    code_prompt_message = (
        (branch_config.code_prompt_message.strip() if branch_config and branch_config.code_prompt_message else '')
        or (config.code_prompt_message.strip() if config and config.code_prompt_message else '')
        or _CODE_PROMPT_FALLBACK
    )
    quest_show_message = (
        (branch_config.quest_show_message.strip() if branch_config and branch_config.quest_show_message else '')
        or (config.quest_show_message.strip() if config and config.quest_show_message else '')
        or _QUEST_SHOW_FALLBACK
    )

    return {
        'id':                  branch.pk,
        'branch_id':           branch.branch_id,
        'name':                branch.name,
        'address':             branch_config.address    if branch_config else '',
        'phone':               branch_config.phone      if branch_config else '',
        'yandex_map':          branch_config.yandex_map if branch_config else '',
        'gis_map':             branch_config.gis_map    if branch_config else '',
        'logotype_url':        _image_url(config.logotype_image) if config else None,
        'coin_icon_url':       _image_url(config.coin_image)     if config else None,
        'vk_group_id':         config.vk_group_id   if config else None,
        'vk_group_name':       config.vk_group_name if config else None,
        'story_image_url':     _image_url(branch.story_image),
        'code_prompt_message': code_prompt_message,
        'quest_show_message':  quest_show_message,
    }


def _resync_vk_status_cached(profile: ClientBranch) -> None:
    """
    Перепроверяет community + newsletter через VK API при каждом init.
    Кеш в Redis 60 секунд: не больше 1 VK-вызова на гостя за минуту.

    Используется в get_client_profile, чтобы поймать гостей, отписавшихся
    от рассылки/сообщества вне приложения. VK Bridge silent-check есть
    только для community; для newsletter единственный путь — server-side.
    """
    import logging
    from django.core.cache import cache
    from apps.tenant.senler.models import SenlerConfig

    logger = logging.getLogger(__name__)

    vk_id = profile.client.vk_id
    cache_key = f'vk_sub_resync:{profile.branch_id}:{vk_id}'
    if cache.get(cache_key):
        return
    cache.set(cache_key, 1, 60)

    config = (
        SenlerConfig.objects.filter(branch=profile.branch).first()
        or SenlerConfig.objects.filter(is_active=True).order_by('branch_id').first()
    )
    if not config or not config.vk_community_token or not config.vk_group_id:
        return

    import json
    import urllib.parse
    import urllib.request

    token = config.vk_community_token
    group_id = config.vk_group_id

    def _vk_call(method, **params):
        params['access_token'] = token
        params['v'] = '5.131'
        url = f'https://api.vk.com/method/{method}?' + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        if 'error' in data:
            raise RuntimeError(data['error'].get('error_msg', ''))
        return data.get('response', {})

    is_member = None
    is_subscriber = None
    try:
        resp = _vk_call('groups.isMember', group_id=group_id, user_id=vk_id)
        is_member = bool(resp) if isinstance(resp, int) else bool(resp.get('member', 0))
    except Exception as e:
        logger.info('resync groups.isMember vk_id=%s: %s', vk_id, e)

    try:
        resp = _vk_call('messages.isMessagesFromGroupAllowed', group_id=group_id, user_id=vk_id)
        is_subscriber = bool(resp.get('is_allowed', 0))
    except Exception as e:
        logger.info('resync isMessagesFromGroupAllowed vk_id=%s: %s', vk_id, e)

    if is_member is None and is_subscriber is None:
        return  # обе ошибки — не трогаем БД
    # Если одна из проверок упала, не перезаписываем второе поле — берём текущее
    vk_status = getattr(profile, 'vk_status', None)
    cur_member     = (vk_status.is_community_member      if vk_status else False) if is_member is None else is_member
    cur_subscriber = (vk_status.is_newsletter_subscriber if vk_status else False) if is_subscriber is None else is_subscriber
    ClientVKStatus.sync(profile, is_member=cur_member, is_subscriber=cur_subscriber)


def get_client_profile(vk_id: int, branch_id: int) -> ClientBranch:
    """
    Returns ClientBranch for the given (vk_id, branch_id) pair.

    На каждом init дополнительно re-syncs community + newsletter через VK API
    (с Redis-кешем 60 сек): ловим гостей, отписавшихся от рассылки/сообщества
    вне приложения.

    Raises:
        ClientNotFound — no profile exists for this combination
    """
    try:
        profile = _profile_qs().get(client__vk_id=vk_id, branch__branch_id=branch_id)
    except ClientBranch.DoesNotExist:
        raise ClientNotFound

    try:
        _resync_vk_status_cached(profile)
        # Возвращаем профиль ещё раз чтобы подхватить обновлённый vk_status
        profile = _profile_qs().get(pk=profile.pk)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('resync VK status failed for vk_id=%s', vk_id)

    return profile


@transaction.atomic
def register_or_get_client(
    vk_id: int,
    branch_id: int,
    *,
    first_name: str = '',
    last_name: str = '',
    photo_url: str = '',
    birth_date=None,
    source: str = 'restaurant',
    invited_by_cb_id: int | None = None,
) -> tuple[ClientBranch, bool]:
    """
    Atomically finds or creates a ClientBranch for the given guest.

    Flow:
      1. Validate Branch (exists, active)
      2. Get or create guest.Client by vk_id; sync mutable fields if changed
      3. Get or create ClientBranch (guest × branch)
      4. Record QR-scan visit (6-hour cooldown, atomic) — skipped for delivery
      5. On first registration: set invited_by if invited_by_vk_id provided

    Returns:
        (ClientBranch, created: bool)

    Raises:
        BranchNotFound  — branch_id not found
        BranchInactive  — branch is disabled
        ClientBlocked   — client.is_active=False
    """
    try:
        branch = Branch.objects.get(branch_id=branch_id)
    except Branch.DoesNotExist:
        raise BranchNotFound

    if not branch.is_active:
        raise BranchInactive

    client, client_created = Client.objects.get_or_create(
        vk_id=vk_id,
        defaults={
            'first_name': first_name,
            'last_name':  last_name,
            'photo_url':  photo_url,
        },
    )

    if not client_created:
        if not client.is_active:
            raise ClientBlocked

        # VK profile fields (name, photo) can change between sessions
        updates = {}
        if first_name and client.first_name != first_name:
            updates['first_name'] = first_name
        if last_name and client.last_name != last_name:
            updates['last_name'] = last_name
        if photo_url and client.photo_url != photo_url:
            updates['photo_url'] = photo_url
        if updates:
            for attr, val in updates.items():
                setattr(client, attr, val)
            client.save(update_fields=list(updates))

    profile, created = ClientBranch.objects.get_or_create(
        client=client,
        branch=branch,
        defaults={
            'birth_date': birth_date,
            'birth_date_set_at': timezone.localdate() if birth_date else None,
        },
    )

    # Для СУЩЕСТВУЮЩИХ клиентов: если birth_date ещё не заполнен,
    # но фронтенд передал его (из VK Bridge или VK OAuth) — дозаписываем.
    # Это покрывает случай: клиент создан раньше (когда дата была скрыта),
    # а теперь ВК отдаёт bdate.
    if not created and not profile.birth_date and birth_date:
        profile.birth_date = birth_date
        profile.birth_date_set_at = timezone.localdate()
        profile.save(update_fields=['birth_date', 'birth_date_set_at'])

    # Record visit: atomic, 6-hour cooldown (delivery tracks via Delivery model)
    if source != 'delivery':
        ClientBranchVisit.record_visit(profile)

    # ── Link referrer from VK story (first-time only, even for existing profiles) ──
    if invited_by_cb_id and not profile.invited_by and invited_by_cb_id != profile.pk:
        try:
            inviter = ClientBranch.objects.get(pk=invited_by_cb_id)
            profile.invited_by = inviter
            profile.save(update_fields=['invited_by'])
        except ClientBranch.DoesNotExist:
            pass

    # ── Sync VK membership status on first registration ──────────────────
    if created:
        _sync_vk_status_on_register(profile)

    # Re-fetch with all relations and fresh balance annotation
    return _profile_qs().get(pk=profile.pk), created


def _sync_vk_status_on_register(profile: ClientBranch) -> None:
    """
    При первой регистрации проверяет через VK API:
      - groups.isMember              → is_community_member
      - messages.isMessagesFromGroupAllowed → is_newsletter_subscriber

    Если SenlerConfig или токен не настроен — молча пропускает.
    """
    import json
    import logging
    import urllib.error
    import urllib.parse
    import urllib.request

    from apps.tenant.senler.models import SenlerConfig

    logger = logging.getLogger(__name__)

    try:
        config = SenlerConfig.objects.get(branch=profile.branch)
    except SenlerConfig.DoesNotExist:
        return

    token = config.vk_community_token
    group_id = config.vk_group_id
    if not token or not group_id:
        return

    vk_id = profile.client.vk_id

    def _vk_call(method, **params):
        params['access_token'] = token
        params['v'] = '5.131'
        url = f'https://api.vk.com/method/{method}?' + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        if 'error' in data:
            raise RuntimeError(data['error'].get('error_msg', ''))
        return data.get('response', {})

    is_member = False
    is_subscriber = False

    try:
        resp = _vk_call('groups.isMember', group_id=group_id, user_id=vk_id)
        is_member = bool(resp) if isinstance(resp, int) else bool(resp.get('member', 0))
    except Exception as e:
        logger.warning('VK sync on register groups.isMember vk_id=%s: %s', vk_id, e)

    try:
        resp = _vk_call('messages.isMessagesFromGroupAllowed', group_id=group_id, user_id=vk_id)
        is_subscriber = bool(resp.get('is_allowed', 0))
    except Exception as e:
        logger.warning('VK sync on register isMessagesFromGroupAllowed vk_id=%s: %s', vk_id, e)

    ClientVKStatus.sync(profile, is_member=is_member, is_subscriber=is_subscriber)


def sync_vk_status_now(vk_id: int, branch_id: int) -> ClientBranch:
    """
    Немедленно синхронизирует VK-статус гостя через прямой вызов VK API.

    Используется в веб-версии после того как пользователь прошёл flow подписки —
    не ждёт Callback от ВК, а сразу идёт в API и записывает актуальный статус.

    Raises: ClientNotFound — профиль не найден для данной пары (vk_id, branch_id).
    """
    profile = get_client_profile(vk_id=vk_id, branch_id=branch_id)
    _sync_vk_status_on_register(profile)
    return _profile_qs().get(pk=profile.pk)


@transaction.atomic
def update_client_profile(
    vk_id: int,
    branch_id: int,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    photo_url: str | None = None,
    birth_date=None,
    community_via_app: bool | None = None,
    newsletter_via_app: bool | None = None,
    is_community_member: bool | None = None,
    is_newsletter_subscriber: bool | None = None,
) -> ClientBranch:
    """
    Partially updates a client profile. Only non-None fields are written.

      first_name / last_name / photo_url → guest.Client
      birth_date                         → ClientBranch
      is_community_member=True/False     → ClientVKStatus.sync() — факт подписки из VK Bridge,
                                           без атрибуции (pre-existing → via_app=False)
      is_newsletter_subscriber=True/False→ то же для рассылки
      community_via_app=True             → ClientVKStatus.mark_subscribed(community=True)
      newsletter_via_app=True            → ClientVKStatus.mark_subscribed(newsletter=True)

    Raises:
        ClientNotFound — no profile for (vk_id, branch_id)
    """
    profile = get_client_profile(vk_id=vk_id, branch_id=branch_id)

    cb_updates = []
    if birth_date is not None:
        profile.birth_date = birth_date
        cb_updates.append('birth_date')
        if profile.birth_date_set_at is None:
            profile.birth_date_set_at = timezone.localdate()
            cb_updates.append('birth_date_set_at')
    if cb_updates:
        profile.save(update_fields=cb_updates)

    client = profile.client
    cl_updates = []
    if first_name is not None:
        client.first_name = first_name
        cl_updates.append('first_name')
    if last_name is not None:
        client.last_name = last_name
        cl_updates.append('last_name')
    if photo_url is not None:
        client.photo_url = photo_url
        cl_updates.append('photo_url')
    if cl_updates:
        client.save(update_fields=cl_updates)

    # Sync membership status from VK Bridge (app init) — no attribution change
    if is_community_member is not None or is_newsletter_subscriber is not None:
        current = getattr(profile, 'vk_status', None)
        ClientVKStatus.sync(
            profile,
            is_member=(
                is_community_member if is_community_member is not None
                else (current.is_community_member if current else False)
            ),
            is_subscriber=(
                is_newsletter_subscriber if is_newsletter_subscriber is not None
                else (current.is_newsletter_subscriber if current else False)
            ),
        )

    # Attribution: user explicitly joined via app
    if community_via_app or newsletter_via_app:
        vk_status, _ = ClientVKStatus.objects.get_or_create(client=profile)
        vk_status.mark_subscribed(
            community=bool(community_via_app),
            newsletter=bool(newsletter_via_app),
        )

    return _profile_qs().get(pk=profile.pk)


def _get_active_branch(branch_id: int) -> Branch:
    try:
        branch = Branch.objects.get(branch_id=branch_id)
    except Branch.DoesNotExist:
        raise BranchNotFound
    if not branch.is_active:
        raise BranchInactive
    return branch


def get_employees(branch_id: int):
    """Returns ClientBranch queryset filtered to employees for the given branch."""
    branch = _get_active_branch(branch_id)
    return _profile_qs().filter(branch=branch, is_employee=True)


def get_promotions(branch_id: int):
    """Returns Promotions queryset for the given branch."""
    branch = _get_active_branch(branch_id)
    return Promotions.objects.filter(branch=branch)


def get_transactions(vk_id: int, branch_id: int):
    """Returns CoinTransaction queryset for the given (vk_id, branch_id) profile."""
    profile = get_client_profile(vk_id=vk_id, branch_id=branch_id)
    return CoinTransaction.objects.filter(client=profile)


@transaction.atomic
def upload_story(vk_id: int, branch_id: int) -> tuple[ClientVKStatus, bool]:
    """
    Marks that the guest published a VK story via the mini-app.

    Returns:
        (ClientVKStatus, uploaded: bool)
        uploaded=True  — first upload, status updated.
        uploaded=False — already uploaded before (idempotent).

    Raises:
        ClientNotFound — no profile for (vk_id, branch_id)
    """
    profile = get_client_profile(vk_id=vk_id, branch_id=branch_id)
    vk_status, _ = ClientVKStatus.objects.get_or_create(client=profile)
    uploaded = vk_status.mark_story_uploaded()
    return vk_status, uploaded


# ── Testimonials ──────────────────────────────────────────────────────────────

def _get_or_create_conversation(
    branch: Branch | None,
    vk_sender_id: str,
    link_vk_guest: bool = False,
) -> TestimonialConversation:
    """
    Возвращает существующий тред для этого отправителя или создаёт новый.

    link_vk_guest=False (APP): branch обязателен, привязывает ClientBranch.
    link_vk_guest=True (VK_MESSAGE): branch=None, привязывает shared Client.
    """
    conv, _ = TestimonialConversation.objects.get_or_create(
        branch=branch,
        vk_sender_id=vk_sender_id,
        defaults={'has_unread': True},
    )

    if link_vk_guest:
        # Линкуем к shared Client (vk_id, first_name, last_name, photo_url).
        # Проверяем при каждом вызове: гость мог зарегистрироваться позже.
        if not conv.vk_guest_id:
            try:
                from apps.shared.guest.models import Client as GuestClient
                guest = GuestClient.objects.filter(vk_id=int(vk_sender_id)).first()
            except (ValueError, TypeError):
                guest = None
            if guest:
                conv.vk_guest = guest
                conv.save(update_fields=['vk_guest'])
    else:
        # Линкуем к ClientBranch (гость зарегистрирован в этой точке).
        if not conv.client_id:
            try:
                cb = ClientBranch.objects.filter(
                    branch=branch,
                    client__vk_id=int(vk_sender_id),
                ).first()
            except (ValueError, TypeError):
                cb = None
            if cb:
                conv.client = cb
                conv.save(update_fields=['client'])

    return conv


def submit_app_review(
    vk_id: int,
    branch_id: int,
    review: str,
    rating: int | None = None,
    phone: str = '',
    table: int | None = None,
) -> TestimonialMessage:
    """
    Создаёт сообщение типа APP в существующем или новом треде.
    Вызывается из POST /api/v1/testimonials/.
    """
    from django.utils import timezone

    branch = Branch.objects.filter(branch_id=branch_id, is_active=True).first()
    if not branch:
        raise BranchNotFound(f'branch_id={branch_id}')

    conv = _get_or_create_conversation(branch, str(vk_id))

    msg = TestimonialMessage.objects.create(
        conversation=conv,
        source=TestimonialMessage.Source.APP,
        text=review,
        rating=rating,
        phone=phone or '',
        table_number=table,
    )

    conv.has_unread = True
    conv.is_replied = False
    conv.last_message_at = timezone.now()
    conv.save(update_fields=['has_unread', 'is_replied', 'last_message_at'])

    from apps.tenant.analytics.ai_service import analyze_and_save
    analyze_and_save(conv.id, review, TestimonialMessage.Source.APP)

    # Push only on FIRST message in the thread (= new review).
    if TestimonialMessage.objects.filter(conversation=conv).count() == 1:
        _safe_push_review_new(conv=conv, source='APP')

    return msg


def handle_vk_incoming_message(
    group_id: int,
    from_id: int,
    message_id: int,
    text: str,
) -> list[TestimonialMessage]:
    """
    Создаёт сообщение типа VK_MESSAGE в одном треде, привязанном к группе.
    Если у группы несколько точек — выбирает ту, где у отправителя уже есть тред,
    иначе первую.  Это предотвращает дублирование отзывов по точкам.
    Возвращает пустой список если сообщение уже было обработано или конфиг не найден.
    """
    from django.utils import timezone
    from apps.tenant.senler.models import SenlerConfig

    configs = list(
        SenlerConfig.objects.select_related('branch').filter(vk_group_id=group_id)
    )
    if not configs:
        return []

    vk_sender_id = str(from_id)
    vk_msg_id_str = str(message_id)

    # Global dedup: skip if this VK message was already processed
    if TestimonialMessage.objects.filter(vk_message_id=vk_msg_id_str).exists():
        return []

    conv = _get_or_create_conversation(None, vk_sender_id, link_vk_guest=True)

    # Dedup: if same text was already saved as an APP message in the last 5 min, skip.
    # This handles VK Callback retries echoing messages submitted via the app form.
    from datetime import timedelta
    recent_cutoff = timezone.now() - timedelta(minutes=5)
    if TestimonialMessage.objects.filter(
        conversation=conv,
        source=TestimonialMessage.Source.APP,
        text=text,
        created_at__gte=recent_cutoff,
    ).exists():
        return []

    msg = TestimonialMessage.objects.create(
        conversation=conv,
        source=TestimonialMessage.Source.VK_MESSAGE,
        text=text,
        vk_message_id=vk_msg_id_str,
    )

    conv.has_unread = True
    conv.is_replied = False
    conv.last_message_at = timezone.now()
    conv.save(update_fields=['has_unread', 'is_replied', 'last_message_at'])

    from apps.tenant.analytics.ai_service import analyze_and_save
    analyze_and_save(conv.id, text, TestimonialMessage.Source.VK_MESSAGE)

    # Push only on FIRST message in the thread (= new VK-originated review).
    if TestimonialMessage.objects.filter(conversation=conv).count() == 1:
        _safe_push_review_new(conv=conv, source='VK_MESSAGE')

    return [msg]


def handle_vk_admin_reply_from_poll(
    group_id: int,
    peer_id: int,
    message_id: int,
    text: str,
) -> TestimonialMessage | None:
    """
    Сохраняет ИСХОДЯЩЕЕ сообщение от сообщества (менеджер ответил гостю
    напрямую в ВК, минуя нашу админку). Помечает conv как `is_replied=True`,
    чтобы управляющий не отвечал повторно.

    peer_id    — vk_id гостя (получатель ответа).
    message_id — id исходящего сообщения VK (для глобального дедупа).

    Возвращает None если SenlerConfig не найден, сообщение уже сохранено
    (в т.ч. нашим send_vk_reply через нашу админку), или conv ещё не создан
    (нет входящих от этого гостя — отвечать не на что).
    """
    from django.utils import timezone
    from apps.tenant.senler.models import SenlerConfig

    if not SenlerConfig.objects.filter(vk_group_id=group_id).exists():
        return None

    vk_sender_id = str(peer_id)
    vk_msg_id_str = str(message_id)

    # Глобальный дедуп: если такое сообщение уже сохранено — пропускаем.
    # Покрывает оба случая:
    #   1) Наш send_vk_reply() уже сохранил это сообщение со вписанным
    #      vk_message_id (= ответ из админки).
    #   2) Прошлый poll уже подобрал это сообщение.
    if TestimonialMessage.objects.filter(vk_message_id=vk_msg_id_str).exists():
        return None

    # Conv создаётся в handle_vk_incoming_message при первом вхождении гостя.
    # Если conv нет — значит гость нам ничего не писал, и ответ менеджера
    # вне контекста треда — пропускаем (не создаём conv «из ниоткуда»).
    # У старых гостей может быть несколько conv (legacy с branch=X +
    # новый с branch=None из Fix A 7c3e89e) — берём самый свежий.
    conv = (
        TestimonialConversation.objects
        .filter(vk_sender_id=vk_sender_id)
        .order_by('-last_message_at', '-created_at')
        .first()
    )
    if conv is None:
        return None

    msg = TestimonialMessage.objects.create(
        conversation=conv,
        source=TestimonialMessage.Source.ADMIN_REPLY,
        text=text,
        vk_message_id=vk_msg_id_str,
    )

    conv.is_replied = True
    conv.has_unread = False
    conv.last_message_at = timezone.now()
    conv.save(update_fields=['is_replied', 'has_unread', 'last_message_at'])

    return msg


def send_vk_reply(
    conversation: TestimonialConversation,
    reply_text: str,
    sender_name: str = 'Администратор',
) -> TestimonialMessage:
    """
    Отправляет сообщение от имени группы в ВКонтакте и сохраняет его в тред.
    Требует SenlerConfig с vk_community_token для этой точки.
    Raises ValueError если нет токена или vk_sender_id.
    """
    import random
    import urllib.request
    import urllib.parse
    import json as _json
    from django.utils import timezone
    from apps.tenant.senler.models import SenlerConfig

    if not conversation.vk_sender_id:
        raise ValueError('Нет VK ID отправителя — не знаем кому ответить')

    # VK-originated треды имеют branch=None (см. handle_vk_incoming_message,
    # коммит 7c3e89e). Раньше .get(branch=None) → DoesNotExist → ответы на ВК-
    # отзывы падали с "SenlerConfig не настроен". Все SenlerConfig тенанта
    # делят один vk_group_id/токен, поэтому для ответа берём конфиг точки,
    # а при её отсутствии — активный конфиг тенанта.
    config = None
    if conversation.branch_id:
        config = SenlerConfig.objects.filter(branch_id=conversation.branch_id).first()
    if config is None:
        config = (
            SenlerConfig.objects.filter(is_active=True).order_by('branch_id').first()
            or SenlerConfig.objects.order_by('branch_id').first()
        )
    if config is None:
        raise ValueError('SenlerConfig не настроен для этой точки')

    if not config.vk_community_token:
        raise ValueError('VK community token не задан в настройках')

    # VK messages.send
    params = {
        'user_id':    conversation.vk_sender_id,
        'message':    reply_text,
        'random_id':  random.randint(1, 2**31),
        'access_token': config.vk_community_token,
        'v':          '5.131',
    }
    url = 'https://api.vk.com/method/messages.send?' + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=10) as resp:
        result = _json.loads(resp.read())

    if 'error' in result:
        raise ValueError(f'VK API error: {result["error"].get("error_msg", result["error"])}')

    vk_msg_id = result.get('response', '')
    msg = TestimonialMessage.objects.create(
        conversation=conversation,
        source=TestimonialMessage.Source.ADMIN_REPLY,
        text=reply_text,
        vk_message_id=str(vk_msg_id) if vk_msg_id else '',
    )

    conversation.is_replied = True
    conversation.has_unread = False
    conversation.last_message_at = timezone.now()
    conversation.save(update_fields=['is_replied', 'has_unread', 'last_message_at'])

    return msg


# ── VK membership events ───────────────────────────────────────────────────────

_MEMBERSHIP_EVENTS = frozenset({'group_join', 'group_leave', 'message_allow', 'message_deny'})


def apply_vk_membership_event(
    group_id: int,
    vk_user_id: int,
    event_type: str,
) -> bool:
    """
    Обновляет ClientVKStatus на основе события VK Callback/LongPoll.

    Обрабатывает:
      group_join     → is_community_member=True
      group_leave    → is_community_member=False (сбрасывает joined_at и via_app)
      message_allow  → is_newsletter_subscriber=True
      message_deny   → is_newsletter_subscriber=False (сбрасывает joined_at и via_app)

    Одна VK-группа может быть привязана к нескольким Branch — событие применяется
    ко всем Branch, где зарегистрирован этот пользователь.

    Returns True если хотя бы одна запись была обновлена.
    """
    if event_type not in _MEMBERSHIP_EVENTS:
        return False

    from apps.tenant.senler.models import SenlerConfig

    configs = list(SenlerConfig.objects.select_related('branch').filter(vk_group_id=group_id))
    if not configs:
        return False

    now = timezone.now()
    updated = False

    for config in configs:
        try:
            cb = ClientBranch.objects.select_related('client').get(
                branch=config.branch,
                client__vk_id=vk_user_id,
            )
        except ClientBranch.DoesNotExist:
            continue  # Гость не в этой точке — переходим к следующей

        vk_status, _ = ClientVKStatus.objects.get_or_create(client=cb)
        update_fields: list[str] = []

        if event_type == 'group_join' and not vk_status.is_community_member:
            vk_status.is_community_member = True
            vk_status.community_joined_at = now
            # via_app не трогаем: если уже mark_subscribed() ставил True — сохраняем
            update_fields += ['is_community_member', 'community_joined_at']

        elif event_type == 'group_leave' and vk_status.is_community_member:
            vk_status.is_community_member = False
            vk_status.community_joined_at = None
            vk_status.community_via_app = None
            update_fields += ['is_community_member', 'community_joined_at', 'community_via_app']

        elif event_type == 'message_allow' and not vk_status.is_newsletter_subscriber:
            vk_status.is_newsletter_subscriber = True
            vk_status.newsletter_joined_at = now
            update_fields += ['is_newsletter_subscriber', 'newsletter_joined_at']

        elif event_type == 'message_deny' and vk_status.is_newsletter_subscriber:
            vk_status.is_newsletter_subscriber = False
            vk_status.newsletter_joined_at = None
            vk_status.newsletter_via_app = None
            update_fields += ['is_newsletter_subscriber', 'newsletter_joined_at', 'newsletter_via_app']

        if update_fields:
            vk_status.save(update_fields=update_fields)
            updated = True

    return updated
