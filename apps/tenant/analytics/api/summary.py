"""
Три JSON-ручки для экранов CheckUp (контракт платформы, ручки №2, №4, №5):

  GET /api/v1/analytics/reviews/summary/   — сводка репутации за период: тональности,
                                              рейтинг, источники, негатив без ответа, по точкам
  GET /api/v1/analytics/stats/detail/       — та же детализация метрики списком гостей, что
                                              веб-страница /analytics/stats/detail/
  GET /api/v1/dashboard/today/              — «задачи дня» одним запросом

Цифры считаются теми же запросами, что и веб-кабинет (analytics/views.py:
ReviewsAnalyticsView, StatsDetailView) — приёмка волны 1 требует совпадения
с вебом за тот же период и те же точки. Права по точкам — effective_branch_ids,
как у остальных ручек аналитики. Всё только чтение.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import connection
from django.db.models import Avg, Count, Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.users.access import current_schema_name, effective_branch_ids
from apps.tenant.branch.models import Branch, DailyCode, TestimonialConversation, TestimonialMessage

from . import services
from .serializers import StatsQuerySerializer

Sentiment = TestimonialConversation.Sentiment

# Тот же порядок и те же подписи, что на веб-странице «Анализ отзывов».
SENTIMENT_LABELS = (
    (Sentiment.POSITIVE, 'Позитивные'),
    (Sentiment.NEGATIVE, 'Негативные'),
    (Sentiment.PARTIALLY_NEGATIVE, 'Частично негативные'),
    (Sentiment.NEUTRAL, 'Нейтральные'),
    (Sentiment.SPAM, 'Спам'),
    (Sentiment.WAITING, 'Ожидают анализа'),
)
NEGATIVE_Q = Q(sentiment__in=(Sentiment.NEGATIVE, Sentiment.PARTIALLY_NEGATIVE))

TOP_BRANCHES = 5
TOP_BRANCHES_DAYS = 30

_QUERY_PARAMS = [
    OpenApiParameter('branch_ids', str, description='id точек через запятую (пересекается с правами)'),
    OpenApiParameter('period', str, enum=StatsQuerySerializer.PERIOD_CHOICES, description='today|7d|30d|90d|year|all (по умолчанию 30d)'),
    OpenApiParameter('start', str, description='YYYY-MM-DD (вместе с end перекрывает period)'),
    OpenApiParameter('end', str),
]


def _parse(request):
    """Общий разбор query: (validated_data | None, error_response | None, branch_ids)."""
    ser = StatsQuerySerializer(data=request.query_params)
    if not ser.is_valid():
        return None, Response(ser.errors, status=status.HTTP_400_BAD_REQUEST), None
    branch_ids = effective_branch_ids(request.user, current_schema_name(), ser.validated_data.get('branch_ids'))
    return ser.validated_data, None, branch_ids


def _conversations(branch_ids, start, end):
    """Треды периода — ровно как ReviewsAnalyticsView: по дате последнего сообщения, фильтр точек по PK."""
    qs = TestimonialConversation.objects.filter(
        last_message_at__date__gte=start, last_message_at__date__lte=end,
    )
    if branch_ids:
        qs = qs.filter(branch_id__in=branch_ids)
    return qs


def _avg_rating_by_branch(conv_qs) -> dict[int, float]:
    rows = (
        TestimonialMessage.objects
        .filter(conversation__in=conv_qs, source=TestimonialMessage.Source.APP,
                rating__isnull=False, conversation__branch__isnull=False)
        .values('conversation__branch_id').annotate(avg=Avg('rating'))
    )
    return {r['conversation__branch_id']: round(r['avg'], 1) for r in rows if r['avg'] is not None}


def _branch_rows(conv_qs, limit: int | None = None) -> list[dict]:
    rows = (
        conv_qs.filter(branch__isnull=False)
        .values('branch_id', 'branch__name', 'branch__branch_id')
        .annotate(
            total=Count('id'),
            negative=Count('id', filter=NEGATIVE_Q),
            unanswered_negative=Count('id', filter=NEGATIVE_Q & Q(is_replied=False)),
        )
        .order_by('-total', 'branch__name')
    )
    if limit:
        rows = rows[:limit]
    rows = list(rows)
    avg_map = _avg_rating_by_branch(conv_qs) if rows else {}
    return [{
        'branch_id': r['branch_id'],
        'public_branch_id': r['branch__branch_id'],
        'name': r['branch__name'],
        'total': r['total'],
        'negative': r['negative'],
        'unanswered_negative': r['unanswered_negative'],
        'avg_rating': avg_map.get(r['branch_id']),
    } for r in rows]


class ReviewsSummaryAPIView(APIView):
    """GET /api/v1/analytics/reviews/summary/?period=30d&branch_ids=1,2"""
    permission_classes = [IsAuthenticated]

    @extend_schema(parameters=_QUERY_PARAMS)
    def get(self, request):
        data, error, branch_ids = _parse(request)
        if error:
            return error
        start, end = data['start'], data['end']
        qs = _conversations(branch_ids, start, end)

        total = qs.count()
        sentiment_counts = dict(qs.values('sentiment').annotate(cnt=Count('id')).values_list('sentiment', 'cnt'))
        msg_qs = TestimonialMessage.objects.filter(
            conversation__in=qs, source=TestimonialMessage.Source.APP, rating__isnull=False,
        )
        avg_rating = msg_qs.aggregate(avg=Avg('rating'))['avg']
        rating_counts = dict(msg_qs.values('rating').annotate(cnt=Count('id')).values_list('rating', 'cnt'))
        source_counts = {
            row['source']: row['cnt']
            for row in TestimonialMessage.objects.filter(conversation__in=qs)
            .exclude(source=TestimonialMessage.Source.ADMIN_REPLY)
            .values('source').annotate(cnt=Count('conversation', distinct=True))
        }
        checkup_counts = dict(
            qs.exclude(checkup_status='').values('checkup_status').annotate(cnt=Count('id')).values_list('checkup_status', 'cnt')
        )

        return Response({
            'meta': {'start': str(start), 'end': str(end), 'branch_ids': branch_ids or []},
            'total': total,
            'avg_rating': round(avg_rating, 1) if avg_rating else None,
            'unanswered_negative': qs.filter(NEGATIVE_Q, is_replied=False).count(),
            'waiting_reply': qs.filter(has_unread=True, is_replied=False).count(),
            'sentiments': [
                {'key': key, 'label': label, 'count': sentiment_counts.get(key, 0)}
                for key, label in SENTIMENT_LABELS
            ],
            'ratings': [{'star': s, 'count': rating_counts.get(s, 0)} for s in range(5, 0, -1)],
            'sources': source_counts,
            'checkup': {k: checkup_counts.get(k, 0) for k in ('in_progress', 'resolved', 'rejected')},
            'vk_threads_without_branch': qs.filter(branch__isnull=True).count(),
            'by_branch': _branch_rows(qs),
        })


class StatsDetailAPIView(APIView):
    """
    GET /api/v1/analytics/stats/detail/?metric=qr_scans&period=30d&limit=50&offset=0

    Список гостей за метрикой общей статистики — те же гости, что на
    веб-странице /analytics/stats/detail/ (services.get_stat_clients).
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(parameters=_QUERY_PARAMS + [
        OpenApiParameter('metric', str, required=True, description='ключ метрики из GET /analytics/stats/ (см. metrics в ответе 400)'),
        OpenApiParameter('limit', int, description='1..200; без limit — до 200'),
        OpenApiParameter('offset', int),
    ])
    def get(self, request):
        from apps.tenant.analytics.views import _METRIC_LABELS
        from apps.tenant.mobile.api.review_filters import parse_pagination

        metric = str(request.query_params.get('metric') or '').strip()
        if metric not in _METRIC_LABELS:
            return Response({'detail': 'Неизвестная метрика', 'metrics': sorted(_METRIC_LABELS)},
                            status=status.HTTP_400_BAD_REQUEST)
        data, error, branch_ids = _parse(request)
        if error:
            return error
        start, end = data['start'], data['end']

        qs = services.get_stat_clients(metric, branch_ids, start, end)
        limit, offset = parse_pagination(request.query_params)
        limit = limit or 200   # список гостей без предела в JSON не отдаём
        total = qs.count()
        page = qs[offset:offset + limit]
        guests = []
        for cb in page:
            client = cb.client
            guests.append({
                'vk_id': client.vk_id,
                'first_name': client.first_name,
                'last_name': client.last_name,
                'photo_url': client.photo_url,
                'branch_id': cb.branch_id,
                'branch_name': cb.branch.name if cb.branch_id else '',
                'is_employee': bool(getattr(cb, 'is_employee', False)),
            })
        return Response({
            'metric': metric,
            'label': _METRIC_LABELS[metric],
            'meta': {'start': str(start), 'end': str(end), 'branch_ids': branch_ids or []},
            'total': total,
            'limit': limit,
            'offset': offset,
            'guests': guests,
        })


class DashboardTodayAPIView(APIView):
    """
    GET /api/v1/dashboard/today/?branch_ids=1,2

    «Задачи дня» для главного экрана: негатив без ответа, черновики ИИ,
    запланированные автоответы, коды дня по точкам, срок оплаты сети, топ точек
    за 30 дней. Один запрос вместо четырёх, которые собирал клиент.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(parameters=[_QUERY_PARAMS[0]])
    def get(self, request):
        from apps.tenant.mobile.api.views import _serialize_daily_code

        ser = StatsQuerySerializer(data={'branch_ids': request.query_params.get('branch_ids', ''), 'period': 'today'})
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        branch_ids = effective_branch_ids(request.user, current_schema_name(), ser.validated_data.get('branch_ids'))
        today = timezone.localdate()

        convs = TestimonialConversation.objects.all()
        if branch_ids:
            convs = convs.filter(branch_id__in=branch_ids)
        reviews = {
            'unanswered_negative': convs.filter(NEGATIVE_Q, is_replied=False).count(),
            'waiting_reply': convs.filter(has_unread=True, is_replied=False).count(),
            'drafts_ready': convs.filter(is_replied=False, ai_draft_rejected=False).exclude(ai_draft='').count(),
            'auto_send_scheduled': convs.filter(auto_send_status=TestimonialConversation.AutoSendStatus.SCHEDULED).count(),
            'new_today': convs.filter(last_message_at__date=today).count(),
            'checkup_in_progress': convs.filter(checkup_status='in_progress').count(),
        }

        branches = Branch.objects.filter(is_active=True)
        codes = DailyCode.objects.filter(valid_date=today, branch__is_active=True).select_related('branch')
        if branch_ids:
            branches = branches.filter(pk__in=branch_ids)
            codes = codes.filter(branch_id__in=branch_ids)
        codes = list(codes.order_by('branch__name', 'purpose'))
        with_code = {c.branch_id for c in codes}
        daily_codes = {
            'date': today.isoformat(),
            'branches_active': branches.count(),
            'branches_without_code': branches.exclude(pk__in=with_code).count(),
            'codes': [_serialize_daily_code(c) for c in codes],
        }

        tenant = getattr(connection, 'tenant', None)
        paid_until = getattr(tenant, 'paid_until', None)
        billing = {
            'paid_until': paid_until.isoformat() if paid_until else None,
            'days_left': (paid_until - today).days if paid_until else None,
            'is_active': bool(getattr(tenant, 'is_active', True)),
        }

        since = today - timedelta(days=TOP_BRANCHES_DAYS - 1)
        top = _branch_rows(_conversations(branch_ids, since, today), limit=TOP_BRANCHES)

        return Response({
            'date': today.isoformat(),
            'meta': {'branch_ids': branch_ids or []},
            'reviews': reviews,
            'daily_codes': daily_codes,
            'billing': billing,
            'top_branches': {'days': TOP_BRANCHES_DAYS, 'rows': top},
        })
