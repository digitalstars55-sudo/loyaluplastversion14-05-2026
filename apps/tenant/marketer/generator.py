"""
Генератор постов через Claude (Anthropic API).

Тот же паттерн доступа, что в analytics/ai_service.py: ключ из
settings.OPENAI_API_KEY, опциональный прокси OPENAI_BASE_URL. Модель
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
KB_LIMIT = 120_000  # знаков базы знаний в одном запросе (~40 тыс. токенов)

_SYSTEM_PROMPT = """Ты — SMM-маркетолог кафе/ресторана. Пишешь посты для стены VK-сообщества на русском языке.

ЖЕЛЕЗНЫЕ ПРАВИЛА:
0. Если передана БАЗА ЗНАНИЙ — она главнее всего остального: её правила, позиционирование, рубрики, меню и запреты соблюдай дословно; стиль бери из её примеров. Где база знаний расходится с данными (JSON) или с этими правилами — прав документ базы знаний.
1. Используй ТОЛЬКО факты из базы знаний, переданных данных (JSON) и заметок владельца. НИЧЕГО не выдумывай: ни цен, ни акций, ни блюд, ни адресов, ни дат, которых там нет.
2. Не называй имён гостей. Цитаты отзывов можно приводить дословно или сокращённо, без имён.
3. Внутренние коды (R3F1 и т.п.) и служебные термины (RF-сегмент, тенант) НИКОГДА не попадают в пост — переводи в человеческий язык или опускай.
4. Длина поста: 350–800 символов. Эмодзи умеренно (3–6 на пост). Хэштеги: максимум 2 или ни одного.
5. Если в данных есть mini_app_link — закончи мягким призывом заглянуть в приложение за подарками, со ссылкой.
6. Цифры недели используй выборочно — 2–3 самые живые, не сваливай всю статистику.
7. Тон — по заметке «Тон бренда», если она есть; иначе тепло и по-соседски, без канцелярита и без агрессивных продаж.
8. Придумай картинку к посту: что в кадре, ракурс, свет, настроение — по визуальным приёмам и правилам из базы знаний, если они там есть. Только реальные блюда и обстановка из базы знаний; никаких надписей, букв и логотипов на картинке. 1–3 предложения.

Формат ответа — строго JSON без markdown:
{"text": "готовый текст поста", "visual": "что на картинке"}

Никакого другого текста — только JSON."""

_DIGEST_TASK = (
    'Напиши еженедельный пост-дайджест «Что нового?» для подписчиков сообщества: '
    'чем жила сеть на этой неделе — гости, визиты, подаренные подарки, добрые '
    'отзывы. Пост должен радовать постоянных гостей и вызывать желание зайти.'
)


def _ask(system: str, user_message: str, max_tokens: int) -> tuple[str, str]:
    """Один вызов модели маркетолога → (сырой текст ответа, модель)."""
    api_key = getattr(settings, 'OPENAI_API_KEY', None)
    if not api_key:
        raise RuntimeError('OPENAI_API_KEY не задан в settings.')

    import os
    from apps.shared.ai import openai_compat as anthropic
    proxy_url = os.getenv('OPENAI_BASE_URL', '')
    client = (
        anthropic.Anthropic(api_key=api_key, base_url=proxy_url)
        if proxy_url else anthropic.Anthropic(api_key=api_key)
    )
    model = getattr(settings, 'MARKETER_AI_MODEL', DEFAULT_MODEL)
    from apps.shared.ai.usage import log_usage
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{'role': 'user', 'content': user_message}],
    )
    log_usage('marketer', message)
    raw = ''.join(getattr(block, 'text', '') for block in message.content
                  if getattr(block, 'type', '') == 'text').strip()
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'):
            raw = raw[4:]
    return raw.strip(), model


def generate_post(knowledge: dict, *, task: str = _DIGEST_TASK,
                  brand_voice: str = '', extra_facts: str = '',
                  kb_text: str | None = None, brief: str = '') -> tuple[str, str]:
    """
    Генерирует текст поста.

    Returns:
        (text, model_used)
    Raises:
        RuntimeError — нет ключа API или ИИ вернул нечитаемый ответ.
    """
    out = generate_post_ex(knowledge, task=task, brand_voice=brand_voice,
                           extra_facts=extra_facts, kb_text=kb_text, brief=brief)
    return out['text'], out['model']


def generate_post_ex(knowledge: dict, *, task: str = _DIGEST_TASK,
                     brand_voice: str = '', extra_facts: str = '',
                     kb_text: str | None = None, brief: str = '') -> dict:
    """Как `generate_post`, но с «что на картинке»: {text, visual, model}."""
    if kb_text is None:
        from apps.tenant.analytics.ai_service import get_knowledge_text
        kb_text = get_knowledge_text('content')

    parts = [f'ЗАДАЧА: {task}']
    # «О чём пост» из строки кабинета (16.10.2026) — главнее общей задачи.
    if (brief or '').strip():
        parts.append('УКАЗАНИЕ ВЛАДЕЛЬЦА ДЛЯ ЭТОГО ПОСТА (главное, выполни его; '
                     'база знаний — источник фактов и тона):\n' + brief.strip()[:1500])
    if kb_text.strip():
        parts.append('БАЗА ЗНАНИЙ СЕТИ (главнее данных ниже):\n' + kb_text.strip()[:KB_LIMIT])
    if brand_voice.strip():
        parts.append(f'ТОН БРЕНДА (от владельца):\n{brand_voice.strip()}')
    if extra_facts.strip():
        parts.append(f'ЗАМЕТКИ ВЛАДЕЛЬЦА (актуальные факты, можно использовать):\n{extra_facts.strip()}')
    parts.append(
        'ДАННЫЕ (Knowledge Core, JSON):\n'
        + json.dumps(knowledge, ensure_ascii=False, indent=1)
    )
    user_message = '\n\n'.join(parts)

    raw, model = _ask(_SYSTEM_PROMPT, user_message, 1400)
    visual = ''
    try:
        # strict=False — модель кладёт в "text" литеральные переводы строк
        result = json.loads(raw, strict=False)
        text = (result.get('text') or '').strip()
        visual = str(result.get('visual') or '').strip()
    except json.JSONDecodeError:
        # ИИ ответил просто текстом — берём как есть (без обломков JSON)
        logger.warning('marketer: non-JSON reply, using raw text (%d chars)', len(raw))
        text = raw if not raw.lstrip().startswith('{') else ''

    if not text:
        raise RuntimeError('ИИ вернул пустой или нечитаемый ответ.')
    return {'text': text, 'visual': visual[:1000], 'model': model}


_REWRITE_PROMPT = """Ты — SMM-маркетолог кафе/ресторана. Тебе дают готовый пост для стены VK и задание владельца: что в нём поменять.

ПРАВИЛА:
1. Выполни задание владельца — оно главное. Если задание — «напиши заново про …», пиши новый пост на эту тему.
2. Факты — ТОЛЬКО из базы знаний и текущего поста. Ничего не выдумывай: ни цен, ни акций, ни блюд, ни дат, которых там нет.
3. Тон — по базе знаний и заметке «Тон бренда», если они есть.
4. Без markdown. Верни ТОЛЬКО новый текст поста, без пояснений, кавычек и подписи."""


def rewrite_post(current_text: str, instruction: str, *, brand_voice: str = '',
                 kb_text: str | None = None) -> dict:
    """Переписать пост по заданию владельца → {text, model} (16.10.2026).

    Владелец: «после того как готов черновик — стереть текст, написать задание
    прямо в этом окне и перегенерировать». Задание не становится текстом поста.
    """
    instruction = (instruction or '').strip()
    if not instruction:
        raise RuntimeError('Пустое задание — нечего выполнять.')
    if kb_text is None:
        from apps.tenant.analytics.ai_service import get_knowledge_text
        kb_text = get_knowledge_text('content')
    parts = ['ЗАДАНИЕ ВЛАДЕЛЬЦА (выполни обязательно):\n' + instruction[:1500]]
    if (current_text or '').strip():
        parts.append('ТЕКУЩИЙ ПОСТ:\n' + current_text.strip()[:6000])
    if kb_text.strip():
        parts.append('БАЗА ЗНАНИЙ СЕТИ:\n' + kb_text.strip()[:KB_LIMIT])
    if brand_voice.strip():
        parts.append(f'ТОН БРЕНДА (от владельца):\n{brand_voice.strip()}')
    raw, model = _ask(_REWRITE_PROMPT, '\n\n'.join(parts), 1400)
    text = (raw or '').strip()
    if text.startswith('{'):
        try:
            text = str(json.loads(text, strict=False).get('text') or '').strip()
        except json.JSONDecodeError:
            text = ''
    if not text:
        raise RuntimeError('ИИ вернул пустой ответ.')
    return {'text': text, 'model': model}


# ── Контент-план по базе знаний (16.10.2026) ─────────────────────────────────

_PLAN_PROMPT = """Ты — SMM-маркетолог кафе/ресторана. Составляешь контент-план постов для стены VK-сообщества.

ПРАВИЛА:
1. Если передана БАЗА ЗНАНИЙ — план строй по ней: её рубрики, частота, поводы, приоритеты и запреты главнее всего. Темы бери оттуда.
2. Не повторяй темы уже запланированных постов и не ставь одну тему два раза подряд.
3. Акции и цены — только если они есть в указании владельца или в базе знаний как действующие сейчас.
4. Для каждой даты: короткая тема (до 60 знаков), тип поста и задача для автора — что именно написать (1–3 предложения, по фактам из базы знаний).
5. Тип поста — одно из: insight (о гостях, блюдах, месте), promo (акция, подарки, приложение), digest (что нового за неделю), custom (всё остальное).

Формат ответа — строго JSON без markdown:
{"items": [{"date": "ГГГГ-ММ-ДД", "topic": "...", "post_type": "insight", "task": "..."}]}"""

POST_TYPES = ('insight', 'promo', 'digest', 'custom')


def parse_plan(raw: str, dates: list[str]) -> list[dict]:
    """Ответ планировщика → ровно по пункту на каждую дату (чужие даты и мусор отбрасываются).

    Дата без пункта получает None — её закроет запасная тема по кругу.
    """
    try:
        items = (json.loads(raw, strict=False) or {}).get('items') or []
    except (json.JSONDecodeError, AttributeError):
        return [None] * len(dates)
    by_date = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        d = str(it.get('date') or '')[:10]
        topic = str(it.get('topic') or '').strip()[:200]
        task = str(it.get('task') or '').strip()[:1500]
        if d not in dates or d in by_date or not (topic and task):
            continue
        pt = str(it.get('post_type') or '').strip().lower()
        by_date[d] = {'topic': topic, 'task': task, 'post_type': pt if pt in POST_TYPES else 'custom'}
    # Пункты без даты (модель иногда её теряет) ставим на свободные даты по порядку.
    spare = [it for it in items if isinstance(it, dict) and str(it.get('date') or '')[:10] not in dates
             and str(it.get('topic') or '').strip() and str(it.get('task') or '').strip()]
    out = []
    for d in dates:
        if d in by_date:
            out.append(by_date[d])
        elif spare:
            it = spare.pop(0)
            pt = str(it.get('post_type') or '').strip().lower()
            out.append({'topic': str(it['topic']).strip()[:200], 'task': str(it['task']).strip()[:1500],
                        'post_type': pt if pt in POST_TYPES else 'custom'})
        else:
            out.append(None)
    return out


def plan_posts(knowledge: dict, dates: list[str], *, brief: str = '', existing: list[str] | None = None,
               brand_voice: str = '', extra_facts: str = '', kb_text: str | None = None) -> list:
    """Темы плана по базе знаний: список той же длины, что dates (None — темы нет)."""
    if kb_text is None:
        from apps.tenant.analytics.ai_service import get_knowledge_text
        kb_text = get_knowledge_text('content')
    parts = [f'ДАТЫ ПОСТОВ ({len(dates)}): ' + ', '.join(dates)]
    if (brief or '').strip():
        parts.append('УКАЗАНИЕ ВЛАДЕЛЬЦА К ПЛАНУ (главное):\n' + brief.strip()[:1500])
    if existing:
        parts.append('УЖЕ ЕСТЬ В ПЛАНЕ (не повторяй):\n' + '\n'.join(existing[:40]))
    if kb_text.strip():
        parts.append('БАЗА ЗНАНИЙ СЕТИ (главнее всего):\n' + kb_text.strip()[:KB_LIMIT])
    if brand_voice.strip():
        parts.append(f'ТОН БРЕНДА:\n{brand_voice.strip()}')
    if extra_facts.strip():
        parts.append(f'ЗАМЕТКИ ВЛАДЕЛЬЦА:\n{extra_facts.strip()}')
    parts.append('ДАННЫЕ (JSON):\n' + json.dumps(knowledge, ensure_ascii=False)[:20000])
    raw, _model = _ask(_PLAN_PROMPT, '\n\n'.join(parts), 3000)
    return parse_plan(raw, dates)
