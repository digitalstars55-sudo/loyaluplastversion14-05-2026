"""
AI-чат для онбординга. Anthropic Claude Haiku + tool-use для атомарной
записи полей Lead.

Дизайн:
- Системный prompt задаёт сценарий (что собирать, в каком порядке).
- KnowledgeBase подмешивается, чтобы Claude мог отвечать на «что такое
  RF-аналитика» если клиент спросит, не уходя из роли.
- Один tool `update_lead` — Claude вызывает его, чтобы сохранить
  только что полученное поле. Tool-result возвращается в Claude
  в следующем turn'е.
- Conversation history хранится в Lead.conversation_history. Каждый
  turn передаётся в Claude в полном объёме (max_tokens ограничено).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Tuple

from django.conf import settings

from .models import Lead

logger = logging.getLogger(__name__)

# Поля, которые Claude может заполнить через tool. Имена 1:1 совпадают
# с полями модели Lead.
LEAD_FIELDS = (
    'cafe_name',
    'cafe_count',
    'traffic_estimate',
    'package_suggested',
    'full_name',
    'email',
    'vk_token',
    'domain_slug',
)

UPDATE_LEAD_TOOL = {
    'name': 'update_lead',
    'description': (
        'Сохранить поле заявки клиента в базу данных. '
        'Вызывай этот tool каждый раз, когда клиент даёт новую информацию '
        '(название кафе, количество точек, ФИО, email, VK-токен и т.д.). '
        'Можно сохранить несколько полей за один вызов.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'cafe_name':         {'type': 'string', 'description': 'Название кафе'},
            'cafe_count':        {'type': 'integer', 'description': 'Количество точек, целое число от 1 до 999'},
            'traffic_estimate':  {'type': 'string', 'description': 'Описание трафика своими словами от клиента'},
            'package_suggested': {'type': 'string', 'enum': ['Start', 'Business', 'Network'], 'description': 'Подходящий пакет — выбери на основе количества точек: Start=1, Business=2-4, Network=5+'},
            'full_name':         {'type': 'string', 'description': 'ФИО клиента'},
            'email':             {'type': 'string', 'description': 'Email клиента'},
            'vk_token':          {'type': 'string', 'description': 'VK API token (длинная строка)'},
            'domain_slug':       {'type': 'string', 'description': 'Латинский поддомен — генерируется автоматически из cafe_name'},
        },
        'required': [],
    },
}


def _build_system_prompt(lead: Lead) -> str:
    """Формирует system prompt с актуальным состоянием лида + KB."""
    state_lines = []
    for f in LEAD_FIELDS:
        val = getattr(lead, f, None)
        state_lines.append(f'  - {f}: {val if val else "—"}')
    state_block = '\n'.join(state_lines)

    # Подмешиваем KnowledgeBase (если есть). Безопасно — без crash при ошибках.
    kb_block = ''
    try:
        from apps.tenant.analytics.models import KnowledgeBaseDocument
        from django_tenants.utils import schema_context, get_public_schema_name
        # KB на public схеме? Скорее всего на tenant. Берём только публичную KB
        # (если её там нет — пусто, не падаем).
        with schema_context(get_public_schema_name()):
            docs = list(
                KnowledgeBaseDocument.objects.filter(is_active=True)
                .exclude(extracted_text='')[:3]
            )
        if docs:
            kb_block = '\n\nБАЗА ЗНАНИЙ ЛоялUP:\n' + '\n\n'.join(
                f'=== {d.title} ===\n{d.extracted_text[:1500]}' for d in docs
            )
    except Exception as e:
        logger.debug('AI onboarding: KnowledgeBase недоступна (это нормально): %s', e)

    return f"""Ты — AI-менеджер по продажам ЛоялUP. Веди живой, дружелюбный диалог с потенциальным клиентом и собирай данные для заявки.

ЛОЯЛUP — программа лояльности для кафе и баров. Включает:
• RF-аналитику гостей (12 сегментов: чемпионы → в риске → спящие)
• Умные рассылки в VK с AI-черновиками + A/B тест
• Управление отзывами (AI ловит негатив, готовит ответы)
• Каталог подарков и квесты для гостей
• Дневные коды для мини-игры

ПАКЕТЫ:
• Start (1 точка) — базовая аналитика + рассылки
• Business (2–4 точки) — все фичи кроме персонального менеджера
• Network (5+ точек) — всё включая персонального менеджера

СБОР ДАННЫХ — строго по порядку, ОДИН вопрос за реплику:
1. Название кафе → tool update_lead{{cafe_name}}
2. Количество точек (целое число) → tool update_lead{{cafe_count}}
3. Трафик одной точки в день своими словами → tool update_lead{{traffic_estimate}}
4. Сразу после трафика — предложи подходящий пакет (по cafe_count) и сохрани → tool update_lead{{package_suggested}}
5. ФИО → tool update_lead{{full_name}}
6. Объясни про VK API token подробно, КАК ЕГО НАЙТИ:
   • vk.com → Управление сообществом → Настройки → Работа с API
   • Создать ключ доступа сообщества
   • Дать права: «Сообщения сообщества» + «Управление»
   • Скопировать длинный ключ
7. Получи vk_token → tool update_lead{{vk_token}}
8. Email → tool update_lead{{email}}
9. Когда все поля заполнены — подведи краткий итог и скажи: «нажмите кнопку Отправить заявку».

ВАЖНЫЕ ПРАВИЛА:
• Используй tool update_lead КАЖДЫЙ раз, когда получаешь новое поле.
• ОДИН вопрос за реплику. Не спрашивай два за раз.
• Будь кратким (2–4 предложения). Без воды.
• Если клиент спрашивает не по теме — кратко ответь и верни к сбору данных.
• Если клиент даёт невалидные данные (не число, не email) — переспроси кратко.
• Тон: дружелюбный, на «вы», деловой.

ТЕКУЩЕЕ СОСТОЯНИЕ ЗАЯВКИ:
{state_block}
{kb_block}"""


def _convert_history_to_messages(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Конвертирует Lead.conversation_history в формат Anthropic messages.
    История содержит только {role, text} — без tool-events. Это OK,
    потому что мы шлём только сами тексты, а tool-call'ы Claude формирует
    заново на каждом запросе.
    """
    out: List[Dict[str, Any]] = []
    for msg in history:
        role = msg.get('role')
        text = msg.get('text', '')
        if role not in ('user', 'assistant') or not text.strip():
            continue
        out.append({'role': role, 'content': text})
    # Anthropic требует чтобы первое сообщение было от user.
    # Если история пустая или начинается с assistant — добавляем затравочное user.
    if not out or out[0]['role'] != 'user':
        out.insert(0, {'role': 'user', 'content': 'Здравствуйте.'})
    return out


def _apply_tool_input(lead: Lead, tool_input: Dict[str, Any]) -> List[str]:
    """
    Применить поля из tool-call к лиду. Возвращает список реально обновлённых
    полей (для логирования / debug).
    """
    updated: List[str] = []
    for key, value in (tool_input or {}).items():
        if key not in LEAD_FIELDS:
            continue
        if value in (None, ''):
            continue
        # Безопасное приведение типов
        if key == 'cafe_count':
            try:
                value = int(value)
                if value < 1 or value > 999:
                    continue
            except (TypeError, ValueError):
                continue
        if key == 'email':
            if '@' not in str(value) or '.' not in str(value):
                continue
        # Применяем
        setattr(lead, key, value)
        updated.append(key)
    if updated:
        # Также автоматически генерим domain_slug из cafe_name, если ещё нет
        if 'cafe_name' in updated and not lead.domain_slug:
            lead.domain_slug = _slugify(lead.cafe_name)
            updated.append('domain_slug')
        lead.save(update_fields=[*updated, 'updated_at'])
    return updated


def _slugify(s: str) -> str:
    """Простой ASCII-slug для домена. Не идеален, но работает для большинства случаев."""
    import re
    # Транслитерация базовых русских букв
    table = str.maketrans({
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'i', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '',
        'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    })
    s = s.lower().translate(table)
    s = re.sub(r'[^a-z0-9]+', '-', s)
    s = re.sub(r'^-+|-+$', '', s)
    return s[:40] or 'cafe'


def run_chat_turn(lead: Lead, user_message: str) -> Tuple[str, List[str]]:
    """
    Один turn чата: получили реплику от клиента, идём в Claude, получаем ответ,
    применяем tool-вызовы, возвращаем (assistant_text, updated_fields).

    Бросает исключение если ANTHROPIC_API_KEY не настроен или Claude недоступен.
    Caller (view) ловит и решает что делать (мобайл получит 503 → fallback на скрипт).
    """
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', None) or os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        raise RuntimeError('ANTHROPIC_API_KEY не настроен в окружении')

    import anthropic

    # Собираем историю
    history = list(lead.conversation_history or [])
    if user_message:
        history.append({'role': 'user', 'text': user_message})

    proxy_url = os.getenv('AI_PROXY_URL', '')
    client_kwargs: Dict[str, Any] = {'api_key': api_key}
    if proxy_url:
        client_kwargs['base_url'] = proxy_url
    client = anthropic.Anthropic(**client_kwargs)

    system_prompt = _build_system_prompt(lead)
    messages = _convert_history_to_messages(history)

    # Цикл: Claude может вызвать tool, мы возвращаем результат, Claude
    # может ответить ещё раз. Лимит — 3 итерации (защита от бесконечных циклов).
    assistant_text = ''
    updated_fields: List[str] = []

    for _ in range(3):
        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=512,
            system=system_prompt,
            tools=[UPDATE_LEAD_TOOL],
            messages=messages,
        )

        # Собираем текст и обрабатываем tool-вызовы
        tool_calls: List[Tuple[str, Dict[str, Any]]] = []
        for block in response.content:
            block_type = getattr(block, 'type', None)
            if block_type == 'text':
                assistant_text = (assistant_text + '\n\n' + block.text).strip() if assistant_text else block.text.strip()
            elif block_type == 'tool_use':
                tool_calls.append((block.id, dict(block.input or {})))

        # Применяем tool-вызовы
        if tool_calls:
            # Сохраняем assistant turn в messages с tool_use blocks
            messages.append({'role': 'assistant', 'content': response.content})
            tool_results = []
            for tool_id, tool_input in tool_calls:
                applied = _apply_tool_input(lead, tool_input)
                updated_fields.extend(applied)
                tool_results.append({
                    'type': 'tool_result',
                    'tool_use_id': tool_id,
                    'content': f'OK, сохранено: {", ".join(applied) if applied else "ничего нового"}',
                })
            messages.append({'role': 'user', 'content': tool_results})
            # Stop_reason должен быть tool_use → продолжаем цикл, чтобы Claude дал текстовый ответ
            if response.stop_reason == 'tool_use':
                continue

        # Если нет tool-вызовов — Claude дал финальный ответ, выходим
        break

    return assistant_text or 'Ок, продолжайте.', list(dict.fromkeys(updated_fields))
