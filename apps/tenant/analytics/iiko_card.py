"""
iikoCard: связь гостя LoyalUP с его картой iiko и его заказы (волна 2 ТЗ маркетолога).

Решение владельца 14.10.2026: за флагом сети `ClientConfig.iiko_card_mode`,
по умолчанию выключено; первый режим — «только связь»: читаем, в iiko не пишем.

Как устроено:
  • гость связывается по телефону, который он САМ дал в мини-приложении с
    согласием (`guest.Client.phone`, `phone_consent_at`); других телефонов не
    ищем и списка гостей iikoCard не выгружаем (в API его и нет);
  • iiko Cloud: `loyalty/iiko/customer/info` (type=phone) — нет гостя → 400
    «There is no user with phone…» → статус «не найден», это не ошибка;
  • заказы — операции `customer/transactions/by_revision`, страницами от
    курсора (revision + lastTransactionId); заказ = операция CloseOrder:
    `orderSum` до скидок, `sum` — оплачено. Повтор не задваивает (уникальность
    link + posOrderId);
  • токен iiko Cloud живёт 15 минут — держим 14 в общем кэше, на 401 входим
    заново один раз.

Проверено чтением 14.10.2026 на живом ключе LevOne (через CheckUp): форма
ответов, текст «нет гостя», пагинация по ревизии.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

import httpx
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

BASE_URL = 'https://api-ru.iiko.services/api/1'
TOKEN_TTL = 14 * 60
PAGE_SIZE = 100
MAX_PAGES = 20                     # за один проход на гостя; остальное — следующим тиком
NOT_FOUND_RECHECK = timedelta(days=7)
CLOSE_ORDER = 'CloseOrder'
# iiko Cloud режет частые запросы (429). 14.10.2026 первый проход LevOne: 99
# поисков подряд прошли, а заказы на 34-м госте получили 429. Между запросами —
# пауза; на 429 — ждём и пробуем ещё раз; не помогло — проход останавливается
# до следующего тика (не «долбим» дальше).
MIN_INTERVAL = 0.5
RATE_LIMIT_WAITS = (3, 10)


class IikoCloudError(Exception):
    """Сбой iiko Cloud (сеть, 5xx, неожиданный ответ). Гость остаётся до следующего тика."""


class IikoCloudRateLimited(IikoCloudError):
    """iiko просит подождать даже после повторов — проход прекращаем."""


class IikoCloudClient:
    def __init__(self, api_login: str, http: httpx.Client | None = None):
        if not api_login:
            raise IikoCloudError('не задан apiLogin iiko Cloud')
        self.api_login = api_login
        self.http = http or httpx.Client(timeout=30)
        self._key = 'iikocloud:token:' + hashlib.sha1(api_login.encode()).hexdigest()
        self._last = 0.0
        self.sleep = time.sleep

    def _pace(self):
        wait = MIN_INTERVAL - (time.monotonic() - self._last)
        if wait > 0:
            self.sleep(wait)
        self._last = time.monotonic()

    def _auth(self) -> str:
        resp = self.http.post(f'{BASE_URL}/access_token', json={'apiLogin': self.api_login})
        if resp.status_code != 200:
            raise IikoCloudError(f'вход в iiko Cloud: HTTP {resp.status_code}')
        token = (resp.json() or {}).get('token')
        if not token:
            raise IikoCloudError('вход в iiko Cloud: нет токена')
        cache.set(self._key, token, TOKEN_TTL)
        return token

    def _send(self, path: str, body: dict, token: str) -> httpx.Response:
        self._pace()
        return self.http.post(f'{BASE_URL}/{path}', json=body,
                              headers={'Authorization': f'Bearer {token}'})

    def post(self, path: str, body: dict) -> httpx.Response:
        token = cache.get(self._key) or self._auth()
        try:
            resp = self._send(path, body, token)
            if resp.status_code == 401:
                resp = self._send(path, body, self._auth())
            for wait in RATE_LIMIT_WAITS:
                if resp.status_code != 429:
                    break
                self.sleep(wait)
                resp = self._send(path, body, cache.get(self._key) or self._auth())
        except httpx.HTTPError as exc:
            raise IikoCloudError(f'{path}: {exc.__class__.__name__}') from exc
        if resp.status_code == 429:
            raise IikoCloudRateLimited(f'{path}: iiko просит подождать (429)')
        return resp

    def customer_by_phone(self, organization_id: str, phone: str) -> dict | None:
        """Гость iikoCard по телефону; None — такого телефона в iikoCard нет."""
        resp = self.post('loyalty/iiko/customer/info',
                         {'organizationId': organization_id, 'type': 'phone', 'phone': phone})
        if resp.status_code == 200:
            return resp.json()
        text = resp.text or ''
        if resp.status_code == 400 and 'There is no user' in text:
            return None
        raise IikoCloudError(f'customer/info: HTTP {resp.status_code} {text[:120]}')

    def customer_by_id(self, organization_id: str, customer_id: str) -> dict | None:
        resp = self.post('loyalty/iiko/customer/info',
                         {'organizationId': organization_id, 'type': 'id', 'id': customer_id})
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 400 and 'There is no user' in (resp.text or ''):
            return None
        raise IikoCloudError(f'customer/info: HTTP {resp.status_code} {(resp.text or "")[:120]}')

    def create_customer(self, organization_id: str, card_number: str, *, phone: str = '', name: str = '') -> str:
        """Новый гость iikoCard с картой (режим «Карта гостя»). Возвращает id гостя."""
        body = {'organizationId': organization_id, 'cardTrack': card_number, 'cardNumber': card_number}
        if phone:
            body['phone'] = phone
        if name:
            body['name'] = name[:60]
        resp = self.post('loyalty/iiko/customer/create_or_update', body)
        customer_id = (resp.json() or {}).get('id') if resp.status_code == 200 else None
        if not customer_id:
            raise IikoCloudError(f'create_or_update: HTTP {resp.status_code} {(resp.text or "")[:120]}')
        return customer_id

    def add_card(self, organization_id: str, customer_id: str, card_number: str) -> None:
        resp = self.post('loyalty/iiko/customer/card/add', {
            'organizationId': organization_id, 'customerId': customer_id,
            'cardTrack': card_number, 'cardNumber': card_number,
        })
        if resp.status_code != 200:
            raise IikoCloudError(f'card/add: HTTP {resp.status_code} {(resp.text or "")[:120]}')

    def transactions(self, organization_id: str, customer_id: str,
                     revision: int | None, last_transaction_id: str) -> dict:
        body = {'organizationId': organization_id, 'customerId': customer_id,
                'revision': revision or 0, 'pageSize': PAGE_SIZE}
        if revision and last_transaction_id:
            body['lastTransactionId'] = last_transaction_id
        resp = self.post('loyalty/iiko/customer/transactions/by_revision', body)
        if resp.status_code != 200:
            raise IikoCloudError(f'transactions: HTTP {resp.status_code} {(resp.text or "")[:120]}')
        return resp.json() or {}


# ── Разбор ответов ────────────────────────────────────────────────────────────

def _dt(raw):
    """Время iiko: '…Z' — UTC; без пояса (firstOrderDate) — считаем UTC."""
    if not raw:
        return None
    value = parse_datetime(str(raw).replace('Z', '+00:00'))
    if value is not None and timezone.is_naive(value):
        value = timezone.make_aware(value, dt_timezone.utc)
    return value


def _money(raw):
    if raw in (None, ''):
        return None
    try:
        return Decimal(str(raw)).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        return None


def _wallets(customer: dict) -> list:
    return [
        {'name': w.get('name') or '', 'type': w.get('type'), 'balance': w.get('balance')}
        for w in (customer.get('walletBalances') or [])
    ]


# ── Связь и заказы ────────────────────────────────────────────────────────────

def link_guest(client, config, api: IikoCloudClient, now=None):
    """Найти гостя в iikoCard по его телефону и записать результат. Возвращает PosGuestLink."""
    from apps.tenant.analytics.models import PosGuestLink, PosGuestLinkStatus

    now = now or timezone.now()
    phone = client.phone or ''
    link = PosGuestLink.objects.filter(client=client).first() or PosGuestLink(client=client, checked_at=now)
    link.phone_checked = phone
    link.checked_at = now
    try:
        customer = api.customer_by_phone(config.iiko_cloud_organization_id, phone)
    except IikoCloudRateLimited:
        raise
    except IikoCloudError as exc:
        link.status = PosGuestLinkStatus.ERROR
        link.error = str(exc)[:255]
        link.save()
        return link

    if customer is None:
        link.status = PosGuestLinkStatus.NOT_FOUND
        link.iiko_customer_id = ''
        link.wallets = []
        link.error = ''
        link.save()
        return link

    if link.iiko_customer_id and link.iiko_customer_id != customer.get('id'):
        # Телефон теперь у другой карты: старые заказы чужие — начинаем с нуля.
        link.orders.all().delete()
        link.last_revision = None
        link.last_transaction_id = ''
    link.status = PosGuestLinkStatus.LINKED
    link.iiko_customer_id = customer.get('id') or ''
    link.wallets = _wallets(customer)
    link.first_order_at = _dt(customer.get('firstOrderDate'))
    link.last_order_at = _dt(customer.get('lastProcessedOrderDate'))
    link.linked_at = link.linked_at or now
    link.error = ''
    link.save()
    return link


def sync_orders(link, config, api: IikoCloudClient, now=None, branches: dict | None = None) -> int:
    """
    Новые закрытые заказы гостя с курсора. Возвращает, сколько заказов добавлено.

    `branches` — {организация iiko: Branch}: точка заказа и, в режиме «Карта
    гостя», баллы за заказы после выдачи карты (`pos_cashback_percent`).
    """
    from apps.tenant.analytics.models import PosGuestOrder

    now = now or timezone.now()
    added = 0
    for _ in range(MAX_PAGES):
        page = api.transactions(config.iiko_cloud_organization_id, link.iiko_customer_id,
                                link.last_revision, link.last_transaction_id)
        rows = page.get('transactions') or []
        with transaction.atomic():
            for t in rows:
                if t.get('typeName') != CLOSE_ORDER or not t.get('posOrderId'):
                    continue
                order_sum = _money(t.get('orderSum'))
                closed_at = _dt(t.get('whenCreated'))
                if order_sum is None or closed_at is None:
                    continue
                org_id = str(t.get('organizationId') or '')[:64]
                branch = (branches or {}).get(org_id)
                order, created = PosGuestOrder.objects.get_or_create(
                    link=link, pos_order_id=str(t['posOrderId']),
                    defaults={
                        'branch_id': branch.branch_id if branch is not None else None,
                        'order_number': str(t.get('orderNumber') or '')[:32],
                        'order_sum': order_sum,
                        'paid_sum': _money(t.get('sum')),
                        'is_delivery': bool(t.get('isDelivery')),
                        'organization_id': org_id,
                        'terminal_group_id': str(t.get('terminalGroupId') or '')[:64],
                        'closed_at': closed_at,
                    },
                )
                added += int(created)
                if created and branch is not None:
                    _credit_order(link, order, branch, config)
            if page.get('lastRevision'):
                link.last_revision = page['lastRevision']
                link.last_transaction_id = page.get('lastTransactionId') or ''
            link.orders_synced_at = now
            link.save(update_fields=['last_revision', 'last_transaction_id', 'orders_synced_at'])
        if len(rows) < PAGE_SIZE:
            break
    return added


def _credit_order(link, order, branch, config) -> int:
    """Баллы за заказ по карте гостя: только режим «Карта гостя» и только после выдачи карты."""
    from apps.shared.config.models import IikoCardMode
    from apps.tenant.branch.models import ClientBranch, CoinTransaction, TransactionSource, TransactionType

    percent = int(getattr(config, 'pos_cashback_percent', 0) or 0)
    if (getattr(config, 'iiko_card_mode', '') != IikoCardMode.CARD or percent <= 0
            or not link.card_issued_at or order.closed_at < link.card_issued_at):
        return 0
    base = order.paid_sum if order.paid_sum is not None else order.order_sum
    coins = int(base * percent // 100)
    if coins <= 0:
        return 0
    cb, _ = ClientBranch.objects.get_or_create(client_id=link.client_id, branch=branch)
    CoinTransaction.objects.create_transfer(
        cb, coins, TransactionType.INCOME, TransactionSource.POS,
        description=f'Заказ iiko №{order.order_number or order.pos_order_id}',
    )
    order.coins = coins
    order.save(update_fields=['coins'])
    return coins


def branches_by_organization() -> dict:
    from apps.tenant.branch.models import Branch
    return {b.iiko_organization_id: b for b in Branch.objects.exclude(iiko_organization_id='')}


CARD_PREFIX = '77'


def card_number_for(client) -> str:
    """Номер карты гостя LoyalUP в iikoCard: 77 + id гостя (12 цифр, читается любым сканером)."""
    return f'{CARD_PREFIX}{int(client.pk):010d}'


def _customer_card(customer: dict) -> str:
    for card in customer.get('cards') or []:
        value = card.get('track') or card.get('number')
        if value:
            return str(value)
    return ''


def issue_card(client, config, api: IikoCloudClient, now=None):
    """
    Режим «Карта гостя»: номер, который гость показывает кассиру. Если гость уже
    есть в iikoCard (по связи или телефону с согласием) — его карта, а нет карты —
    добавляем нашу; иначе создаём гостя iikoCard с нашей картой. Возвращает PosGuestLink.
    """
    from apps.tenant.analytics.models import PosGuestLink, PosGuestLinkStatus

    now = now or timezone.now()
    org = config.iiko_cloud_organization_id
    link = PosGuestLink.objects.filter(client=client).first() or PosGuestLink(client=client, checked_at=now)
    if link.card_number and link.iiko_customer_id:
        return link

    customer = None
    if link.status == PosGuestLinkStatus.LINKED and link.iiko_customer_id:
        customer = api.customer_by_id(org, link.iiko_customer_id)
    elif client.phone and client.phone_consent_at:
        customer = api.customer_by_phone(org, client.phone)

    if customer is not None:
        number = _customer_card(customer)
        if not number:
            number = card_number_for(client)
            api.add_card(org, customer['id'], number)
        link.iiko_customer_id = customer.get('id') or ''
        link.wallets = _wallets(customer)
    else:
        number = card_number_for(client)
        phone = client.phone if client.phone_consent_at else ''
        link.iiko_customer_id = api.create_customer(org, number, phone=phone,
                                                    name=getattr(client, 'first_name', '') or '')
        link.wallets = []
    link.status = PosGuestLinkStatus.LINKED
    link.card_number = number
    link.card_issued_at = now
    link.linked_at = link.linked_at or now
    link.phone_checked = client.phone or ''
    link.checked_at = now
    link.error = ''
    link.save()
    return link


def guests_to_link(now=None):
    """Гости сети с телефоном и согласием, которых ещё не проверяли (или пора перепроверить)."""
    from django.db.models import F, Q
    from apps.shared.guest.models import Client
    from apps.tenant.analytics.models import PosGuestLinkStatus

    now = now or timezone.now()
    base = (Client.objects
            .filter(is_active=True, phone_consent_at__isnull=False)
            .exclude(phone='')
            .filter(branch_profiles__isnull=False)
            .distinct())
    # Гость с выданной картой связан навсегда: смена телефона карту не отвязывает.
    base = base.exclude(pos_link__card_issued_at__isnull=False)
    return base.filter(
        Q(pos_link__isnull=True)
        | ~Q(pos_link__phone_checked=F('phone'))
        | Q(pos_link__status=PosGuestLinkStatus.ERROR)
        | Q(pos_link__status=PosGuestLinkStatus.NOT_FOUND, pos_link__checked_at__lt=now - NOT_FOUND_RECHECK)
    )


def run_tenant(config, *, link_limit=200, sync_limit=200, api: IikoCloudClient | None = None, now=None) -> dict:
    """Один проход по текущей схеме: связать новых, забрать заказы связанных."""
    from django.db.models import F
    from apps.tenant.analytics.models import PosGuestLink, PosGuestLinkStatus

    now = now or timezone.now()
    api = api or IikoCloudClient(config.iiko_cloud_api_login)
    out = {'checked': 0, 'linked': 0, 'not_found': 0, 'errors': 0, 'orders_added': 0}
    try:
        for client in guests_to_link(now)[:link_limit]:
            link = link_guest(client, config, api, now)
            out['checked'] += 1
            out[{'linked': 'linked', 'not_found': 'not_found'}.get(link.status, 'errors')] += 1
    except IikoCloudRateLimited as exc:
        out['stopped'] = str(exc)
        return out
    synced = (PosGuestLink.objects.filter(status=PosGuestLinkStatus.LINKED)
              .order_by(F('orders_synced_at').asc(nulls_first=True))[:sync_limit])
    branches = branches_by_organization()
    for link in synced:
        try:
            out['orders_added'] += sync_orders(link, config, api, now, branches=branches)
        except IikoCloudRateLimited as exc:
            out['stopped'] = str(exc)
            break
        except IikoCloudError as exc:
            out['errors'] += 1
            logger.warning('iikoCard: заказы гостя %s не забраны: %s', link.client_id, exc)
    return out


def pos_summary(client_id) -> dict | None:
    """Блок «Связь с кассой» для карточки гостя: None — связи не проверяли."""
    from django.db.models import Count, Max, Sum
    from apps.tenant.analytics.models import PosGuestLink

    link = PosGuestLink.objects.filter(client_id=client_id).first()
    if link is None:
        return None
    agg = link.orders.aggregate(n=Count('id'), total=Sum('order_sum'), paid=Sum('paid_sum'),
                                last=Max('closed_at'))
    return {
        'status': link.status,
        'checked_at': link.checked_at.isoformat() if link.checked_at else None,
        'linked_at': link.linked_at.isoformat() if link.linked_at else None,
        'orders_count': agg['n'] or 0,
        'orders_sum': float(agg['total'] or 0),
        'paid_sum': float(agg['paid'] or 0),
        'last_order_at': agg['last'].isoformat() if agg['last'] else None,
        'first_order_at': link.first_order_at.isoformat() if link.first_order_at else None,
        'orders_synced_at': link.orders_synced_at.isoformat() if link.orders_synced_at else None,
        'wallets': link.wallets or [],
    }
