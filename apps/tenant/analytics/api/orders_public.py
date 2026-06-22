"""
Публичный приём суточного количества заказов от POS-системы (Dooglys / iiko).

POS после закрытия смен (≈4:00) шлёт за предыдущие сутки числа по каждой точке
на POST /api/v1/orders/daily/ (корневой домен, public schema). Тенант/точка
определяются по cafe_id = Branch.dooglys_branch_id (как в вебхуке доставки),
с фолбэком на dooglys_sale_point_id (UUID).

Данные пишутся в DailyOrderStat (полная разбивка ТЗ) и зеркалятся в
POSGuestCache.guest_count = orders_total, чтобы индекс сканирований сразу считался
по пуш-данным без обращения к POS API.
"""

from __future__ import annotations

import hmac
import logging
import os

from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.clients.models import Company
from apps.tenant.delivery.api.services import BranchNotFound

logger = logging.getLogger(__name__)


def _verify_orders_secret(request) -> bool:
    """
    Проверяет заголовок X-Webhook-Secret против ORDERS_INGEST_SECRET (env).
    Если секрет не задан — пропускаем (как в вебхуке доставки, удобно для dev).
    Сравнение constant-time.
    """
    secret = os.getenv('ORDERS_INGEST_SECRET', '')
    if not secret:
        return True
    received = request.headers.get('X-Webhook-Secret', '')
    return hmac.compare_digest(received.encode('utf-8'), secret.encode('utf-8'))


class DailyOrdersItemSerializer(serializers.Serializer):
    """Одна точка за один день."""
    source = serializers.ChoiceField(choices=['dooglys', 'iiko'], default='dooglys')
    date = serializers.DateField()
    cafe_id = serializers.CharField()
    cafe_name = serializers.CharField(required=False, allow_blank=True, default='')
    orders_total = serializers.IntegerField(min_value=0, required=False, allow_null=True)
    orders_in_cafe = serializers.IntegerField(min_value=0, default=0)
    orders_pickup_admin = serializers.IntegerField(min_value=0, default=0)
    orders_delivery_admin = serializers.IntegerField(min_value=0, default=0)

    def validate(self, data):
        computed = (
            data['orders_in_cafe']
            + data['orders_pickup_admin']
            + data['orders_delivery_admin']
        )
        # orders_total не прислали (или null) → считаем как сумму трёх
        if data.get('orders_total') is None:
            data['orders_total'] = computed
        return data


def _store_for_current_schema(item: dict) -> bool:
    """
    Ищет Branch по cafe_id в ТЕКУЩЕЙ схеме и пишет статистику.
    Возвращает True, иначе кидает BranchNotFound.
    """
    from apps.tenant.analytics.models import DailyOrderStat, POSGuestCache
    from apps.tenant.branch.models import Branch

    cafe_id = str(item['cafe_id']).strip()
    branch = None
    if cafe_id.isdigit():
        branch = Branch.objects.filter(dooglys_branch_id=int(cafe_id)).first()
    if branch is None:
        branch = Branch.objects.filter(dooglys_sale_point_id=cafe_id).first()
    if branch is None:
        raise BranchNotFound(cafe_id)

    DailyOrderStat.objects.update_or_create(
        branch=branch,
        date=item['date'],
        defaults={
            'orders_total': item['orders_total'],
            'orders_in_cafe': item['orders_in_cafe'],
            'orders_pickup_admin': item['orders_pickup_admin'],
            'orders_delivery_admin': item['orders_delivery_admin'],
            'source': item['source'],
            'cafe_name_raw': item.get('cafe_name', '') or '',
        },
    )
    # Зеркалим total в знаменатель индекса сканирований.
    POSGuestCache.objects.update_or_create(
        branch=branch,
        date=item['date'],
        defaults={'guest_count': item['orders_total']},
    )
    return True


class PublicDailyOrdersIngest(APIView):
    """
    POST /api/v1/orders/daily/

    Тело: один объект, массив объектов, или {"points": [...]} — каждый элемент
    с полями cafe_id, date, orders_* (см. DailyOrdersItemSerializer).
    Точка определяется по cafe_id среди всех тенантов (как вебхук доставки).
    """

    def post(self, request: Request) -> Response:
        if not _verify_orders_secret(request):
            return Response(
                {'detail': 'Неверная подпись запроса.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        payload = request.data
        if isinstance(payload, list):
            raw_items = payload
        elif isinstance(payload, dict) and 'points' in payload:
            raw_items = payload['points']
        else:
            raw_items = [payload]

        ser = DailyOrdersItemSerializer(data=raw_items, many=True)
        ser.is_valid(raise_exception=True)

        public = get_public_schema_name()
        tenants = list(
            Company.objects.filter(is_active=True).exclude(schema_name=public)
        )

        stored, not_found = [], []
        for item in ser.validated_data:
            placed = False
            for company in tenants:
                with schema_context(company.schema_name):
                    try:
                        _store_for_current_schema(item)
                    except BranchNotFound:
                        continue
                stored.append({
                    'cafe_id': item['cafe_id'],
                    'date': str(item['date']),
                    'tenant': company.schema_name,
                    'orders_total': item['orders_total'],
                })
                placed = True
                break
            if not placed:
                not_found.append({'cafe_id': item['cafe_id'], 'date': str(item['date'])})

        resp_status = status.HTTP_200_OK if not not_found else status.HTTP_207_MULTI_STATUS
        return Response(
            {'stored': len(stored), 'not_found': not_found, 'details': stored},
            status=resp_status,
        )
