"""
Expo Push helper. Шлёт push-уведомления через Expo Push API.
Один HTTP POST на batch. Без внешних зависимостей кроме requests (уже в стеке).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = 'https://exp.host/--/api/v2/push/send'


def send_expo_push(
    tokens: List[str],
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Отправить push на список Expo-токенов.

    tokens — список строк вида ExponentPushToken[xxx]
    Возвращает {'sent': N, 'response'?: dict, 'errors'?: list}.
    Не бросает — все ошибки залогирует и отдаст в errors.
    """
    if not tokens:
        return {'sent': 0, 'errors': []}

    messages = []
    for tok in tokens:
        if not tok or not isinstance(tok, str):
            continue
        if not (tok.startswith('ExponentPushToken[') or tok.startswith('ExpoPushToken[')):
            logger.warning('send_expo_push: skipping non-Expo token: %s', tok[:24])
            continue
        messages.append({
            'to':       tok,
            'title':    title,
            'body':     body,
            'data':     data or {},
            'priority': 'high',
            'sound':    'default',
        })

    if not messages:
        return {'sent': 0, 'errors': ['no_valid_tokens']}

    try:
        resp = requests.post(
            EXPO_PUSH_URL,
            json=messages,
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info('Expo push sent: %d messages → %s', len(messages), resp.status_code)
        return {'sent': len(messages), 'response': resp.json()}
    except Exception as e:
        logger.exception('Expo push send failed: %s', e)
        return {'sent': 0, 'errors': [str(e)]}
