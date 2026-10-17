"""
Плагин iikoFront «LoyalUP»: подарки гостя на кассе (17.10.2026, слово владельца «пиши»).

Контракт — `CheckUp/iiko-front-plugin/README.md`:

    GET  /api/v1/pos/plugin/gifts?card=<номер>           → гость и его активированные подарки
    POST /api/v1/pos/plugin/gifts/<id>/redeem  {order_id, order_sum}  → подарок выдан в заказе
    POST /api/v1/pos/plugin/gifts/<id>/release {order_id}             → заказ отменён до оплаты

Вход — заголовок `X-Plugin-Key: <ключ точки>`; в базе только sha256 ключа
(`Branch.pos_plugin_key_hash`). Тенант — по хосту, как у всего API. Флаг сети
`ClientConfig.pos_plugin_enabled` выключен → 404 на всё, как будто ручек нет.

Карта — номер «Карты гостя» iikoCard (`PosGuestLink.card_number`), который гость
показывает кассиру QR-кодом. Подарки — `InventoryItem` гостя во ВСЕХ точках сети
в статусе «активирован» (гость нажал «Активировать» в приложении).

redeem идемпотентен по заказу: повтор с тем же `order_id` — тот же ответ `ok`,
подарок, уже выданный в ДРУГОМ заказе, — 409 `already_used`. release до оплаты
ничего не списывал и ничего не возвращает: подарок остаётся активным; release
для подарка, уже выданного в этом заказе, — 409 `already_redeemed` (возврат
оплаченного заказа плагин не отменяет — решает управляющий).

Выдача ключа точки (кабинет CheckUp → Лояльность → Точки, network_admin):

    GET  /api/v1/branches/<id>/pos-plugin-key/  → {has_key, hint}
    POST /api/v1/branches/<id>/pos-plugin-key/  → {key, hint} — ключ показывается ОДИН раз,
                                                   прежний перестаёт работать
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from decimal import Decimal, InvalidOperation

from django.db import connection, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

log = logging.getLogger(__name__)

HEADER = 'HTTP_X_PLUGIN_KEY'


def key_hash(key: str) -> str:
    return hashlib.sha256((key or '').encode()).hexdigest()


def new_key() -> str:
    return 'lup_' + secrets.token_urlsafe(24)


def _err(code, detail, st):
    return Response({'ok': False, 'code': code, 'detail': detail}, status=st)


def _config():
    tenant = getattr(connection, 'tenant', None)
    return getattr(tenant, 'config', None) if tenant is not None else None


def plugin_enabled() -> bool:
    cfg = _config()
    return bool(cfg is not None and getattr(cfg, 'pos_plugin_enabled', False))


def branch_by_key(raw: str):
    """Точка по ключу. Сравнение — по хешу; пустой ключ не находит ничего."""
    from apps.tenant.branch.models import Branch
    raw = (raw or '').strip()
    if len(raw) < 16:
        return None
    h = key_hash(raw)
    branch = Branch.objects.filter(pos_plugin_key_hash=h, is_active=True).first()
    if branch is None or not hmac.compare_digest(branch.pos_plugin_key_hash, h):
        return None
    return branch


class _PluginThrottle(SimpleRateThrottle):
    """Перебор ключей и карт: 120 запросов в минуту с адреса."""
    scope = 'pos_plugin'
    rate = '120/min'

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': self.get_ident(request)}


class _PluginView(APIView):
    authentication_classes = []
    permission_classes = []
    throttle_classes = [_PluginThrottle]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not plugin_enabled():
            raise NotFound('Не найдено.')

    def _auth(self, request):
        self.branch = branch_by_key(request.META.get(HEADER, ''))
        if self.branch is None:
            return _err('bad_key', 'Ключ точки не подходит.', status.HTTP_401_UNAUTHORIZED)
        return None


def _gift_json(item):
    product = item.product
    return {
        'id': item.pk,
        'title': product.name if product else 'Подарок',
        'iiko_product_id': (getattr(product, 'iiko_product_id', '') or None) if product else None,
        # Плагин показывает строку кассиру как есть: «до 17.10 13:40».
        'expires_at': timezone.localtime(item.expires_at).strftime('%d.%m %H:%M') if item.expires_at else None,
        'min_order': int(item.min_order_amount or 0),
    }


def _guest_by_card(card: str):
    from apps.tenant.analytics.models import PosGuestLink
    card = (card or '').strip()
    if not card:
        return None
    link = PosGuestLink.objects.select_related('client').filter(card_number=card).first()
    return link.client if link is not None else None


class PluginGiftsView(_PluginView):
    def get(self, request):
        from apps.tenant.inventory.models import InventoryItem

        error = self._auth(request)
        if error:
            return error
        guest = _guest_by_card(request.query_params.get('card'))
        if guest is None:
            # 200, а не 404: плагин на любой не-2xx пишет «LoyalUP недоступен».
            return Response({'guest': None, 'gifts': [], 'code': 'card_not_found'})
        now = timezone.now()
        items = (InventoryItem.objects.select_related('product')
                 .filter(client_branch__client=guest, activated_at__isnull=False, used_at__isnull=True)
                 .order_by('expires_at', 'pk'))
        gifts = [_gift_json(i) for i in items if not (i.expires_at and i.expires_at <= now)]
        log.info('pos-plugin: точка %s, карта …%s, подарков %s',
                 self.branch.branch_id, request.query_params.get('card', '')[-4:], len(gifts))
        return Response({'guest': {'name': (guest.first_name or '').strip() or 'Гость'}, 'gifts': gifts})


def _order(request):
    order_id = str(request.data.get('order_id') or '').strip()[:64]
    return order_id


class PluginRedeemView(_PluginView):
    def post(self, request, pk):
        from apps.tenant.inventory.models import InventoryItem, ItemStatus

        error = self._auth(request)
        if error:
            return error
        order_id = _order(request)
        if not order_id:
            return _err('order_required', 'Нужен номер заказа.', status.HTTP_400_BAD_REQUEST)
        raw_sum = request.data.get('order_sum')
        try:
            order_sum = Decimal(str(raw_sum)) if raw_sum not in (None, '') else None
        except (InvalidOperation, ValueError):
            return _err('bad_sum', 'Сумма заказа — число.', status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            item = InventoryItem.objects.select_for_update().filter(pk=pk).first()
            if item is None:
                return _err('gift_not_found', 'Подарок не найден.', status.HTTP_404_NOT_FOUND)
            if item.used_at:
                if item.pos_order_id == order_id:
                    return Response({'ok': True, 'repeat': True})
                return _err('already_used', 'Подарок уже выдан.', status.HTTP_409_CONFLICT)
            if item.status != ItemStatus.ACTIVE:
                return _err('not_active', 'Подарок не активирован или истёк.', status.HTTP_409_CONFLICT)
            if item.min_order_amount and order_sum is not None and order_sum < item.min_order_amount:
                return _err('min_order', f'Подарок от {item.min_order_amount} ₽ в заказе.',
                            status.HTTP_409_CONFLICT)
            item.used_at = timezone.now()
            item.pos_order_id = order_id
            item.pos_order_sum = order_sum
            item.pos_branch = self.branch
            item.save(update_fields=['used_at', 'pos_order_id', 'pos_order_sum', 'pos_branch'])
        log.info('pos-plugin: подарок %s выдан, точка %s, заказ %s', pk, self.branch.branch_id, order_id)
        return Response({'ok': True})


class PluginReleaseView(_PluginView):
    def post(self, request, pk):
        from apps.tenant.inventory.models import InventoryItem

        error = self._auth(request)
        if error:
            return error
        order_id = _order(request)
        item = InventoryItem.objects.filter(pk=pk).first()
        if item is None:
            return _err('gift_not_found', 'Подарок не найден.', status.HTTP_404_NOT_FOUND)
        if item.used_at and item.pos_order_id and item.pos_order_id == order_id:
            return _err('already_redeemed', 'Заказ уже оплачен — подарок выдан.', status.HTTP_409_CONFLICT)
        log.info('pos-plugin: подарок %s отпущен, точка %s, заказ %s', pk, self.branch.branch_id, order_id)
        return Response({'ok': True})


# ── Выдача ключа точки (кабинет сети) ──────────────────────────────────────────

def _may_manage(user) -> bool:
    return bool(getattr(user, 'is_superuser', False)
                or getattr(user, 'role', '') == 'network_admin'
                or getattr(user, 'is_network_admin', False) is True)


class BranchPluginKeyView(APIView):
    permission_classes = [IsAuthenticated]

    def _branch(self, request, pk):
        from apps.shared.users.access import current_schema_name, has_tenant_access
        from apps.tenant.branch.models import Branch
        if not has_tenant_access(request.user, current_schema_name()) or not _may_manage(request.user):
            return None, _err('forbidden', 'Ключ выдаёт администратор сети.', status.HTTP_403_FORBIDDEN)
        branch = Branch.objects.filter(pk=pk).first()
        if branch is None:
            return None, _err('branch_not_found', 'Точка не найдена.', status.HTTP_404_NOT_FOUND)
        return branch, None

    def get(self, request, pk):
        branch, error = self._branch(request, pk)
        if error:
            return error
        return Response({'has_key': bool(branch.pos_plugin_key_hash), 'hint': branch.pos_plugin_key_hint or None,
                         'enabled': plugin_enabled()})

    def post(self, request, pk):
        branch, error = self._branch(request, pk)
        if error:
            return error
        key = new_key()
        branch.pos_plugin_key_hash = key_hash(key)
        branch.pos_plugin_key_hint = key[-4:]
        branch.save(update_fields=['pos_plugin_key_hash', 'pos_plugin_key_hint'])
        log.info('pos-plugin: новый ключ точки %s (…%s), выдал %s', branch.branch_id, key[-4:], request.user)
        return Response({'key': key, 'hint': key[-4:], 'enabled': plugin_enabled()})
