"""
Скан QR фискального чека гостем — `receipts.py` (волна 2, 14.10.2026).

    POST /api/v1/client/receipt/   {vk_id, qr}   → баллы за чек
    GET  /api/v1/client/receipt/?vk_id=          → последние чеки гостя

Флаг сети `receipt_scan_enabled` выключен → 404, мини-приложение кнопку не
показывает. Засчитать чек может только доказанный гость (подпись запуска ВК
или веб-сессия): иначе чужой vk_id получил бы баллы за подобранную строку.
"""

import logging

from django.db import connection
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.shared.guest.models import Client

from ..receipts import ReceiptError, process_receipt
from .client_phone import _err, _proven_vk_id

log = logging.getLogger(__name__)


class ReceiptSerializer(serializers.Serializer):
    vk_id = serializers.IntegerField(min_value=1)
    qr = serializers.CharField(max_length=255)


def _config():
    tenant = getattr(connection, 'tenant', None)
    return getattr(tenant, 'config', None) if tenant is not None else None


def _guest(request, claimed):
    proven = _proven_vk_id(request)
    if not proven:
        return None, _err('vk_sign_required', 'Нужны параметры запуска мини-приложения.', status.HTTP_403_FORBIDDEN)
    if proven != claimed:
        return None, _err('vk_id_mismatch', 'vk_id не совпадает с параметрами запуска.', status.HTTP_403_FORBIDDEN)
    client = Client.objects.filter(vk_id=claimed, is_active=True).first()
    if client is None:
        return None, _err('client_not_found', 'Профиль гостя не найден.', status.HTTP_404_NOT_FOUND)
    return client, None


class ClientReceiptView(APIView):

    def post(self, request):
        cfg = _config()
        if not getattr(cfg, 'receipt_scan_enabled', False):
            return _err('feature_disabled', 'Не найдено.', status.HTTP_404_NOT_FOUND)
        s = ReceiptSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        client, error = _guest(request, int(s.validated_data['vk_id']))
        if error:
            return error
        try:
            result = process_receipt(client, s.validated_data['qr'], cfg)
        except ReceiptError as e:
            log.info('receipt: vk_id=%s отказ %s', client.vk_id, e.code)
            return _err(e.code, e.detail, e.http_status)
        log.info('receipt: vk_id=%s %s баллов=%s', client.vk_id, result['status'], result['coins'])
        return Response(result)

    def get(self, request):
        from ..models import ReceiptScan

        cfg = _config()
        if not getattr(cfg, 'receipt_scan_enabled', False):
            return _err('feature_disabled', 'Не найдено.', status.HTTP_404_NOT_FOUND)
        try:
            claimed = int(request.query_params.get('vk_id') or 0)
        except ValueError:
            claimed = 0
        client, error = _guest(request, claimed)
        if error:
            return error
        rows = ReceiptScan.objects.filter(client=client).select_related('branch')[:20]
        return Response({
            'cashback_percent': int(getattr(cfg, 'receipt_cashback_percent', 0) or 0),
            'results': [{
                'receipt_at': r.receipt_at.isoformat(), 'amount': str(r.amount), 'status': r.status,
                'coins': r.coins, 'branch': r.branch.name if r.branch else None,
            } for r in rows],
        })
