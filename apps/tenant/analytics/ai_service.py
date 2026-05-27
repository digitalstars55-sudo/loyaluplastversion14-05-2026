"""
AI-сервис для анализа тональности отзывов и обращений гостей.

Использует Claude (Anthropic API) с инструкциями из базы знаний (KnowledgeBaseDocument).

Вызывается синхронно при сохранении нового TestimonialMessage с source APP или VK_MESSAGE.
Обновляет sentiment и ai_comment у родительского TestimonialConversation.

Настройка: добавьте ANTHROPIC_API_KEY в settings.py (или .env).
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# ── Sentiment choices (must match TestimonialConversation.Sentiment) ──────────

SENTIMENT_POSITIVE           = 'POSITIVE'
SENTIMENT_NEGATIVE           = 'NEGATIVE'
SENTIMENT_PARTIALLY_NEGATIVE = 'PARTIALLY_NEGATIVE'
SENTIMENT_NEUTRAL            = 'NEUTRAL'
SENTIMENT_SPAM               = 'SPAM'
SENTIMENT_WAITING            = 'WAITING'

_VALID_SENTIMENTS = {
    SENTIMENT_POSITIVE, SENTIMENT_NEGATIVE,
    SENTIMENT_PARTIALLY_NEGATIVE, SENTIMENT_NEUTRAL, SENTIMENT_SPAM,
}


# ── Pre-classifier: numeric-only ratings ──────────────────────────────────────

def _try_numeric_rating(text: str, source: str) -> dict | None:
    """
    Если text это только цифра-оценка (1–10) без смыслового сопровождения —
    раскладываем по шкале:
      VK сообщения / умолчание — шкала 1–10:
        1–6  → NEGATIVE,  7–8 → PARTIALLY_NEGATIVE,  9–10 → POSITIVE
      APP — шкала 1–5:
        1–2  → NEGATIVE,  3   → PARTIALLY_NEGATIVE,  4–5  → POSITIVE
    Допускаются пунктуация, пробелы и эмодзи вокруг цифры — главное, чтобы
    кроме цифры не было букв.
    """
    if not text:
        return None
    import re
    only_alnum = re.sub(r'[\W_]+', '', text, flags=re.UNICODE)
    if not (only_alnum.isdigit() and 1 <= int(only_alnum) <= 10):
        return None
    n = int(only_alnum)
    src = (source or '').upper()
    if src == 'APP':
        if n > 5:
            return None
        if n <= 2:
            sentiment = SENTIMENT_NEGATIVE
        elif n == 3:
            sentiment = SENTIMENT_PARTIALLY_NEGATIVE
        else:
            sentiment = SENTIMENT_POSITIVE
    else:
        if n <= 6:
            sentiment = SENTIMENT_NEGATIVE
        elif n <= 8:
            sentiment = SENTIMENT_PARTIALLY_NEGATIVE
        else:
            sentiment = SENTIMENT_POSITIVE
    return {
        'sentiment': sentiment,
        'comment':   f'Оценка-цифра: {text.strip()} (без текста).',
    }

# ── System prompt ─────────────────────────────────────────────────────────────

_BASE_SYSTEM_PROMPT = """Ты — аналитик отзывов ресторана/кафе. Твоя задача: определить тональность
сообщения гостя и объяснить своё решение на русском языке.

Правила анализа:
1. Возвращай строго одно из значений тональности: POSITIVE, NEGATIVE, PARTIALLY_NEGATIVE, NEUTRAL, SPAM
2. POSITIVE — гость доволен, хвалит, благодарит
3. NEGATIVE — гость явно недоволен, жалуется, ругает
4. PARTIALLY_NEGATIVE — есть и позитив, и негатив одновременно
5. NEUTRAL — информационный вопрос, нейтральное сообщение
6. SPAM — не по теме, реклама, случайный текст, нечитаемое сообщение

ВАЖНО про оценки-цифры:
- Сообщения вида «10», «5», «9/10», «5 из 5», «5 баллов» — это оценка, а НЕ спам.
- Если сообщение это только оценка без текста — раскладывай по шкале:
    [Источник: VK_MESSAGE] (шкала 1–10): 1–6 → NEGATIVE, 7–8 → PARTIALLY_NEGATIVE, 9–10 → POSITIVE.
    [Источник: APP] (шкала 1–5): 1–2 → NEGATIVE, 3 → PARTIALLY_NEGATIVE, 4–5 → POSITIVE.
- Если есть и цифра, и осмысленный текст — анализируй В ПЕРВУЮ ОЧЕРЕДЬ текст,
  цифра вторична. Например, «10, но доставка долгая» — это PARTIALLY_NEGATIVE.

Формат ответа — строго JSON без markdown, например:
{"sentiment": "NEGATIVE", "comment": "Гость жалуется на долгое ожидание заказа и невежливый персонал."}

Никакого другого текста — только JSON."""


def _get_knowledge_base_text() -> str:
    """Загружает и объединяет текст всех активных KnowledgeBaseDocument."""
    try:
        from apps.tenant.analytics.models import KnowledgeBaseDocument
        docs = KnowledgeBaseDocument.objects.filter(is_active=True).exclude(extracted_text='')
        texts = [f"=== {doc.title} ===\n{doc.extracted_text}" for doc in docs]
        return '\n\n'.join(texts) if texts else ''
    except Exception as e:
        logger.warning('Failed to load KnowledgeBase: %s', e)
        return ''


def _build_system_prompt() -> str:
    kb_text = _get_knowledge_base_text()
    if kb_text:
        return (
            _BASE_SYSTEM_PROMPT
            + '\n\n--- Дополнительные инструкции из базы знаний ---\n'
            + kb_text
        )
    return _BASE_SYSTEM_PROMPT


def analyze_message(text: str, source: str = '') -> dict:
    """
    Анализирует текст сообщения через Claude.

    Returns:
        {'sentiment': str, 'comment': str}
    Raises:
        RuntimeError если API ключ не задан или произошла ошибка API.
    """
    # Pre-classifier: text consists only of a numeric rating → bucket it without AI
    numeric = _try_numeric_rating(text, source)
    if numeric:
        logger.info('Numeric-only rating detected (source=%s): %r → %s',
                    source, (text or '').strip()[:40], numeric['sentiment'])
        return numeric

    import json

    api_key = getattr(settings, 'ANTHROPIC_API_KEY', None)
    if not api_key:
        raise RuntimeError(
            'ANTHROPIC_API_KEY не задан в settings.py. '
            'Добавьте: ANTHROPIC_API_KEY = "sk-ant-..."'
        )

    import os, anthropic
    proxy_url = os.getenv('AI_PROXY_URL', '')
    client = anthropic.Anthropic(api_key=api_key, base_url=proxy_url) if proxy_url else anthropic.Anthropic(api_key=api_key)

    source_note = f'[Источник: {source}] ' if source else ''
    user_message = f'{source_note}Сообщение гостя:\n\n{text}'

    message = client.messages.create(
        model='claude-haiku-4-5-20251001',    # fast + cheap for classification
        max_tokens=256,
        system=_build_system_prompt(),
        messages=[{'role': 'user', 'content': user_message}],
    )

    raw = message.content[0].text.strip()

    # Strip possible markdown code fences
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: try to extract sentiment from raw text
        logger.warning('Claude returned non-JSON: %s', raw[:200])
        for s in _VALID_SENTIMENTS:
            if s in raw.upper():
                return {'sentiment': s, 'comment': raw[:500]}
        return {'sentiment': SENTIMENT_NEUTRAL, 'comment': raw[:500]}

    sentiment = result.get('sentiment', '').upper()
    if sentiment not in _VALID_SENTIMENTS:
        sentiment = SENTIMENT_NEUTRAL

    return {
        'sentiment': sentiment,
        'comment':   result.get('comment', ''),
    }


def analyze_and_save(conversation_id: int, message_text: str, source: str = '') -> bool:
    """
    Запускает анализ и сохраняет результат в TestimonialConversation.

    Возвращает True при успехе, False при ошибке (логирует, не бросает).
    Используется для вызова из services без прерывания основного потока.
    """
    try:
        result = analyze_message(message_text, source)
    except RuntimeError as e:
        logger.warning('AI analysis skipped: %s', e)
        return False
    except Exception as e:
        logger.exception('AI analysis failed for conversation %s: %s', conversation_id, e)
        return False

    try:
        from apps.tenant.branch.models import TestimonialConversation
        TestimonialConversation.objects.filter(pk=conversation_id).update(
            sentiment=result['sentiment'],
            ai_comment=result['comment'],
        )
        return True
    except Exception as e:
        logger.exception('Failed to save AI result for conversation %s: %s', conversation_id, e)
        return False
