from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from drf_spectacular.utils import extend_schema
from drf_spectacular.types import OpenApiTypes

from .serializers import (
    CodeActivationRequestSerializer,
    CodeResolveRequestSerializer,
    DeliverySerializer,
    WebhookRequestSerializer,
)
from .services import (
    AmbiguousDeliveryCode, BranchNotFound, ClientNotFound, DeliveryNotFound,
    activate_delivery, register_delivery, resolve_and_activate_network_delivery,
    verify_webhook_signature,
)


def _branch_brief(branch) -> dict:
    """branch_id + название + адрес (адрес живёт на BranchConfig)."""
    cfg = getattr(branch, 'config', None)
    return {
        'branch_id': branch.branch_id,
        'name':      branch.name,
        'address':   (getattr(cfg, 'address', '') or '').strip(),
    }


class DeliveryWebhook(APIView):
    """
    POST /api/v1/webhook/delivery/

    Receives a new delivery order from a POS system (iiko / Dooglys).
    Secured with DELIVERY_WEBHOOK_SECRET (X-Webhook-Secret header).
    Returns 201 when created, 200 when the code already exists (idempotent).
    """

    @extend_schema(request=WebhookRequestSerializer, responses={200: DeliverySerializer, 201: DeliverySerializer, 403: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        if not verify_webhook_signature(request):
            return Response(
                {'detail': 'Неверная подпись запроса.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        s = WebhookRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)

        try:
            delivery, created = register_delivery(**s.validated_data)
        except BranchNotFound:
            return Response(
                {'detail': 'Торговая точка не найдена.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        resp_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(DeliverySerializer(delivery).data, status=resp_status)


class DeliveryCodeView(APIView):
    """
    POST /api/v1/code/

    Guest enters the 5-digit short code to activate their delivery.
    Idempotent: re-submitting the same code by the same client returns 200.
    """

    @extend_schema(request=CodeActivationRequestSerializer, responses={200: DeliverySerializer, 404: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        s = CodeActivationRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)

        try:
            delivery = activate_delivery(**s.validated_data)
        except ClientNotFound:
            return Response(
                {'detail': 'Профиль гостя не найден.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        except DeliveryNotFound:
            return Response(
                {'detail': 'Код не найден или срок его действия истёк.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(DeliverySerializer(delivery).data)


class DeliveryCodeResolveView(APIView):
    """
    POST /api/v1/code/resolve/   body: {short_code, vk_id}

    Сетевая активация для тенант-QR «один на всю сеть»: точку определяем по коду
    (без выбора гостем), регистрируем гостя и активируем доставку. Возвращает
    точку (branch_id + название + адрес) + саму доставку — фронт ставит эту точку
    и запускает обычный поток игры.

    Старый пер-точечный POST /api/v1/code/ НЕ затрагивается.

    200 — активировано; 404 — код не найден; 409 — код у нескольких точек
    (коллизия), в теле список точек для выбора.
    """

    @extend_schema(request=CodeResolveRequestSerializer, responses={200: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT, 409: OpenApiTypes.OBJECT})
    def post(self, request: Request) -> Response:
        s = CodeResolveRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)

        try:
            branch, delivery = resolve_and_activate_network_delivery(**s.validated_data)
        except DeliveryNotFound:
            return Response(
                {'detail': 'Код не найден или срок его действия истёк.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        except AmbiguousDeliveryCode as exc:
            return Response(
                {
                    'detail': 'Такой код есть у нескольких точек — выберите вашу.',
                    'ambiguous': True,
                    'branches': [_branch_brief(b) for b in exc.branches],
                },
                status=status.HTTP_409_CONFLICT,
            )

        return Response({
            'branch': _branch_brief(branch),
            'delivery': DeliverySerializer(delivery).data,
        })
