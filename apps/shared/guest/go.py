"""
Распределитель входа гостя: `GET /go/` на домене сети (`<schema>.levelupapp.ru/go/`).

Одна ссылка для QR, постов и рассылок. Сервер сам решает, где открыть приложение:
  • внутри приложения ВКонтакте — мини-апп ВК (`vk.com/app<ID>/#/…`), как раньше;
  • внутри Телеграма — Телеграм-мини-апп, если он настроен (`TELEGRAM_GUEST_APP_URL`),
    иначе веб-версия; веб — только при включённом у сети `web_entry_enabled`;
  • в обычном браузере — экран выбора «ВКонтакте / в браузере / Телеграм»; выбор
    запоминается в cookie на 180 дней, следующий заход идёт сразу.
Веб-вход у сети выключен — всегда мини-апп ВК (302), поведение прежнее.

Место, где гость остановился, переносится параметром `to` (экран мини-аппа из белого
списка) и обычными параметрами входа (`branch`, `table`, `src`, …) — во всех трёх
каналах гость попадает на тот же экран той же точки.

⚠️ Уже напечатанные QR ведут прямо на `vk.com/app…` и не меняются: распределитель —
для новых ссылок. Параметры проверяются по белому списку и шаблонам: в редирект не
попадает ничего, кроме цифр и коротких меток.
"""
from __future__ import annotations

import base64
import logging
import re
from urllib.parse import urlencode

from django.conf import settings
from django.db import connection
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

log = logging.getLogger(__name__)

CHANNEL_VK = 'vk'
CHANNEL_TELEGRAM = 'tg'
CHANNEL_BROWSER = 'browser'

TARGET_VK = 'vk'
TARGET_WEB = 'web'
TARGET_TG = 'tg'

PREF_COOKIE = 'lu_go_via'
PREF_MAX_AGE = 180 * 24 * 3600

# Экраны мини-аппа (vk-miniapp/src/routes.js). '' — главная.
ROUTES = ('', 'profile', 'transactions', 'inventory', 'catalog', 'quest', 'coupon', 'review')

_DIGITS = re.compile(r'^\d{1,12}$')
_LABEL = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
# Порядок = порядок в ссылке; src — последним, как в qr_links.build_qr_link.
PARAM_RULES = (
    ('branch', _DIGITS),
    ('table', _DIGITS),
    ('delivery', re.compile(r'^true$')),
    ('web', _LABEL),
    ('source', _LABEL),
    ('from', _LABEL),
    ('is_referral', _LABEL),
    ('birthday', _LABEL),
    ('src', _LABEL),
)

_VK_UA = re.compile(r'VKAndroidApp|VKiOSApp|vk_app|VKontakte/', re.I)
_TG_UA = re.compile(r'Telegram', re.I)


def detect_channel(user_agent: str) -> str:
    ua = user_agent or ''
    if _VK_UA.search(ua):
        return CHANNEL_VK
    if _TG_UA.search(ua):
        return CHANNEL_TELEGRAM
    return CHANNEL_BROWSER


def clean_params(query) -> tuple[str, list[tuple[str, str]]]:
    """(экран, [(ключ, значение)…]) — только то, что прошло белый список."""
    route = (query.get('to') or '').strip().strip('/')
    if route not in ROUTES:
        route = ''
    params = []
    for key, rule in PARAM_RULES:
        value = (query.get(key) or '').strip()
        if value and rule.match(value):
            params.append((key, value))
    return route, params


def _entry_query(company_id: str, params) -> str:
    return urlencode([('company', company_id), *params])


def vk_link(company_id: str, route: str, params) -> str:
    app_id = getattr(settings, 'VK_MINI_APP_ID', '')
    return f'https://vk.com/app{app_id}/#/{route}?{_entry_query(company_id, params)}'


def web_link(company_id: str, route: str, params) -> str:
    # Параметры в query: webEntryParams.js сам перенесёт их в хэш, экран — в пути хэша.
    base = (getattr(settings, 'GUEST_WEB_BASE_URL', '') or 'https://loyalupp.ru').rstrip('/')
    return f'{base}/?{_entry_query(company_id, params)}#/{route}'


def encode_start_param(company_id: str, route: str, params) -> str:
    """Телеграм `startapp`: base64url без '=' (разрешены только A-Za-z0-9_-, ≤ 512)."""
    query = _entry_query(company_id, params)
    if route:
        query += '&' + urlencode([('to', route)])
    return base64.urlsafe_b64encode(query.encode()).decode().rstrip('=')


def tg_link(company_id: str, route: str, params) -> str | None:
    base = (getattr(settings, 'TELEGRAM_GUEST_APP_URL', '') or '').strip()
    if not base:
        return None
    start = encode_start_param(company_id, route, params)
    if len(start) > 512:
        return None
    sep = '&' if '?' in base else '?'
    return f'{base}{sep}startapp={start}'


def decide(channel: str, *, web_enabled: bool, tg_available: bool,
           via: str = '', remembered: str = '') -> str | None:
    """
    Куда вести. None — показать экран выбора (обычный браузер, выбора ещё не было).
    """
    available = {TARGET_VK}
    if web_enabled:
        available.add(TARGET_WEB)
        if tg_available:
            available.add(TARGET_TG)
    if via in available:
        return via
    if channel == CHANNEL_VK:
        return TARGET_VK
    if channel == CHANNEL_TELEGRAM:
        if TARGET_TG in available:
            return TARGET_TG
        return TARGET_WEB if web_enabled else TARGET_VK
    if not web_enabled:
        return TARGET_VK
    if remembered in available:
        return remembered
    return None


def _company_and_config():
    company = getattr(connection, 'tenant', None)
    if company is None or getattr(company, 'schema_name', 'public') == 'public':
        raise Http404
    if not getattr(company, 'is_active', False):
        raise Http404
    paid_until = getattr(company, 'paid_until', None)
    if paid_until and paid_until < timezone.localdate():
        raise Http404
    return company, getattr(company, 'config', None)


@require_GET
def go(request):
    company, config = _company_and_config()
    company_id = str(company.client_id)
    route, params = clean_params(request.GET)
    channel = detect_channel(request.META.get('HTTP_USER_AGENT', ''))
    web_enabled = bool(getattr(config, 'web_entry_enabled', False))

    links = {
        TARGET_VK: vk_link(company_id, route, params),
        TARGET_WEB: web_link(company_id, route, params),
    }
    tg = tg_link(company_id, route, params)
    if tg:
        links[TARGET_TG] = tg

    via = (request.GET.get('via') or '').strip()
    target = decide(channel, web_enabled=web_enabled, tg_available=bool(tg),
                    via=via, remembered=request.COOKIES.get(PREF_COOKIE, ''))
    src = dict(params).get('src', '')
    log.info('go tenant=%s channel=%s target=%s route=%s src=%s',
             company.schema_name, channel, target or 'choose', route or '-', src or '-')

    if target is None:
        choices = [
            {'via': TARGET_VK, 'title': 'Открыть во ВКонтакте', 'primary': True},
            {'via': TARGET_WEB, 'title': 'Продолжить в браузере', 'primary': False},
        ]
        if tg:
            choices.append({'via': TARGET_TG, 'title': 'Открыть в Телеграме', 'primary': False})
        base_query = request.GET.copy()
        for item in choices:
            base_query['via'] = item['via']
            base_query['remember'] = '1'
            item['href'] = f'{request.path}?{base_query.urlencode()}'
        return render(request, 'go/choose.html', {
            'company_name': company.name,
            'choices': choices,
        })

    response = HttpResponseRedirect(links[target])
    if via and request.GET.get('remember') == '1' and target in (TARGET_VK, TARGET_WEB, TARGET_TG):
        response.set_cookie(PREF_COOKIE, target, max_age=PREF_MAX_AGE, secure=True,
                            httponly=True, samesite='Lax')
    return response
