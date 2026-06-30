"""
Бизнес-логика сетевого входа из каталога VK.

Работает на публичной схеме, но для подарка/активации заходит в схему выбранного
тенанта через schema_context. Приветственный подарок = StoryGiftEntry(source='vk_catalog')
— переиспользует всю готовую механику активации по сетевому коду дня.
"""

from django.utils import timezone
from django_tenants.utils import tenant_context

from apps.shared.clients.models import Company, Domain
from .models import DiscoveryEvent, DiscoveryStage, DiscoveryClaim


# ── Exceptions ────────────────────────────────────────────────────────────────

class DiscoveryError(Exception):
    pass


class AlreadyClaimed(DiscoveryError):
    """У гостя уже есть приветственный приз (1 на человека)."""
    pass


class CityNotAvailable(DiscoveryError):
    """Выбранный город/сеть не участвует или недоступен."""
    pass


class NoWelcomeGift(DiscoveryError):
    """У сети нет настроенного приветственного подарка / активной точки."""
    pass


class NotClaimed(DiscoveryError):
    """Гость ещё не выбрал город — нечего активировать."""
    pass


class ActivationDenied(DiscoveryError):
    """Код дня не введён/неверный. Несёт текст-инструкцию."""
    def __init__(self, instruction_text: str = '', reason: str = 'need_code'):
        self.instruction_text = instruction_text
        self.reason = reason


# ── Helpers ───────────────────────────────────────────────────────────────────

def _participating_companies():
    """[(Company, city)] — активные сети, включённые в каталог VK, с заданным городом."""
    from apps.shared.config.models import ClientConfig
    today = timezone.localdate()
    out = []
    cfgs = (
        ClientConfig.objects
        .select_related('company')
        .filter(vk_catalog_enabled=True)
        .exclude(vk_catalog_city='')
    )
    for cfg in cfgs:
        company = cfg.company
        if not company.is_active:
            continue
        if company.paid_until and company.paid_until < today:
            continue
        out.append((company, cfg.vk_catalog_city))
    return out


def _company_by_client_id(client_id: int):
    try:
        return Company.objects.get(client_id=client_id)
    except Company.DoesNotExist:
        raise CityNotAvailable


def _company_primary_domain(company) -> str | None:
    d = (
        Domain.objects.filter(tenant=company, is_primary=True).first()
        or Domain.objects.filter(tenant=company).first()
    )
    return d.domain if d else None


# ── Cities ────────────────────────────────────────────────────────────────────

def list_cities() -> list[dict]:
    """
    Список городов-участников с точками (для экрана выбора).
    Город = тенант. В список попадают только сети с включённым каталогом VK,
    заданным городом, активной точкой и настроенным приветственным подарком.
    """
    from apps.tenant.branch.models import Branch
    from apps.tenant.catalog.models import Product

    result = []
    for company, city in _participating_companies():
        with tenant_context(company):
            has_gift = Product.objects.filter(
                is_vk_catalog_welcome=True, is_archived=False,
            ).exists()
            if not has_gift:
                continue
            branches = _branches_payload()
        if not branches:
            continue
        result.append({
            'client_id': company.client_id,
            'city': city,
            'company_name': company.name,
            'branches': branches,
        })
    result.sort(key=lambda r: (r['city'] or '').lower())
    return result


# ── Funnel events ─────────────────────────────────────────────────────────────

def record_open(vk_id: int) -> None:
    DiscoveryEvent.record(vk_id, DiscoveryStage.OPEN)


def record_claim_open(vk_id: int) -> None:
    DiscoveryEvent.record(vk_id, DiscoveryStage.CLAIM_OPEN)


def play(vk_id: int) -> dict:
    """Гость крутанул колесо. Приз один (фиксированный) — колесо косметическое."""
    DiscoveryEvent.record(vk_id, DiscoveryStage.PLAY)
    return {'won': True}


# ── Welcome gift in tenant schema ─────────────────────────────────────────────

def _create_welcome_gift(vk_id: int, *, first_name='', last_name='', photo_url=''):
    """
    В схеме текущего тенанта: создаёт ClientBranch (без визита/скана) и
    StoryGiftEntry(source='vk_catalog') в состоянии «ожидает визита».
    Возвращает (branch, entry, settings).
    """
    from apps.shared.guest.models import Client
    from apps.tenant.branch.models import Branch, ClientBranch
    from apps.tenant.catalog.models import Product
    from apps.tenant.inventory.models import StoryGiftEntry
    from apps.tenant.inventory.api.story_services import _resolve_story_settings

    product = (
        Product.objects
        .filter(is_vk_catalog_welcome=True, is_archived=False)
        .order_by('id')
        .first()
    )
    if not product:
        raise NoWelcomeGift
    branch = Branch.objects.filter(is_active=True).order_by('id').first()
    if not branch:
        raise NoWelcomeGift

    # Клиент (public) + профиль на точке (tenant). БЕЗ записи визита/скана —
    # это онлайн-приз, а не посещение кафе (метрики сканов не трогаем).
    client, _ = Client.objects.get_or_create(
        vk_id=vk_id,
        defaults={'first_name': first_name, 'last_name': last_name, 'photo_url': photo_url},
    )
    cb, _ = ClientBranch.objects.get_or_create(client=client, branch=branch)

    settings = _resolve_story_settings(cb)
    entry, _ = StoryGiftEntry.objects.get_or_create(
        client_branch=cb,
        defaults={'source': 'vk_catalog', 'campaign_key': 'vk_catalog'},
    )
    # Сразу «сыграл + выбрал + получил» → статус WAITING_CAFE_VISIT.
    now = timezone.now()
    entry.source = 'vk_catalog'
    entry.campaign_key = 'vk_catalog'
    entry.product = product
    entry.min_order_amount = settings['min_order_amount']
    entry.duration = settings['activation_minutes']
    if not entry.played_at:
        entry.played_at = now
    if not entry.selected_at:
        entry.selected_at = now
        entry.received_at = now
    entry.save()
    return branch, entry, settings


def _serialize_gift(company, city, branch, entry, settings, branches) -> dict:
    from apps.tenant.inventory.api.story_services import render_story_text
    gift_name = entry.product.name if entry.product else ''
    activation_text = render_story_text(
        settings['activation_text'],
        cafe_name=branch.name, settings=settings, gift_name=gift_name,
    )
    return {
        'claimed': True,
        'city': city,
        'company_name': company.name,
        'client_id': company.client_id,
        'home_branch_id': branch.branch_id,
        'gift_name': gift_name,
        'min_order_amount': settings['min_order_amount'],
        'duration_minutes': settings['activation_minutes'],
        'status': entry.status,
        'activation_text': activation_text,
        'branches': branches,
    }


def _branch_dict(b):
    # address/телефон/карты живут на BranchConfig (relation 'config'), не на Branch.
    cfg = getattr(b, 'config', None)
    return {
        'branch_id': b.branch_id,
        'name': b.name,
        'address': getattr(cfg, 'address', '') or '',
        'yandex_map': getattr(cfg, 'yandex_map', '') or '',
        'gis_map': getattr(cfg, 'gis_map', '') or '',
    }


def _branches_payload():
    from apps.tenant.branch.models import Branch
    return [
        _branch_dict(b)
        for b in Branch.objects.filter(is_active=True).select_related('config').order_by('name')
    ]


# ── Claim (choose city) ───────────────────────────────────────────────────────

def claim(vk_id: int, client_id: int, *, first_name='', last_name='', photo_url='') -> dict:
    """
    Гость выбрал город → создаём приветственный приз в этой сети.
    1 приз на человека (unique vk_id в DiscoveryClaim).
    """
    existing = DiscoveryClaim.objects.filter(vk_id=vk_id).first()
    if existing:
        raise AlreadyClaimed

    company = _company_by_client_id(client_id)
    # Сеть должна реально участвовать (включён каталог + задан город + активна).
    city = next(
        (cty for c, cty in _participating_companies() if c.pk == company.pk),
        None,
    )
    if city is None:
        raise CityNotAvailable

    with tenant_context(company):
        branch, entry, settings = _create_welcome_gift(
            vk_id, first_name=first_name, last_name=last_name, photo_url=photo_url,
        )
        branches = _branches_payload()
        payload = _serialize_gift(company, city, branch, entry, settings, branches)

    try:
        DiscoveryClaim.objects.create(
            vk_id=vk_id, company=company, city=city, home_branch_id=branch.branch_id,
        )
    except Exception:
        # гонка двойного клейма — приз уже создан, возвращаем статус
        raise AlreadyClaimed
    return payload


# ── Status (resume) ───────────────────────────────────────────────────────────

def status(vk_id: int) -> dict:
    claim_obj = DiscoveryClaim.objects.select_related('company').filter(vk_id=vk_id).first()
    if not claim_obj:
        return {'claimed': False}
    company = claim_obj.company
    from apps.tenant.inventory.api.story_services import _resolve_story_settings
    from apps.tenant.branch.models import Branch
    with tenant_context(company):
        from apps.tenant.inventory.models import StoryGiftEntry
        branch = Branch.objects.filter(branch_id=claim_obj.home_branch_id).first()
        entry = (
            StoryGiftEntry.objects
            .select_related('product')
            .filter(client_branch__client__vk_id=vk_id,
                    client_branch__branch__branch_id=claim_obj.home_branch_id)
            .first()
        )
        if not branch or not entry:
            return {'claimed': True, 'city': claim_obj.city, 'company_name': company.name,
                    'client_id': company.client_id, 'home_branch_id': claim_obj.home_branch_id,
                    'status': 'unknown'}
        settings = _resolve_story_settings(entry.client_branch)
        branches = _branches_payload()
        return _serialize_gift(company, claim_obj.city, branch, entry, settings, branches)


# ── Activate at register (network day code) ───────────────────────────────────

def activate(vk_id: int, code: str | None) -> dict:
    """Активация приветственного приза на кассе по коду дня любой точки сети."""
    from apps.tenant.inventory.api import story_services as ss

    claim_obj = DiscoveryClaim.objects.select_related('company').filter(vk_id=vk_id).first()
    if not claim_obj:
        raise NotClaimed
    company = claim_obj.company

    with tenant_context(company):
        try:
            entry = ss.activate_story_gift(vk_id, claim_obj.home_branch_id, code)
        except ss.StoryActivationDenied as denied:
            raise ActivationDenied(
                instruction_text=getattr(denied, 'instruction_text', ''),
                reason=getattr(denied, 'reason', 'need_code'),
            )
        from apps.tenant.branch.models import Branch
        branch = Branch.objects.filter(branch_id=claim_obj.home_branch_id).first()
        settings = ss._resolve_story_settings(entry.client_branch)
        branches = _branches_payload()
        payload = _serialize_gift(company, claim_obj.city, branch, entry, settings, branches)

    redeemed_branch_id = entry.activated_branch.branch_id if entry.activated_branch else None
    claim_obj.mark_redeemed(redeemed_branch_id)
    return payload
