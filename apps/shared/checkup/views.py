"""
POST /api/v1/internal/auth/exchange/ — обмен токена CheckUp → LoyalUP.

Порядок проверок намеренно такой: сначала «откуда пришли» (внутренний адрес),
потом «включено ли» (503, чтобы CheckUp отличал выключенную ручку от неверного
секрета), потом секрет, потом тело. Ручка смонтирована на public-схеме
(main/public_urls.py) рядом с релеем жалоб и вызывается с Host: levelupapp.ru;
сеть передаётся в теле (tenant_schema), как и у релея.
"""
import json
import logging

from django.http import JsonResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

# Единое правило «внутренний запрос» с релеем жалоб: loopback ИЛИ приватный адрес
# (docker-мост 172.x), без X-Forwarded-For. Через публичный nginx сюда не попасть.
from apps.shared.relay.views import _is_internal_request

from .services import (
    ExchangeError, exchange_enabled, list_platform_tenants, perform_exchange, secret_matches,
)

log = logging.getLogger(__name__)

SECRET_HEADER = 'X-LoyalUP-Exchange-Secret'


@method_decorator(csrf_exempt, name='dispatch')
class TokenExchangeView(View):
    http_method_names = ['post']

    def post(self, request):
        if not _is_internal_request(request):
            log.warning('exchange: отклонён внешний запрос remote=%s xff=%s',
                        request.META.get('REMOTE_ADDR', ''), request.headers.get('X-Forwarded-For', ''))
            return JsonResponse({'code': 'forbidden', 'detail': 'только с внутреннего адреса сервера'}, status=403)

        if not exchange_enabled():
            return JsonResponse({'code': 'exchange_disabled',
                                 'detail': 'обмен токена выключен (CHECKUP_TOKEN_EXCHANGE_SECRET пуст)'}, status=503)

        if not secret_matches(request.headers.get(SECRET_HEADER, '')):
            log.warning('exchange: неверный секрет remote=%s', request.META.get('REMOTE_ADDR', ''))
            return JsonResponse({'code': 'bad_secret', 'detail': f'неверный {SECRET_HEADER}'}, status=401)

        try:
            data = json.loads(request.body or b'{}')
        except (ValueError, json.JSONDecodeError):
            return JsonResponse({'code': 'invalid_json', 'detail': 'тело не JSON'}, status=400)

        try:
            result = perform_exchange(data)
        except ExchangeError as e:
            log.info('exchange: отказ %s %s (%s)', e.status, e.code, e.detail)
            return JsonResponse(e.as_dict(), status=e.status)

        user = result['user']
        try:
            from apps.shared.audit.services import record_event
            record_event(
                action='login', request=request, actor=user,
                tenant_schema=result['tenant'].schema_name, tenant_name=result['tenant'].name,
                target='Вход через CheckUp (обмен токена)',
                meta={'via': 'checkup', 'checkup_user_id': result['identity'].checkup_user_id,
                      'role': user.role, 'created': result['created']},
            )
        except Exception:
            pass

        from apps.shared.users.api.serializers import ProfileSerializer
        profile = ProfileSerializer(user).data
        expires_at = result['expires_at']
        return JsonResponse({
            'token': result['token'],
            'expires_at': expires_at.isoformat(),
            'expires_in': max(0, int((expires_at - timezone.now()).total_seconds())),
            'tenant_schema': result['tenant'].schema_name,
            'tenant_domain': profile.get('tenant_domain'),
            'created': result['created'],
            # Пары «публичный branch_id → внутренний id» точек сотрудника;
            # null у network_admin = все точки сети (см. branches_payload).
            'branches': result['branches'],
            # Контракт 3в.1: платформенный доступ и «сеть ещё не переехала».
            'platform': result['platform'],
            'tenant_open': result['tenant_open'],
            'profile': profile,
        })


@method_decorator(csrf_exempt, name='dispatch')
class InternalTenantsView(View):
    """
    GET /api/v1/internal/tenants/ — живые сети платформы (контракт 3в.2).

    Те же три проверки, что у обмена: внутренний адрес → включено → секрет.
    Нужна переключателю сетей платформенного пользователя CheckUp; секретов
    интеграций и настроек не отдаёт.
    """
    http_method_names = ['get']

    def get(self, request):
        if not _is_internal_request(request):
            return JsonResponse({'code': 'forbidden', 'detail': 'только с внутреннего адреса сервера'}, status=403)
        if not exchange_enabled():
            return JsonResponse({'code': 'exchange_disabled',
                                 'detail': 'обмен токена выключен (CHECKUP_TOKEN_EXCHANGE_SECRET пуст)'}, status=503)
        if not secret_matches(request.headers.get(SECRET_HEADER, '')):
            return JsonResponse({'code': 'bad_secret', 'detail': f'неверный {SECRET_HEADER}'}, status=401)
        return JsonResponse({'tenants': list_platform_tenants()})
