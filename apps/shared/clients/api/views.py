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
            'feed': data.get('feed', []),
        })


class CrossTenantReviewsView(APIView):
    """
    GET /api/v1/overview/reviews/?period=30d&sentiment=all&page=1

    Все отзывы со всех клиентов за период с фильтром по типу + пагинацией.
    Только суперадмин.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        u = request.user
        if not (u.is_superuser or getattr(u, 'role', None) == 'superadmin'):
            return Response({'detail': 'Только для суперадмина.'}, status=status.HTTP_403_FORBIDDEN)

        from django.core.paginator import Paginator
        from apps.shared.clients.cross_stats import (
            get_cross_tenant_reviews, parse_overview_period, SENTIMENT_FILTERS,
        )
        start, end, active_period = parse_overview_period(request)
        sentiment = request.GET.get('sentiment', 'all')
        if sentiment not in dict(SENTIMENT_FILTERS):
            sentiment = 'all'
        reviews = get_cross_tenant_reviews(start, end, sentiment)
        paginator = Paginator(reviews, 30)
        page_obj = paginator.get_page(request.GET.get('page'))
        return Response({
            'period': active_period, 'sentiment': sentiment,
            'start': start.isoformat(), 'end': end.isoformat(),
            'total': paginator.count, 'page': page_obj.number, 'num_pages': paginator.num_pages,
            'results': [
                {**r, 'created_at': r['created_at'].isoformat()} for r in page_obj
            ],
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
