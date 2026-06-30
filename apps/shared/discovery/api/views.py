"""
Публичные эндпоинты сетевого входа из каталога VK (гость без QR).

Все — AllowAny (гость не авторизован), идентификация по vk_id.
Базовый путь: /api/v1/discovery/...
"""

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import services as svc


def _vk_id(data) -> int | None:
    raw = data.get('vk_id')
    try:
        v = int(raw)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


class DiscoveryCitiesView(APIView):
    """GET /api/v1/discovery/cities/?vk_id= — список городов-участников (+ событие claim_open)."""
    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        vk_id = _vk_id(request.GET)
        if vk_id:
            svc.record_claim_open(vk_id)
        return Response({'cities': svc.list_cities()})


class DiscoveryOpenView(APIView):
    """POST /api/v1/discovery/open/ {vk_id} — открыл экран сетевого входа."""
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        vk_id = _vk_id(request.data)
        if not vk_id:
            return Response({'detail': 'vk_id обязателен.'}, status=status.HTTP_400_BAD_REQUEST)
        svc.record_open(vk_id)
        return Response({'ok': True})


class DiscoveryPlayView(APIView):
    """POST /api/v1/discovery/play/ {vk_id} — крутанул колесо (приз фиксированный)."""
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        vk_id = _vk_id(request.data)
        if not vk_id:
            return Response({'detail': 'vk_id обязателен.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(svc.play(vk_id))


class DiscoveryClaimView(APIView):
    """POST /api/v1/discovery/claim/ {vk_id, client_id, first_name?, last_name?, photo_url?}."""
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        vk_id = _vk_id(request.data)
        try:
            client_id = int(request.data.get('client_id'))
        except (TypeError, ValueError):
            client_id = None
        if not vk_id or not client_id:
            return Response({'detail': 'vk_id и client_id обязательны.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            data = svc.claim(
                vk_id, client_id,
                first_name=request.data.get('first_name', '') or '',
                last_name=request.data.get('last_name', '') or '',
                photo_url=request.data.get('photo_url', '') or '',
            )
        except svc.AlreadyClaimed:
            # Уже выбирал город — отдаём текущий статус (идемпотентно для фронта).
            return Response({**svc.status(vk_id), 'already_claimed': True},
                            status=status.HTTP_409_CONFLICT)
        except svc.CityNotAvailable:
            return Response({'detail': 'Этот город сейчас недоступен.'},
                            status=status.HTTP_404_NOT_FOUND)
        except svc.NoWelcomeGift:
            return Response({'detail': 'В этом городе пока нет приветственного подарка.'},
                            status=status.HTTP_409_CONFLICT)
        return Response(data)


class DiscoveryStatusView(APIView):
    """GET /api/v1/discovery/status/?vk_id= — текущее состояние приза гостя."""
    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        vk_id = _vk_id(request.GET)
        if not vk_id:
            return Response({'detail': 'vk_id обязателен.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(svc.status(vk_id))


class DiscoveryActivateView(APIView):
    """POST /api/v1/discovery/activate/ {vk_id, code} — активация на кассе по коду дня."""
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        vk_id = _vk_id(request.data)
        if not vk_id:
            return Response({'detail': 'vk_id обязателен.'}, status=status.HTTP_400_BAD_REQUEST)
        code = (request.data.get('code') or '').strip()
        try:
            data = svc.activate(vk_id, code)
        except svc.NotClaimed:
            return Response({'detail': 'Сначала выберите город.'}, status=status.HTTP_409_CONFLICT)
        except svc.ActivationDenied as denied:
            return Response(
                {'detail': 'Нужен код дня.', 'reason': denied.reason,
                 'instruction_text': denied.instruction_text, 'needs_code': True},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(data)
