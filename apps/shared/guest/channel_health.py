"""
Доступность площадок входа гостя «в моменте» (15.10.2026, решение владельца:
распределитель /go/ ведёт только туда, что сейчас работает).

Раз в минуту (celery beat, `check_guest_channels_task`) сервер проверяет:
  • `vk`   — мини-приложение ВК: страница, которую ВК открывает внутри себя
             (`VK_MINI_APP_HOST`, levonework.ru). 15.10 она пропала с 06:32 до 09:25
             (второй адрес сервера слетел), а /go продолжал слать гостей во ВК;
  • `web`  — веб-версия (`GUEST_WEB_BASE_URL`);
  • `vkid` — вход через VK ID отвечает на наш app_id (без него веб для нового гостя
             бесполезен);
  • `tg`   — Телеграм-мини-апп = та же веб-сборка; api.telegram.org с сервера
             закрыт, поэтому `tg` = настроен бот и жив `web`.

Площадка «лежит» только после двух неудач подряд (разовый сбой сети не в счёт).
Нет свежих данных (старше 5 минут, задача не шла) — считаем всё живым: проверка
не имеет права сама уронить вход.

Результат — в Redis (`VK_CALLBACK_CACHE_ALIAS`), общий для web и celery.
Смена состояния пишется в лог `guest.health` (WARNING): `channel DOWN vk …`.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
import urllib.request

from django.conf import settings
from django.core.cache import caches

log = logging.getLogger('guest.health')

KEY = 'guest:channel_health:v1'
STALE_AFTER = 300
FAILS_TO_DOWN = 2
TIMEOUT = 8
CHANNELS = ('vk', 'web', 'vkid', 'tg')

_UA = 'LoyalUP-ChannelHealth/1'
TITLES = {'vk': 'ВКонтакте (мини-приложение)', 'web': 'веб-версия', 'vkid': 'вход через VK ID',
          'tg': 'Телеграм'}


def _alert(title: str, body: str, channel: str, event: str) -> None:
    """Пуш суперадминам тем же путём, что мониторинг платформы. Сбой не роняет проверку."""
    try:
        from django_tenants.utils import schema_context

        from apps.shared.monitoring.alerts import _send_one, _superadmin_recipients
        with schema_context('public'):
            users, tokens = _superadmin_recipients()
            _send_one(users, tokens, title, body,
                      {'type': 'guest_channel', 'channel': channel, 'event': event})
    except Exception as e:  # noqa: BLE001
        log.warning('guest.health: тревога не ушла: %s', e)


def _cache():
    return caches[getattr(settings, 'VK_CALLBACK_CACHE_ALIAS', 'default')]


def _get(url: str) -> tuple[bool, str, str]:
    """(ok, итоговый адрес, ошибка). Тело читаем коротко."""
    req = urllib.request.Request(url, headers={'User-Agent': _UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read(4096).decode('utf-8', 'ignore')
            return 200 <= resp.status < 400, resp.geturl(), body
    except Exception as e:  # noqa: BLE001 — любая ошибка = площадка не ответила
        return False, '', str(e)[:200]


def _check_page(url: str) -> tuple[bool, str]:
    ok, _final, body = _get(url)
    if not ok:
        return False, body
    # Пустая заглушка хостинга вместо сборки — тоже «лежит».
    if '<div id="root"' not in body and 'assets/index-' not in body:
        return False, 'нет сборки мини-приложения на странице'
    return True, ''


def _check_vkid() -> tuple[bool, str]:
    app_id = getattr(settings, 'VK_WEB_APP_ID', None)
    if not app_id:
        return False, 'VK_WEB_APP_ID не задан'
    base = (getattr(settings, 'GUEST_WEB_BASE_URL', '') or '').rstrip('/') + '/'
    query = urllib.parse.urlencode({
        'response_type': 'code', 'client_id': app_id, 'redirect_uri': base,
        'state': 'healthcheck0000000000000000000000', 'scope': '',
        'code_challenge': 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM',
        'code_challenge_method': 'S256',
    })
    ok, final, err = _get(f'https://id.vk.ru/authorize?{query}')
    if not ok:
        return False, err
    # Живое приложение VK уводит на /auth; заблокированное/чужой адрес — нет.
    if '/auth?' not in final:
        return False, f'VK ID не принял приложение ({final[:80]})'
    return True, ''


def _probe_all() -> dict[str, tuple[bool, str, int]]:
    out = {}
    vk_host = getattr(settings, 'VK_MINI_APP_HOST', '') or 'https://levonework.ru/'
    web_host = getattr(settings, 'GUEST_WEB_BASE_URL', '') or 'https://loyalupp.ru'
    for name, fn in (
        ('vk', lambda: _check_page(vk_host)),
        ('web', lambda: _check_page(web_host.rstrip('/') + '/')),
        ('vkid', _check_vkid),
    ):
        started = time.monotonic()
        ok, err = fn()
        out[name] = (ok, err, int((time.monotonic() - started) * 1000))
    tg_configured = bool((getattr(settings, 'TELEGRAM_GUEST_APP_URL', '') or '').strip())
    web_ok = out['web'][0]
    out['tg'] = (tg_configured and web_ok,
                 '' if tg_configured and web_ok else ('бот не настроен' if not tg_configured else 'веб лежит'),
                 0)
    return out


def run_checks(now: float | None = None) -> dict:
    """Проверить площадки, обновить Redis, записать смены состояния. Возвращает состояние."""
    now = time.time() if now is None else now
    prev = _cache().get(KEY) or {}
    probes = _probe_all()
    state = {'checked_at': now, 'channels': {}}
    for name in CHANNELS:
        ok, err, ms = probes[name]
        before = (prev.get('channels') or {}).get(name) or {}
        fails = 0 if ok else int(before.get('fails', 0)) + 1
        up = ok or fails < FAILS_TO_DOWN
        was_up = before.get('up', True)
        since = before.get('since', now) if was_up == up else now
        state['channels'][name] = {'up': up, 'ok': ok, 'fails': fails, 'ms': ms,
                                   'error': err, 'since': since}
        if was_up and not up:
            log.warning('channel DOWN %s error=%s', name, err)
            _alert(f'🔴 LoyalUP: не работает вход «{TITLES[name]}»',
                   f'{err[:150]}. Распределитель /go ведёт гостей в другие площадки.', name, 'down')
        elif not was_up and up:
            minutes = int((now - before.get('since', now)) / 60)
            log.warning('channel UP %s after %dmin', name, minutes)
            _alert(f'✅ LoyalUP: вход «{TITLES[name]}» снова работает',
                   f'Лежал примерно {minutes} мин.', name, 'up')
    _cache().set(KEY, state, timeout=3600)
    return state


def get_state(now: float | None = None) -> dict | None:
    """Свежее состояние или None (нет данных / устарели)."""
    now = time.time() if now is None else now
    try:
        state = _cache().get(KEY)
    except Exception:
        return None
    if not state or now - float(state.get('checked_at', 0)) > STALE_AFTER:
        return None
    return state


def is_up(name: str, state: dict | None = None) -> bool:
    state = state if state is not None else get_state()
    if state is None:
        return True
    return bool(((state.get('channels') or {}).get(name) or {}).get('up', True))
