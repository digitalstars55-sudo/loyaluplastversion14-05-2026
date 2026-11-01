"""Wallet. Гостю: ссылки на карту. Телефону: веб-служба Apple PassKit (адреса по её спецификации)."""
from datetime import datetime, timezone as dt_tz

from django.conf import settings
from django.http import HttpResponse
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import wallet
from ..models import WalletDevice, WalletPass
from .client_phone import _err
from .client_receipt import _guest


class ClientWalletView(APIView):
    """GET /client/wallet/?vk_id= → ссылки. Только сам гость: подпись запуска проверяет `_guest`."""

    def get(self, request):
        if not wallet.включён():
            return _err('feature_disabled', 'Не найдено.', status.HTTP_404_NOT_FOUND)
        try:
            заявлен = int(request.query_params.get('vk_id') or 0)
        except ValueError:
            заявлен = 0
        client, error = _guest(request, заявлен)
        if error:
            return error
        wp = wallet.карта_гостя(client, request.get_host())
        return Response({
            'apple_url': f'https://{wp.host}/api/v1/wallet/pass/{wp.serial}/?t={wp.auth_token}' if wallet.apple_готов() else '',
            'google_url': wallet.ссылка_google(client, wp),
        })


class _ОткрытыйView(APIView):
    """Телефон и браузер ходят сюда без входа: доступ даёт ключ карты."""
    permission_classes = [AllowAny]
    authentication_classes = []


def _pkpass(wp) -> HttpResponse:
    ответ = HttpResponse(wallet.собрать_pkpass(wp.client, wp), content_type='application/vnd.apple.pkpass')
    ответ['Content-Disposition'] = 'attachment; filename="card.pkpass"'
    ответ['Last-Modified'] = wp.changed_at.strftime('%a, %d %b %Y %H:%M:%S GMT')
    return ответ


class СкачатьКартуView(_ОткрытыйView):
    def get(self, request, serial):
        wp = WalletPass.objects.select_related('client').filter(serial=serial, auth_token=request.query_params.get('t') or '-').first()
        if wp is None or not wallet.apple_готов():
            return Response(status=404)
        return _pkpass(wp)


def _карта_по_ключу(request, pass_type, serial):
    if pass_type != getattr(settings, 'WALLET_PASS_TYPE_ID', ''):
        return None
    заголовок = request.headers.get('Authorization') or ''
    if not заголовок.startswith('ApplePass '):
        return None
    return WalletPass.objects.select_related('client').filter(serial=serial, auth_token=заголовок[len('ApplePass '):]).first()


class РегистрацияView(_ОткрытыйView):
    def post(self, request, device, pass_type, serial):
        wp = _карта_по_ключу(request, pass_type, serial)
        if wp is None:
            return Response(status=401)
        токен = str(request.data.get('pushToken') or '')[:256]
        if not токен:
            return Response(status=400)
        _, новая = WalletDevice.objects.update_or_create(wallet_pass=wp, device_id=device[:128], defaults={'push_token': токен})
        return Response(status=201 if новая else 200)

    def delete(self, request, device, pass_type, serial):
        wp = _карта_по_ключу(request, pass_type, serial)
        if wp is None:
            return Response(status=401)
        WalletDevice.objects.filter(wallet_pass=wp, device_id=device).delete()
        return Response(status=200)


class ИзменившиесяView(_ОткрытыйView):
    def get(self, request, device, pass_type):
        if pass_type != getattr(settings, 'WALLET_PASS_TYPE_ID', ''):
            return Response(status=404)
        qs = WalletPass.objects.filter(devices__device_id=device)
        с = request.query_params.get('passesUpdatedSince')
        if с:
            try:
                qs = qs.filter(changed_at__gt=datetime.fromtimestamp(float(с), dt_tz.utc))
            except (TypeError, ValueError, OSError):
                pass
        строки = list(qs.values_list('serial', 'changed_at'))
        if not строки:
            return Response(status=204)
        return Response({'serialNumbers': [s for s, _ in строки], 'lastUpdated': str(max(t for _, t in строки).timestamp())})


class КартаДляТелефонаView(_ОткрытыйView):
    def get(self, request, pass_type, serial):
        wp = _карта_по_ключу(request, pass_type, serial)
        if wp is None:
            return Response(status=401)
        return _pkpass(wp)


class ЖурналView(_ОткрытыйView):
    def post(self, request):
        return Response(status=200)
