"""
Бизнес-логика механики «игра через сториз» (внешние пользователи).

Сущность — StoryGiftEntry (один на client_branch = один на VK ID на точку).
Активация подарка возможна ТОЛЬКО после ввода кода дня (DailyCodePurpose.GAME)
в кафе — это доказательство присутствия. Домашняя активация показывает
инструкцию и НЕ запускает таймер (ТЗ §9).

Метрики (Фаза 3) считаются как COUNT записей StoryGiftEntry:
  «Получили через сториз»   = received_at  заполнен
  «Активировали через сториз» = activated_at заполнен
Поэтому здесь достаточно корректно проставлять эти таймстемпы через методы модели.
"""

from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone

from apps.tenant.branch.models import (
    ClientBranch,
    DailyCode, DailyCodePurpose,
    current_code_date,
)
from apps.tenant.catalog.models import Product

from ..models import StoryGiftEntry, StoryStatus


# ── Exceptions ────────────────────────────────────────────────────────────────

class ClientNotFound(Exception):
    pass


class StoryDisabled(Exception):
    """Механика выключена для точки или вне периода кампании."""
    pass


class StoryAlreadyPlayed(Exception):
    """VK ID уже играл через сториз на этой точке."""
    pass


class StoryNotPlayed(Exception):
    """Попытка выбрать подарок до игры."""
    pass


class NoStoryGifts(Exception):
    """Нет настроенных подарков для сториз на точке."""
    pass


class StoryGiftNotFound(Exception):
    """Нет записи story-подарка в ожидаемом состоянии."""
    pass


class StoryAlreadySelected(Exception):
    pass


class ProductNotFound(Exception):
    pass


class StoryAlreadyActivated(Exception):
    pass


class StoryActivationDenied(Exception):
    """
    Активация не выполнена (нет кода дня / условия не выполнены).
    Несёт текст инструкции для показа пользователю; таймер НЕ запускается.
    """
    def __init__(self, instruction_text: str = '', reason: str = 'need_code'):
        self.instruction_text = instruction_text
        self.reason = reason


# ── Defaults ──────────────────────────────────────────────────────────────────

_DEFAULT_MIN_ORDER          = 600
_DEFAULT_ACTIVATION_MINUTES = 40
_DEFAULT_REQUIRE_CAFE       = True

_DEFAULT_ACTIVATION_TEXT = (
    'Чтобы активировать подарок «[название подарка]», приходите в «[название кафе]» '
    '(адрес: [адрес кафе]). Сделайте заказ от [сумма] ₽, попросите у сотрудника код '
    'дня, отсканируйте QR ЛоялАпп и активируйте подарок здесь, в разделе «Мои подарки». '
    'После активации он будет действовать [время] минут.'
)
_DEFAULT_SAVED_TEXT = (
    'Подарок «[название подарка]» сохранён в разделе «Мои подарки». '
    'Заберите его в кафе!'
)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_client_branch(vk_id: int, branch_id: int) -> ClientBranch:
    try:
        return ClientBranch.objects.select_related('branch', 'branch__config').get(
            client__vk_id=vk_id, branch__branch_id=branch_id,
        )
    except ClientBranch.DoesNotExist:
        raise ClientNotFound


def _network_config():
    """ClientConfig текущего тенанта (или None). Защищено от FakeTenant в shell-контексте."""
    try:
        from apps.shared.config.models import ClientConfig
        company = getattr(connection, 'tenant', None)
        if company is None or not getattr(company, 'pk', None):
            return None
        return ClientConfig.objects.filter(company=company).first()
    except Exception:
        return None


def _resolve_story_settings(cb: ClientBranch) -> dict:
    """Резолв настроек сториз: точка (override) → сеть → хардкод-дефолт."""
    b = getattr(cb.branch, 'config', None)
    net = _network_config()

    def _pick(branch_val, net_val, default, *, truthy=False):
        # truthy=True → пустое/0/None ветки и сети трактуем как «не задано»
        if truthy:
            if branch_val:
                return branch_val
            if net_val:
                return net_val
            return default
        if branch_val is not None:
            return branch_val
        if net_val is not None:
            return net_val
        return default

    enabled = _pick(
        getattr(b, 'story_game_enabled', None) if b else None,
        getattr(net, 'story_game_enabled', None) if net else None,
        False,
    )
    min_order = _pick(
        getattr(b, 'story_min_order_amount', None) if b else None,
        getattr(net, 'story_min_order_amount', None) if net else None,
        _DEFAULT_MIN_ORDER, truthy=True,
    )
    activation_minutes = _pick(
        None,
        getattr(net, 'story_activation_minutes', None) if net else None,
        _DEFAULT_ACTIVATION_MINUTES, truthy=True,
    )
    require_cafe = _pick(
        None,
        getattr(net, 'story_require_cafe_visit', None) if net else None,
        _DEFAULT_REQUIRE_CAFE,
    )
    cafe_address = _pick(
        getattr(b, 'story_cafe_address', '') if b else '',
        getattr(net, 'story_cafe_address', '') if net else '',
        (getattr(b, 'address', '') if b else ''), truthy=True,
    )
    activation_text = _pick(
        getattr(b, 'story_activation_text', '') if b else '',
        getattr(net, 'story_activation_text', '') if net else '',
        _DEFAULT_ACTIVATION_TEXT, truthy=True,
    )
    saved_text = _pick(
        getattr(b, 'story_saved_text', '') if b else '',
        getattr(net, 'story_saved_text', '') if net else '',
        _DEFAULT_SAVED_TEXT, truthy=True,
    )
    return {
        'enabled':            bool(enabled),
        'min_order_amount':   int(min_order),
        'activation_minutes': int(activation_minutes),
        'require_cafe_visit': bool(require_cafe),
        'cafe_address':       cafe_address,
        'activation_text':    activation_text,
        'saved_text':         saved_text,
        'campaign_start':     getattr(net, 'story_campaign_start', None) if net else None,
        'campaign_end':       getattr(net, 'story_campaign_end', None) if net else None,
    }


def _campaign_active(settings: dict) -> bool:
    today = timezone.localdate()
    if settings['campaign_start'] and today < settings['campaign_start']:
        return False
    if settings['campaign_end'] and today > settings['campaign_end']:
        return False
    return True


def render_story_text(template: str, *, cafe_name: str, settings: dict, gift_name: str) -> str:
    """Подставляет переменные ТЗ §5.2 в текст."""
    return (
        (template or '')
        .replace('[адрес кафе]', settings.get('cafe_address') or '')
        .replace('[сумма]', str(settings.get('min_order_amount', 0)))
        .replace('[время]', str(settings.get('activation_minutes', 0)))
        .replace('[название кафе]', cafe_name or '')
        .replace('[название подарка]', gift_name or '')
    )


def _story_gifts_qs(branch):
    """Пул подарков для сториз на точке (is_story_prize, активные, не в архиве)."""
    return (
        Product.objects
        .filter(branch_assignments__branch=branch, is_story_prize=True, is_archived=False)
        .annotate(branch_ordering=F('branch_assignments__ordering'))
        .order_by('branch_ordering', 'name')
    )


# ── Public service functions ──────────────────────────────────────────────────

def get_story_settings(vk_id: int, branch_id: int) -> dict:
    """Публичный резолв story-настроек для пользователя (для контекста сериализатора)."""
    return _resolve_story_settings(_get_client_branch(vk_id, branch_id))


def get_story_access(vk_id: int, branch_id: int) -> dict:
    """
    Состояние доступа к игре через сториз для пользователя.

    enabled        — механика включена и кампания активна
    can_play       — можно играть сейчас (включено и ещё не играл)
    already_played — VK ID уже играл на этой точке
    status         — текущий статус StoryGiftEntry (или available_to_play)
    """
    cb = _get_client_branch(vk_id, branch_id)
    settings = _resolve_story_settings(cb)
    entry = StoryGiftEntry.objects.filter(client_branch=cb).first()
    already_played = bool(entry and entry.played_at)
    enabled = settings['enabled'] and _campaign_active(settings)
    return {
        'enabled':        enabled,
        'can_play':       enabled and not already_played,
        'already_played': already_played,
        'status':         entry.status if entry else StoryStatus.AVAILABLE_TO_PLAY,
        'has_gifts':      _story_gifts_qs(cb.branch).exists(),
    }


@transaction.atomic
def play_story_game(
    vk_id: int, branch_id: int,
    source: str = 'story', source_ref: str = '',
) -> StoryGiftEntry:
    """
    Пользователь играет в игру через сториз (одноразово на VK ID на точку).
    По логике ТЗ пользователь всегда выигрывает — далее выбирает подарок.

    source/source_ref — источник входа: 'story' (сториз, по умолчанию) или
    'website' (QR с сайта клиента). source_ref — метка сайта для аналитики.
    Для website-входа подарок «сетевой»: забирается в любой точке сети по её
    коду дня (см. activate_story_gift).

    Raises:
        ClientNotFound     — нет профиля
        StoryDisabled      — механика выключена / вне кампании
        NoStoryGifts       — нет настроенных подарков для сториз
        StoryAlreadyPlayed — уже играл
    """
    try:
        cb = (
            ClientBranch.objects
            .select_for_update()
            .select_related('branch')
            .get(client__vk_id=vk_id, branch__branch_id=branch_id)
        )
    except ClientBranch.DoesNotExist:
        raise ClientNotFound

    settings = _resolve_story_settings(cb)
    if not settings['enabled'] or not _campaign_active(settings):
        raise StoryDisabled
    if not _story_gifts_qs(cb.branch).exists():
        raise NoStoryGifts

    entry, _created = StoryGiftEntry.objects.get_or_create(
        client_branch=cb,
        defaults={'source': source or 'story', 'campaign_key': source_ref or ''},
    )
    if entry.played_at:
        raise StoryAlreadyPlayed
    entry.mark_played()
    return entry


def get_story_gifts(vk_id: int, branch_id: int):
    """
    Пул подарков для сториз (для экрана выбора).

    Raises:
        ClientNotFound — нет профиля
        StoryNotPlayed — ещё не играл (нечего выбирать)
        NoStoryGifts   — пул пуст
    """
    cb = _get_client_branch(vk_id, branch_id)
    entry = StoryGiftEntry.objects.filter(client_branch=cb).first()
    if not entry or not entry.played_at:
        raise StoryNotPlayed
    qs = _story_gifts_qs(cb.branch)
    if not qs.exists():
        raise NoStoryGifts
    return qs


@transaction.atomic
def select_story_gift(vk_id: int, branch_id: int, product_id: int) -> StoryGiftEntry:
    """
    Пользователь выбирает подарок из набора сториз → сохраняем в «Мои подарки».
    Фиксирует received_at → метрика «Получили подарок через сториз».

    Raises:
        ClientNotFound       — нет профиля
        StoryNotPlayed       — ещё не играл
        StoryAlreadySelected — подарок уже выбран
        ProductNotFound      — товар не из набора сториз этой точки
    """
    try:
        cb = (
            ClientBranch.objects
            .select_for_update()
            .select_related('branch')
            .get(client__vk_id=vk_id, branch__branch_id=branch_id)
        )
    except ClientBranch.DoesNotExist:
        raise ClientNotFound

    entry = (
        StoryGiftEntry.objects
        .select_for_update(of=('self',))
        .filter(client_branch=cb)
        .first()
    )
    if not entry or not entry.played_at:
        raise StoryNotPlayed
    if entry.selected_at:
        raise StoryAlreadySelected

    try:
        product = _story_gifts_qs(cb.branch).get(pk=product_id)
    except Product.DoesNotExist:
        raise ProductNotFound

    settings = _resolve_story_settings(cb)
    entry.select_gift(
        product,
        min_order_amount=settings['min_order_amount'],
        duration=settings['activation_minutes'],
    )

    # Атрибуция подписки к источнику «сториз»: если гость подписался через приложение
    # ради получения story-подарка (только что, при «Забрать»), проставим source=story,
    # перебивая инференс cafe. Гард по свежести (10 мин) — чтобы не трогать старые кафе-подписки.
    from datetime import timedelta
    from django.utils import timezone
    from apps.tenant.branch.models import ClientVKStatus, SubscriptionSource
    # Источник подписки = website для входа с сайта, иначе story.
    sub_source = (
        SubscriptionSource.WEBSITE if entry.source == 'website'
        else SubscriptionSource.STORY
    )
    vk = ClientVKStatus.objects.filter(client=cb).first()
    if vk:
        recent = timezone.now() - timedelta(minutes=10)
        fields = []
        if (vk.community_via_app and vk.community_source in (None, SubscriptionSource.CAFE)
                and vk.community_joined_at and vk.community_joined_at >= recent):
            vk.community_source = sub_source
            fields.append('community_source')
        if (vk.newsletter_via_app and vk.newsletter_source in (None, SubscriptionSource.CAFE)
                and vk.newsletter_joined_at and vk.newsletter_joined_at >= recent):
            vk.newsletter_source = sub_source
            fields.append('newsletter_source')
        if fields:
            vk.save(update_fields=fields)

    return entry


def get_story_gift(vk_id: int, branch_id: int) -> StoryGiftEntry | None:
    """Текущая запись story-подарка пользователя (для «Мои подарки»), или None."""
    cb = _get_client_branch(vk_id, branch_id)
    return (
        StoryGiftEntry.objects
        .select_related('product', 'client_branch__branch', 'client_branch__branch__config')
        .filter(client_branch=cb)
        .first()
    )


def _validate_game_code(branch, code: str | None) -> None:
    if not code:
        raise StoryActivationDenied(reason='need_code')
    daily = DailyCode.objects.filter(
        branch=branch,
        purpose=DailyCodePurpose.GAME,
        valid_date=current_code_date(),
    ).first()
    if not daily or daily.code != code.upper().strip():
        raise StoryActivationDenied(reason='bad_code')


def _validate_game_code_network(code: str | None):
    """
    Валидация кода дня для СЕТЕВОГО подарка (вход с сайта): принимает код дня
    ЛЮБОЙ активной точки сети и возвращает эту точку. Так гость, сыгравший на
    сайте, забирает подарок в любой из точек по её коду дня.
    """
    if not code:
        raise StoryActivationDenied(reason='need_code')
    daily = (
        DailyCode.objects
        .filter(
            purpose=DailyCodePurpose.GAME,
            valid_date=current_code_date(),
            code=code.upper().strip(),
            branch__is_active=True,
        )
        .select_related('branch')
        .first()
    )
    if not daily:
        raise StoryActivationDenied(reason='bad_code')
    return daily.branch


@transaction.atomic
def activate_story_gift(vk_id: int, branch_id: int, code: str | None = None) -> StoryGiftEntry:
    """
    Фактическая активация подарка из сториз в кафе.

    Требует ввод кода дня (DailyCodePurpose.GAME) — доказательство присутствия.
    Без валидного кода поднимает StoryActivationDenied с текстом инструкции
    (таймер НЕ запускается, метрика активации НЕ растёт — ТЗ §9).

    Raises:
        ClientNotFound        — нет профиля
        StoryGiftNotFound     — нет подарка в состоянии «ожидает визита»
        StoryAlreadyActivated — уже активирован
        StoryActivationDenied — код не введён/неверный (несёт текст инструкции)
    """
    try:
        cb = (
            ClientBranch.objects
            .select_for_update()
            .select_related('branch')
            .get(client__vk_id=vk_id, branch__branch_id=branch_id)
        )
    except ClientBranch.DoesNotExist:
        raise ClientNotFound

    entry = (
        StoryGiftEntry.objects
        .select_for_update(of=('self',))
        .select_related('product')
        .filter(client_branch=cb)
        .first()
    )
    if not entry:
        raise StoryGiftNotFound
    if entry.status in (StoryStatus.ACTIVATED, StoryStatus.EXPIRED, StoryStatus.USED):
        raise StoryAlreadyActivated
    if entry.status != StoryStatus.WAITING_CAFE_VISIT:
        # ещё не выбрал подарок / не получил
        raise StoryGiftNotFound

    settings = _resolve_story_settings(cb)
    activated_branch = None
    if settings['require_cafe_visit']:
        try:
            if entry.source == 'website':
                # Сетевой подарок: код дня любой точки сети → она и есть точка забора.
                activated_branch = _validate_game_code_network(code)
            else:
                _validate_game_code(cb.branch, code)
        except StoryActivationDenied as denied:
            denied.instruction_text = render_story_text(
                settings['activation_text'],
                cafe_name=cb.branch.name,
                settings=settings,
                gift_name=entry.product.name if entry.product else '',
            )
            raise denied

    entry.activate(activated_branch=activated_branch)
    return entry
