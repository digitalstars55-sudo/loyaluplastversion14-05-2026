from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from django_tenants.utils import schema_context

from apps.shared.clients.models import Company
from .serializers import WebhookRequestSerializer, DeliverySerializer
from .services import BranchNotFound, register_delivery, verify_webhook_signature


class PublicDeliveryWebhook(APIView):
    """
    POST /api/v1/delivery/webhook/

    Публичный эндпоинт на корневом домене.
    Принимает вебхук от POS-системы (Dooglys / iiko), ищет тенанта
    по dooglys_branch_id / iiko_organization_id и регистрирует доставку.
    """

    def post(self, request: Request) -> Response:
        if not verify_webhook_signature(request):
            return Response(
                {'detail': 'Неверная подпись запроса.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        s = WebhookRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)

        from django_tenants.utils import get_public_schema_name
        for company in Company.objects.filter(is_active=True).exclude(schema_name=get_public_schema_name()):
            with schema_context(company.schema_name):
                try:
                    delivery, created = register_delivery(**s.validated_data)
                except BranchNotFound:
                    continue

                resp_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
                return Response(DeliverySerializer(delivery).data, status=resp_status)

        return Response(
            {'detail': 'Торговая точка не найдена.'},
            status=status.HTTP_404_NOT_FOUND,
        )
