"""
Генератор постов через Claude (Anthropic API).

Тот же паттерн доступа, что в analytics/ai_service.py: ключ из
settings.ANTHROPIC_API_KEY, опциональный прокси AI_PROXY_URL. Модель
задаётся MARKETER_AI_MODEL (дефолт — claude-sonnet-5: посты наружу,
haiku тут экономить не стоит).

Жёсткое правило промпта: ТОЛЬКО факты из Knowledge Core и заметок
владельца. Никаких выдуманных цен, акций, адресов, дат.
"""
from __future__ import annotations

import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'claude-sonnet-5'

_SYSTEM_PROMPT = """Ты — SMM-маркетолог кафе/ресторана. Пишешь посты для стены VK-сообщества на русском языке.

ЖЕЛЕЗНЫЕ ПРАВИЛА:
1. Используй ТОЛЬКО факты из переданных данных (JSON) и заметок владельца. НИЧЕГО не выдумывай: ни цен, ни акций, ни блюд, ни адресов, ни дат, которых нет в данных.
2. Не называй имён гостей. Цитаты отзывов можно приводить дословно или сокращённо, без имён.
3. Внутренние коды (R3F1 и т.п.) и служебные термины (RF-сегмент, тенант) НИКОГДА не попадают в пост — переводи в человеческий язык или опускай.
4. Длина поста: 350–800 символов. Эмодзи умеренно (3–6 на пост). Хэштеги: максимум 2 или ни одного.
5. Если в данных есть mini_app_link — закончи мягким призывом заглянуть в приложение за подарками, со ссылкой.
6. Цифры недели используй выборочно — 2–3 самые живые, не сваливай всю статистику.
7. Тон — по заметке «Тон бренда», если она есть; иначе тепло и по-соседски, без канцелярита и без агрессивных продаж.

Формат ответа — строго JSON без markdown:
{"text": "готовый текст поста"}

Никакого другого текста — только JSON."""

_DIGEST_TASK = (
    'Напиши еженедельный пост-дайджест «Что нового?» для подписчиков сообщества: '
    'чем жила сеть на этой неделе — гости, визиты, подаренные подарки, добрые '
    'отзывы. Пост должен радовать постоянных гостей и вызывать желание зайти.'
)


def generate_post(knowledge: dict, *, task: str = _DIGEST_TASK,
                  brand_voice: str = '', extra_facts: str = '') -> tuple[str, str]:
    """
    Генерирует текст поста.

    Returns:
        (text, model_used)
    Raises:
        RuntimeError — нет ключа API или ИИ вернул нечитаемый ответ.
    """
    api_key = getattr(settings, 'ANTHROPIC_API_KEY', None)
    if not api_key:
        raise RuntimeError('ANTHROPIC_API_KEY не задан в settings.')

    import os
    import anthropic

    proxy_url = os.getenv('AI_PROXY_URL', '')
    client = (
        anthropic.Anthropic(api_key=api_key, base_url=proxy_url)
        if proxy_url else anthropic.Anthropic(api_key=api_key)
    )
    model = getattr(settings, 'MARKETER_AI_MODEL', DEFAULT_MODEL)

    parts = [f'ЗАДАЧА: {task}']
    if brand_voice.strip():
        parts.append(f'ТОН БРЕНДА (от владельца):\n{brand_voice.strip()}')
    if extra_facts.strip():
        parts.append(f'ЗАМЕТКИ ВЛАДЕЛЬЦА (актуальные факты, можно использовать):\n{extra_facts.strip()}')
    parts.append(
        'ДАННЫЕ (Knowledge Core, JSON):\n'
        + json.dumps(knowledge, ensure_ascii=False, indent=1)
    )
    user_message = '\n\n'.join(parts)

    message = client.messages.create(
        model=model,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[{'role': 'user', 'content': user_message}],
    )
    raw = message.content[0].text.strip()

    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]

    try:
        # strict=False — модель кладёт в "text" литеральные переводы строк
        result = json.loads(raw, strict=False)
        text = (result.get('text') or '').strip()
    except json.JSONDecodeError:
        # ИИ ответил просто текстом — берём как есть (без обломков JSON)
        logger.warning('marketer: non-JSON reply, using raw text (%d chars)', len(raw))
        text = raw if not raw.lstrip().startswith('{') else ''

    if not text:
        raise RuntimeError('ИИ вернул пустой или нечитаемый ответ.')
    return text, model
