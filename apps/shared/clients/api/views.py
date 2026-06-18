from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from .serializers import TenantDomainResponseSerializer
from .services import CompanyExpired, CompanyInactive, CompanyNotFound, get_tenant_domain


class CrossTenantOverviewView(APIView):
    """
    GET /api/v1/overview/stats/?period=30d   (или ?start=&end=)

    Сводная статистика по ВСЕМ подключённым клиентам за период — для мобильного
    приложения админа. Только суперадмин (кросс-тенантные данные).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        u = request.user
        is_super = u.is_superuser or getattr(u, 'role', None) == 'superadmin'
        if not is_super:
            return Response({'detail': 'Только для суперадмина.'}, status=status.HTTP_403_FORBIDDEN)

        from apps.shared.clients.cross_stats import (
            get_cross_tenant_overview, parse_overview_period, OVERVIEW_PERIODS,
        )
        start, end, active_period = parse_overview_period(request)
        data = get_cross_tenant_overview(start, end)
        return Response({
            'period': active_period,
            'start': start.isoformat(),
            'end': end.isoformat(),
            'period_choices': [{'code': c, 'label': l} for c, l in OVERVIEW_PERIODS],
            'client_count': data['client_count'],
            'totals': data['totals'],
            'rows': data['rows'],
        })


class TenantDomainView(APIView):
    """
    GET /api/company/<client_id>/

    Возвращает домен тенанта по публичному ID компании.
    Используется при первом открытии приложения гостем.
    """

    def get(self, request: Request, client_id: int) -> Response:
        try:
            data = get_tenant_domain(client_id)
        except CompanyNotFound:
            return Response(
                {'detail': 'Компания не найдена.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        except CompanyInactive:
            return Response(
                {'detail': 'Компания неактивна.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        except CompanyExpired:
            return Response(
                {'detail': 'Срок подписки компании истёк.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = TenantDomainResponseSerializer(data)
        return Response(serializer.data)
