"""
Нажатие цветной callback-кнопки под сообщением ВК (№78 «поделиться номером»).

У ссылочных кнопок ВК (open_link / open_app) цвета нет — цвет есть только у
кнопок типа `callback`. По нажатию ВК шлёт на наш callback-сервер событие
`message_event` с payload кнопки, а мы отвечаем `messages.sendMessageEventAnswer`
с `event_data = {type: open_link, link}` — клиент ВК открывает мини-апп.

Отвечать нужно быстро (гость ждёт под пальцем, ВК ждёт ≤ 1 мин), поэтому вызов
идёт прямо в запросе callback с коротким таймаутом. Любой сбой — только в лог:
событие всё равно подтверждается 'ok', иначе ВК посчитает сервер сломанным и
отключит callback у сообщества (инцидент 15.09).

Чужие payload (не наш маркер) молча игнорируются — у сообществ есть и другие
боты с callback-кнопками на том же Callback API.

Требование: у сообщества включено событие «message_event» в настройках
Callback API нашего сервера (`groups.setCallbackSettings … message_event=1`).
"""

import json
import logging

import requests

log = logging.getLogger(__name__)

PAYLOAD_KEY = 'lu'                    # маркер наших кнопок
PAYLOAD_PHONE_REQUEST = 'phone_request'
VK_API_VERSION = '5.131'
_ALLOWED_LINK_PREFIX = 'https://vk.com/app'


def build_payload(url: str) -> str:
    """payload кнопки «поделиться номером»: JSON-строка, ≤ 255 символов по правилам ВК."""
    return json.dumps({PAYLOAD_KEY: PAYLOAD_PHONE_REQUEST, 'url': url}, ensure_ascii=False)


def handle_message_event(config, obj: dict) -> bool:
    """
    Обработать `message_event`. True — ответ ВК отправлен (кнопка открыла ссылку).
    `config` — SenlerConfig группы с `vk_community_token`.
    """
    payload = obj.get('payload')
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = None
    if not isinstance(payload, dict) or payload.get(PAYLOAD_KEY) != PAYLOAD_PHONE_REQUEST:
        return False

    link = str(payload.get('url') or '')
    event_id, user_id, peer_id = obj.get('event_id'), obj.get('user_id'), obj.get('peer_id')
    if not link.startswith(_ALLOWED_LINK_PREFIX) or not (event_id and user_id and peer_id):
        log.warning('vk message_event: кнопка без ссылки/ид user=%s', user_id)
        return False

    token = getattr(config, 'vk_community_token', '') or ''
    if not token:
        log.warning('vk message_event: нет токена сообщества, user=%s', user_id)
        return False

    try:
        resp = requests.post(
            'https://api.vk.com/method/messages.sendMessageEventAnswer',
            data={
                'event_id': event_id,
                'user_id': user_id,
                'peer_id': peer_id,
                'event_data': json.dumps({'type': 'open_link', 'link': link}, ensure_ascii=False),
                'access_token': token,
                'v': VK_API_VERSION,
            },
            timeout=4,
        )
        data = resp.json()
    except Exception as e:  # noqa: BLE001 — сбой ответа не должен ронять callback
        log.warning('vk message_event: ответ не отправлен user=%s: %s', user_id, e)
        return False

    if 'error' in data:
        log.warning('vk message_event: VK API error user=%s: %s', user_id, data['error'].get('error_msg'))
        return False
    log.info('vk message_event: phone_request → open_link user=%s', user_id)
    return True
