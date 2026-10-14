"""
Картинка к посту маркетолога (волна 4 ТЗ, 14.10.2026) — OpenAI Images.

Тот же путь, что у иллюстраций уроков CheckUp (`apps/lms/lesson_style.py`):
одна картинка 1536×1024 medium, без текста и логотипов на изображении.
Картинка — только ПРЕДЛОЖЕНИЕ: человек видит её в черновике и может заменить
или убрать до публикации.
"""
from __future__ import annotations

import base64
import logging
import uuid

import requests
from django.conf import settings
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)

URL = 'https://api.openai.com/v1/images/generations'
STYLE = ('Appetizing, warm, realistic food and cafe atmosphere photo-illustration for a social media '
         'post of a restaurant. No text, no letters, no numbers, no logos, no watermarks anywhere.')


class ImageError(Exception):
    pass


def prompt_for(post) -> str:
    topic = (post.topic or '').strip()
    body = ' '.join((post.text or '').split())[:600]
    return f'{STYLE} Theme: {topic}. Post: {body}'[:1400]


def generate_for_post(post) -> None:
    """Рисует и прикрепляет картинку к посту. Ошибку — исключением (текст для человека)."""
    key = getattr(settings, 'OPENAI_API_KEY', '') or ''
    if not key:
        raise ImageError('Не задан ключ ИИ — картинку нарисовать нечем.')
    model = getattr(settings, 'MARKETER_IMAGE_MODEL', 'gpt-image-1')
    base = (getattr(settings, 'OPENAI_IMAGES_URL', '') or URL)
    try:
        r = requests.post(base, headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                          json={'model': model, 'prompt': prompt_for(post), 'size': '1536x1024',
                                'quality': 'medium', 'n': 1, 'output_format': 'jpeg',
                                'output_compression': 82},
                          timeout=180)
    except Exception as e:
        raise ImageError(f'Сеть до ИИ недоступна: {e}')
    if r.status_code != 200:
        try:
            err = (r.json() or {}).get('error') or {}
        except ValueError:
            err = {}
        raise ImageError(f'ИИ не нарисовал картинку: {err.get("message") or r.status_code}')
    data = r.json()
    raw = base64.b64decode(data['data'][0]['b64_json'])
    usage = data.get('usage') or {}
    logger.info('ai.usage where=marketer_image model=%s in=%s out=%s', model,
                usage.get('input_tokens', 0), usage.get('output_tokens', 0))
    if post.image:
        post.image.delete(save=False)
    post.image.save(f'{uuid.uuid4().hex}.jpg', ContentFile(raw), save=False)
    post.save(update_fields=['image', 'updated_at'])
