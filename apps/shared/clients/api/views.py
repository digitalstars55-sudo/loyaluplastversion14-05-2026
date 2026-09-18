from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from .serializers import TenantDomainResponseSerializer
from .services import CompanyExpired, CompanyInactive, CompanyNotFound, get_tenant_domain


def _is_platform(request) -> bool:
    """JWT из обмена CheckUp с признаком platform: true (контракт 3в.1)."""
    token = getattr(request, 'auth', None)
    if not isinstance(token, str) or not token:
        return False
    try:
        from apps.shared.users.auth import decode_token
        return decode_token(token).get('platform') is True
    except Exception:
        return False


def _may_see_platform(request) -> bool:
    u = request.user
    return bool(u.is_superuser or getattr(u, 'role', None) == 'superadmin' or _is_platform(request))


class CrossTenantOverviewView(APIView):
    """
    GET /api/v1/overview/stats/?period=30d   (или ?start=&end=)

    Сводная статистика по ВСЕМ подключённым клиентам за период — для мобильного
    приложения админа. Только суперадмин (кросс-тенантные данные).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        u = request.user
        is_super = _may_see_platform(request)
        if not is_super:
            return Response({'detail': 'Только для суперадмина.'}, status=status.HTTP_403_FORBIDDEN)

        from apps.shared.clients.cross_stats import (
            get_cross_tenant_overview, parse_overview_period, OVERVIEW_PERIODS,
        )
        start, end, active_period = parse_overview_period(request)
        # Расчёт идёт по всем схемам — кэш 5 минут на период (контракт 3в.3).
        from django.core.cache import cache
        cache_key = f'overview:stats:{start.isoformat()}:{end.isoformat()}'
        data = cache.get(cache_key)
        if data is None:
            data = get_cross_tenant_overview(start, end)
            cache.set(cache_key, data, 300)
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
        if not _may_see_platform(request):
            return Response({'detail': 'Только для суперадмина.'}, status=status.HTTP_403_FORBIDDEN)

        from django.core.paginator import Paginator
        from apps.shared.clients.cross_stats import (
            get_cross_tenant_reviews, parse_overview_period, SENTIMENT_FILTERS,
        )
        start, end, active_period = parse_overview_period(request)
        sentiment = request.GET.get('sentiment', 'all')
        if sentiment not in dict(SENTIMENT_FILTERS):
            sentiment = 'all'
        schema = (request.GET.get('schema') or '').strip().lower() or None   # одна сеть (3в.3)
        reviews = get_cross_tenant_reviews(start, end, sentiment, schema=schema)
        paginator = Paginator(reviews, 30)
        page_obj = paginator.get_page(request.GET.get('page'))
        return Response({
            'period': active_period, 'sentiment': sentiment, 'schema': schema,
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

    Плюс флаги входа компании (`ClientConfig`, оба default=False):
    `web_entry_enabled` (работать вне ВК через VK ID) и `degrade_enabled`
    («Продолжить в браузере» при сбое ВК) — фронту они нужны ещё до того,
    как он узнал домен тенанта.
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


# ── №34: экспорт сводной в CSV (контракт v1.8) ───────────────────────────────

EXPORT_COLUMNS = [
    ('name', 'Клиент'), ('schema', 'Сеть'), ('client_id', 'client_id'), ('domain', 'Домен'),
    ('total_scans', 'Сканы'), ('qr_scans', 'Сканы QR'), ('pos_guests', 'Гости POS'), ('scan_index', 'Индекс сканирования, %'),
    ('new_community', 'Новые в сообществе'), ('new_newsletter', 'Новые подписки на рассылку'),
    ('stories', 'Сториз'), ('reviews', 'Отзывы'),
    ('gift_cost', 'Себестоимость подарков, ₽'), ('service_cost', 'Обслуживание, ₽'), ('total_cost', 'Итого затраты, ₽'),
    ('sub_contacts', 'Контакты'), ('unique_digitized', 'Уникальных оцифровано'),
    ('cost_per_contact', 'Цена контакта, ₽'), ('cost_per_unique', 'Цена уникального, ₽'),
    ('no_cost_products', 'Подарков без себестоимости'), ('gift_breakdown_title', 'Подарки за период'),
]


def overview_rows_to_csv(rows: list[dict], totals: dict | None, start, end) -> str:
    """Те же строки и колонки, что у GET /overview/stats/; BOM для Excel, разделитель «;»."""
    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=';', lineterminator='\r\n')
    writer.writerow([f'Сводная по клиентам {start} — {end}'])
    writer.writerow([label for _, label in EXPORT_COLUMNS])
    for row in rows:
        writer.writerow([_csv_cell(row.get(key)) for key, _ in EXPORT_COLUMNS])
    if totals:
        writer.writerow(['Итого', '', '', ''] + [_csv_cell(totals.get(key, '')) for key, _ in EXPORT_COLUMNS[4:]])
    return '\ufeff' + buf.getvalue()


def _csv_cell(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'да' if value else 'нет'
    if isinstance(value, float):
        return f'{value:.2f}'.replace('.', ',')
    if isinstance(value, (list, dict)):
        return ''
    return str(value)


class CrossTenantOverviewExportView(APIView):
    """
    GET /api/v1/overview/export/?period=30d   (или ?start=&end=)

    CSV сводной по всем клиентам (те же строки и цифры, что /overview/stats/,
    из того же кэша на 5 минут). Только суперадмин или платформенный JWT.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request: Request):
        if not _may_see_platform(request):
            return Response({'code': 'role_not_allowed', 'detail': 'Только для платформенного доступа.'},
                            status=status.HTTP_403_FORBIDDEN)
        from django.core.cache import cache
        from django.http import HttpResponse
        from apps.shared.clients.cross_stats import get_cross_tenant_overview, parse_overview_period
        start, end, _period = parse_overview_period(request)
        cache_key = f'overview:stats:{start.isoformat()}:{end.isoformat()}'
        data = cache.get(cache_key)
        if data is None:
            data = get_cross_tenant_overview(start, end)
            cache.set(cache_key, data, 300)
        body = overview_rows_to_csv(data.get('rows', []), data.get('totals'), start.isoformat(), end.isoformat())
        resp = HttpResponse(body, content_type='text/csv; charset=utf-8')
        resp['Content-Disposition'] = f'attachment; filename="loyalup-overview-{start.isoformat()}-{end.isoformat()}.csv"'
        return resp
