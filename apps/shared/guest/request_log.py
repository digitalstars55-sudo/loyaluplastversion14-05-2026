"""
Журнал гостевого API (15.10.2026, слово владельца «добавь запись»): до этого
в `docker logs web` попадали только запросы без подписи ВК, а обычные запросы
гостей, неудачный вход через VK ID и ошибки в браузере гостя не видел никто.

  • `GuestRequestLogMiddleware` — одна строка на каждый запрос к `/api/`:
    сеть, метод, путь, код, время, как опознан гость, откуда пришёл запрос.
    Без тела, токенов и подписей.
  • `ClientLogView` — `POST /api/v1/client-log/`: браузер гостя сообщает о
    своей ошибке (зависла загрузка, не обменялся код VK ID, сбой JS).
    Без входа; поля обрезаны; не чаще 30 сообщений в минуту с адреса.

Смотреть: `docker logs web 2>&1 | grep -E 'guest req|guest client|vk auth'`.
"""

import logging
import time

from django.core.cache import cache
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger('guest.requests')
client_logger = logging.getLogger('guest.client')

_SKIP = ('/api/v1/vk/callback',)   # вебхук ВК — шумный, к гостю отношения не имеет


def _short(value, n=120) -> str:
    return (str(value or '-').replace('\n', ' ').replace('\r', ' '))[:n]


def _ip(request) -> str:
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    return (fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR', '')) or '-'


class GuestRequestLogMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path or ''
        if not path.startswith('/api/') or path.startswith(_SKIP) or request.method == 'OPTIONS':
            return self.get_response(request)
        started = time.monotonic()
        response = None
        try:
            response = self.get_response(request)
            return response
        finally:
            try:
                tenant = getattr(getattr(request, 'tenant', None), 'schema_name', '-')
                logger.info(
                    'guest req %s %s %s %s %dms sign=%s web=%s tg=%s origin=%s ip=%s ua=%s',
                    tenant, request.method, path,
                    getattr(response, 'status_code', 500),
                    int((time.monotonic() - started) * 1000),
                    getattr(request, 'vk_sign_status', '-'),
                    'yes' if request.META.get('HTTP_X_WEB_SESSION') or request.META.get('HTTP_AUTHORIZATION') else 'no',
                    'yes' if request.META.get('HTTP_X_TELEGRAM_INIT_DATA') else 'no',
                    _short(request.META.get('HTTP_ORIGIN'), 60),
                    _ip(request),
                    _short(request.META.get('HTTP_USER_AGENT'), 90),
                )
            except Exception:
                pass


class ClientLogView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        key = f'guest-client-log:{_ip(request)}'
        try:
            count = cache.get_or_set(key, 0, 60)
            if count >= 30:
                return Response(status=status.HTTP_429_TOO_MANY_REQUESTS)
            cache.set(key, count + 1, 60)
        except Exception:
            pass
        data = request.data if isinstance(request.data, dict) else {}
        tenant = getattr(getattr(request, 'tenant', None), 'schema_name', '-')
        client_logger.warning(
            'guest client %s stage=%s msg=%s url=%s mode=%s extra=%s ip=%s ua=%s',
            tenant,
            _short(data.get('stage'), 40),
            _short(data.get('message'), 400),
            _short(data.get('url'), 200),
            _short(data.get('mode'), 20),
            _short(data.get('extra'), 400),
            _ip(request),
            _short(request.META.get('HTTP_USER_AGENT'), 120),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
