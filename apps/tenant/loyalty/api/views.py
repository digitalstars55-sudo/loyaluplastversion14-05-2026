"""
Сервис-API лояльности для ordering-BFF (приложение заказа LevOne / Шавуха).

Аутентификация — ТОЛЬКО сервисным ключом (ServiceKeyAuthentication,
`Authorization: Bearer <LOYALTY_SERVICE_API_KEY>`). Гостевые JWT/Session сюда
НЕ пускаются: классы аутентификации переопределены на уровне вью, поэтому
глобальная JWT-цепочка (которая пыталась бы декодировать ключ как JWT) не
участвует.

Все вью тонкие: парсинг → вызов services → JSON. Бизнес-логика и атомарность —
в `apps.tenant.loyalty.services`.
"""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.users.auth import ServiceKeyAuthentication

from .. import services
from ..services import LoyaltyError


def _error(exc: LoyaltyError) -> Response:
    body = {'detail': exc.detail, 'code': exc.code}
    body.update(exc.extra)
    return Response(body, status=exc.http_status)


def _int(value, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise services.InvalidAmount(f'{field}: ожидается целое число')


class _ServiceView(APIView):
    authentication_classes = [ServiceKeyAuthentication]
    permission_classes = [IsAuthenticated]


class BalanceView(_ServiceView):
    """GET /api/v1/loyalty/balance?vk_id=&branch_id="""

    def get(self, request):
        try:
            vk_id = _int(request.query_params.get('vk_id'), 'vk_id')
            client = services.get_client(vk_id)
            branch_id = request.query_params.get('branch_id')
            return Response({
                'vk_id': vk_id,
                'branch_id': _int(branch_id, 'branch_id') if branch_id else None,
                'balance': services.network_balance(client),
                'currency': 'points',
            })
        except LoyaltyError as e:
            return _error(e)


class AccrueView(_ServiceView):
    """POST /api/v1/loyalty/accrue"""

    def post(self, request):
        d = request.data
        try:
            result, _ = services.accrue(
                vk_id=_int(d.get('vk_id'), 'vk_id'),
                branch_id=_int(d.get('branch_id'), 'branch_id'),
                order_id=d.get('order_id'),
                order_total=_int(d.get('order_total'), 'order_total'),
                idempotency_key=d.get('idempotency_key') or f'accrue:{d.get("order_id")}',
                points=d.get('points'),
            )
            return Response(result)
        except LoyaltyError as e:
            return _error(e)


class RedeemView(_ServiceView):
    """POST /api/v1/loyalty/redeem"""

    def post(self, request):
        d = request.data
        try:
            result, _ = services.redeem(
                vk_id=_int(d.get('vk_id'), 'vk_id'),
                branch_id=_int(d.get('branch_id'), 'branch_id'),
                order_id=d.get('order_id'),
                amount=_int(d.get('amount'), 'amount'),
                idempotency_key=d.get('idempotency_key') or f'redeem:{d.get("order_id")}',
            )
            return Response(result)
        except LoyaltyError as e:
            return _error(e)


class RefundView(_ServiceView):
    """POST /api/v1/loyalty/refund"""

    def post(self, request):
        d = request.data
        try:
            result, _ = services.refund(
                vk_id=_int(d.get('vk_id'), 'vk_id'),
                branch_id=_int(d.get('branch_id'), 'branch_id'),
                order_id=d.get('order_id'),
                idempotency_key=d.get('idempotency_key') or f'refund:{d.get("order_id")}',
            )
            return Response(result)
        except LoyaltyError as e:
            return _error(e)


class SpendView(_ServiceView):
    """GET /api/v1/loyalty/spend?vk_id=&branch_id=&period_days=90"""

    def get(self, request):
        try:
            vk_id = _int(request.query_params.get('vk_id'), 'vk_id')
            period_raw = request.query_params.get('period_days') or 90
            result = services.spend_total(vk_id=vk_id, period_days=_int(period_raw, 'period_days'))
            branch_id = request.query_params.get('branch_id')
            result['branch_id'] = _int(branch_id, 'branch_id') if branch_id else None
            return Response(result)
        except LoyaltyError as e:
            return _error(e)
