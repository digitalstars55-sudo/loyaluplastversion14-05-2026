"""Кнопка меню бота гостей — на сеть этого гостя (17.10.2026).

Бот `@loyalup_bot` общий на все сети, и общая кнопка меню вела в одну сеть.
Когда гость входит в мини-апп сети, его личная кнопка меню
(`setChatMenuButton` с `chat_id`) начинает открывать именно эту сеть.
Вызов к Телеграму — в отдельном потоке: вход гостя его не ждёт.
"""
import logging
import threading

from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)


def адрес_сети(client_id) -> str:
    base = (getattr(settings, 'GUEST_WEB_BASE_URL', '') or 'https://vkapp.levelupapp.ru').rstrip('/')
    return f'{base}/?company={client_id}'


def _отправить(token, tg_user_id, url, name):
    import requests
    try:
        r = requests.post(f'https://api.telegram.org/bot{token}/setChatMenuButton', timeout=10, json={
            'chat_id': tg_user_id,
            'menu_button': {'type': 'web_app', 'text': (name or 'Открыть')[:32], 'web_app': {'url': url}},
        })
        if not r.json().get('ok'):
            logger.info('tg menu: %s не принял кнопку: %s', tg_user_id, r.text[:200])
    except Exception:  # noqa: BLE001 — кнопка меню не повод ронять вход
        logger.warning('tg menu: кнопка гостю %s не ушла', tg_user_id, exc_info=True)


def запомнить_сеть(tg_user_id) -> bool:
    token = (getattr(settings, 'TELEGRAM_GUEST_BOT_TOKEN', '') or '').strip()
    tenant = getattr(connection, 'tenant', None)
    client_id = getattr(tenant, 'client_id', None)
    if not (token and tg_user_id and client_id):
        return False
    threading.Thread(target=_отправить, daemon=True,
                     args=(token, int(tg_user_id), адрес_сети(client_id), getattr(tenant, 'name', ''))).start()
    return True
