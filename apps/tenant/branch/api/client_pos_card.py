"""
Карта гостя для кассы (режим iikoCard «Карта гостя», волна 2, 14.10.2026).

    GET  /api/v1/client/pos-card/?vk_id=   → {card_number | null}
    POST /api/v1/client/pos-card/ {vk_id}  → выдать карту (создаёт/дополняет гостя в iikoCard)

Гость показывает номер QR-кодом, кассир сканирует его в iikoFront — заказ
ложится на карту, задача синхронизации забирает его и начисляет баллы
(`pos_cashback_percent`). Режим сети не «Карта гостя» → 404.
"""

import logging

from django.db import connection
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .client_phone import _err
from .client_receipt import _guest

log = logging.getLogger(__name__)


class PosCardSerializer(serializers.Serializer):
    vk_id = serializers.IntegerField(min_value=1)


def card_mode_config():
    from apps.shared.config.models import IikoCardMode
    tenant = getattr(connection, 'tenant', None)
    cfg = getattr(tenant, 'config', None) if tenant is not None else None
    if (cfg is None or cfg.iiko_card_mode != IikoCardMode.CARD
            or not (cfg.iiko_cloud_api_login and cfg.iiko_cloud_organization_id)):
        return None
    return cfg


def _payload(link, cfg):
    return {
        'card_number': link.card_number if link is not None and link.card_number else None,
        'cashback_percent': int(cfg.pos_cashback_percent or 0),
    }


class ClientPosCardView(APIView):

    def get(self, request):
        from apps.tenant.analytics.models import PosGuestLink

        cfg = card_mode_config()
        if cfg is None:
            return _err('feature_disabled', 'Не найдено.', status.HTTP_404_NOT_FOUND)
        try:
            claimed = int(request.query_params.get('vk_id') or 0)
        except ValueError:
            claimed = 0
        client, error = _guest(request, claimed)
        if error:
            return error
        return Response(_payload(PosGuestLink.objects.filter(client=client).first(), cfg))

    def post(self, request):
        from apps.tenant.analytics.iiko_card import IikoCloudClient, IikoCloudError, issue_card

        cfg = card_mode_config()
        if cfg is None:
            return _err('feature_disabled', 'Не найдено.', status.HTTP_404_NOT_FOUND)
        s = PosCardSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        client, error = _guest(request, int(s.validated_data['vk_id']))
        if error:
            return error
        try:
            link = issue_card(client, cfg, IikoCloudClient(cfg.iiko_cloud_api_login))
        except IikoCloudError as exc:
            log.warning('pos-card: vk_id=%s карта не выдана: %s', client.vk_id, exc)
            return _err('pos_unavailable', 'Касса сейчас не отвечает. Попробуйте через минуту.',
                        status.HTTP_503_SERVICE_UNAVAILABLE)
        log.info('pos-card: vk_id=%s карта %s', client.vk_id, link.card_number)
        return Response(_payload(link, cfg))
