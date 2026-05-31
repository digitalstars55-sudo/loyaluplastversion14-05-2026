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


# Список типов push'ей, которые умеет слать бек. Используется на мобайле для
# отрисовки экрана «Настройки уведомлений». Добавляем новый тип — добавляй сюда.
PUSH_TYPES: list[tuple[str, str, str]] = [
    ('review_new',      'Новый отзыв',         'Гость оставил новый отзыв (APP или ВК).'),
    ('draft_ready',     'AI-черновик готов',   'Анализатор подготовил черновик ответа.'),
    ('chat_message',    'Чат поддержки',       'Сообщение от менеджера в саппорт-чате.'),
    ('daily_code',      'Коды дня',            'Утренняя сводка кодов дня.'),
    ('guest_birthday',  'ДР гостя',            'У гостя сегодня день рождения.'),
    ('broadcast_done',  'Рассылка отправлена', 'ВК-рассылка завершена.'),
    ('report_ready',    'Отчёт готов',         'Аналитический отчёт сгенерирован.'),
]
PUSH_TYPE_CODES = [t[0] for t in PUSH_TYPES]


def is_push_allowed(user, schema_name: str, push_type: str) -> bool:
    """
    Проверяет настройки пользователя: разрешён ли push указанного типа с указанного тенанта.

    Логика:
      - prefs.types[push_type] (если ключ есть) > True (default)
      - prefs.tenants[schema_name] (если ключ есть) > prefs.tenants["*"] (если есть) > True

    Все ОБА должны быть True. Пустой/отсутствующий push_prefs = всё включено.

    SU и обычные юзеры обрабатываются ОДИНАКОВО: prefs управляются самим юзером.
    """
    prefs = getattr(user, 'push_prefs', None) or {}
    if not isinstance(prefs, dict):
        return True

    types = prefs.get('types') or {}
    if push_type in types and types[push_type] is False:
        return False

    tenants = prefs.get('tenants') or {}
    if schema_name in tenants:
        return bool(tenants[schema_name])
    if '*' in tenants:
        return bool(tenants['*'])
    return True


def filter_users_by_prefs(users, schema_name: str, push_type: str) -> list:
    """Удобный helper для фильтрации списка User'ов по их push-prefs."""
    return [u for u in users if is_push_allowed(u, schema_name, push_type)]


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


def log_notification(users, ntype: str, title: str, body: str, data: Optional[Dict[str, Any]] = None) -> None:
    """
    Записать уведомление в историю (Notification) для каждого пользователя.
    Best-effort: ошибки не пробрасываются — логирование не должно ломать отправку.
    Вызывать в public schema (Notification живёт там же где User).
    """
    try:
        from django_tenants.utils import schema_context
        from apps.shared.users.models import Notification

        user_list = list(users)
        if not user_list:
            return
        with schema_context('public'):
            Notification.objects.bulk_create([
                Notification(
                    user=u,
                    type=ntype,
                    title=title or '',
                    body=body or '',
                    data=data or {},
                )
                for u in user_list
            ])
    except Exception as e:
        logger.warning('log_notification failed: %s', e)
