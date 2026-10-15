"""
Картинка к посту маркетолога (волна 4 ТЗ, 14.10.2026) — OpenAI Images.

Тот же путь, что у иллюстраций уроков CheckUp (`apps/lms/lesson_style.py`):
одна картинка 1536×1024 medium, без текста и логотипов на изображении.
Картинка — только ПРЕДЛОЖЕНИЕ: человек видит её в черновике и может заменить
или убрать до публикации.

16.10.2026 — фирменный стиль:
  · фотобанк: при «сначала фотобанк» к посту ставится настоящее фото, если
    блюдо из его названия/меток есть в тексте; ИИ тогда не зовётся;
  · образ: общий стиль + «Фирменный стиль картинок» из настроек + «что на
    картинке», которое ИИ выбрал по базе знаний вместе с текстом;
  · образцы стиля из фотобанка уходят ИИ как примеры (images/edits). Не
    принял — рисуем без них, а не падаем.
"""
from __future__ import annotations

import base64
import logging
import os
import uuid

import requests
from django.conf import settings
from django.core.files.base import ContentFile

from apps.tenant.marketer import progress

logger = logging.getLogger(__name__)

URL = 'https://api.openai.com/v1/images/generations'
STYLE = ('Appetizing, warm, realistic food and cafe atmosphere photo-illustration for a social media '
         'post of a restaurant. No text, no letters, no numbers, no logos, no watermarks anywhere.')
REFERENCE_NOTE = ('The attached photos are real photos of this restaurant: match their lighting, '
                  'tableware, table surfaces, colors and mood. Do not copy them, create a new scene.')
MAX_REFERENCES = 4


class ImageError(Exception):
    pass


def prompt_for(post, brand_style: str = '', with_references: bool = False) -> str:
    topic = (post.topic or '').strip()
    body = ' '.join((post.text or '').split())[:500]
    visual = ' '.join((getattr(post, 'visual', '') or '').split())[:700]
    parts = [STYLE]
    if with_references:
        parts.append(REFERENCE_NOTE)
    if brand_style.strip():
        parts.append('Brand visual style (follow it): ' + ' '.join(brand_style.split())[:600])
    if visual:
        parts.append(f'Scene: {visual}')
    if topic:
        parts.append(f'Theme: {topic}.')
    parts.append(f'Post: {body}')
    return ' '.join(parts)[:3000]


def _base_url() -> str:
    # 15.10.2026: тот же адрес, что у текста (`openai_compat`). Напрямую
    # api.openai.com с нашего сервера отвечает «Country, region, or territory
    # not supported» — картинка не рисовалась ни разу.
    configured = (getattr(settings, 'OPENAI_BASE_URL', '') or os.getenv('OPENAI_BASE_URL') or '')
    return configured.rstrip('/') if configured else 'https://api.openai.com/v1'


def _images_url() -> str:
    return f'{_base_url()}/images/generations'


def _session() -> requests.Session:
    """Сессия с прокси OpenAI (`OPENAI_PROXY_URL`) — только для ИИ, не глобально."""
    sess = requests.Session()
    proxy_url = (getattr(settings, 'OPENAI_PROXY_URL', '') or os.getenv('OPENAI_PROXY_URL') or '').strip()
    if proxy_url:
        sess.proxies.update({'http': proxy_url, 'https': proxy_url})
    return sess


def _error_text(r) -> str:
    try:
        err = (r.json() or {}).get('error') or {}
    except ValueError:
        err = {}
    return str(err.get('message') or r.status_code)


def _draw(prompt: str, references: list) -> tuple[bytes, dict]:
    """Рисует картинку; с образцами — через images/edits, при отказе — без них."""
    key = getattr(settings, 'OPENAI_API_KEY', '') or ''
    if not key:
        raise ImageError('Не задан ключ ИИ — картинку нарисовать нечем.')
    model = getattr(settings, 'MARKETER_IMAGE_MODEL', 'gpt-image-1')
    common = {'model': model, 'size': '1536x1024', 'quality': 'medium', 'n': 1}
    sess = _session()
    if references:
        files = []
        try:
            for i, ref in enumerate(references[:MAX_REFERENCES]):
                ref.image.open('rb')
                try:
                    files.append(('image[]', (f'ref{i}{os.path.splitext(ref.image.name)[1] or ".jpg"}',
                                              ref.image.read(), 'image/jpeg')))
                finally:
                    ref.image.close()
            r = sess.post(f'{_base_url()}/images/edits', headers={'Authorization': f'Bearer {key}'},
                          data={**{k: str(v) for k, v in common.items()}, 'prompt': prompt},
                          files=files, timeout=240)
            if r.status_code == 200:
                data = r.json()
                return base64.b64decode(data['data'][0]['b64_json']), data.get('usage') or {}
            logger.warning('marketer image: edits with references refused (%s), drawing without',
                           _error_text(r))
        except Exception as e:                           # noqa: BLE001 — без образцов всё равно рисуем
            logger.warning('marketer image: references failed (%s), drawing without', e)
        prompt = prompt.replace(REFERENCE_NOTE + ' ', '')
    base = getattr(settings, 'OPENAI_IMAGES_URL', '') or _images_url()
    try:
        r = sess.post(base, headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                      json={**common, 'prompt': prompt, 'output_format': 'jpeg',
                            'output_compression': 82},
                      timeout=180)
    except Exception as e:
        raise ImageError(f'Сеть до ИИ недоступна: {e}')
    if r.status_code != 200:
        raise ImageError(f'ИИ не нарисовал картинку: {_error_text(r)}')
    data = r.json()
    return base64.b64decode(data['data'][0]['b64_json']), data.get('usage') or {}


def generate_for_post(post, job=None, *, allow_bank: bool | None = None) -> str:
    """Ставит картинку к посту. Возвращает 'bank' или 'ai'. Ошибку — исключением (текст для человека).

    allow_bank=None — по настройке сети («сначала фотобанк»); True/False — явно.
    """
    from apps.tenant.marketer import bank
    from apps.tenant.marketer.models import MarketerPhoto, MarketerSettings

    cfg = MarketerSettings.objects.first()
    if allow_bank is None:
        allow_bank = bool(cfg and cfg.image_source == 'bank_first')

    progress.stage(job, 'bank')
    if allow_bank:
        photo = bank.best_match(post.text, getattr(post, 'visual', ''),
                                MarketerPhoto.objects.filter(kind='photo', is_active=True))
        if photo is not None:
            progress.stage(job, 'save', f'Ставлю фото из банка: {photo.title or "без названия"}')
            bank.copy_to_post(photo, post)
            return 'bank'

    references = list(MarketerPhoto.objects.filter(kind='style', is_active=True)
                      .order_by('-created_at')[:MAX_REFERENCES])
    progress.stage(job, 'compose', 'Собираю образ: стиль сети'
                   + (f', {len(references)} образца из фотобанка' if references else '')
                   + (' и приём из базы знаний' if getattr(post, 'visual', '') else ''))
    prompt = prompt_for(post, cfg.image_style if cfg else '', with_references=bool(references))

    progress.stage(job, 'draw', 'Рисую — обычно 30–60 секунд')
    raw, usage = _draw(prompt, references)
    model = getattr(settings, 'MARKETER_IMAGE_MODEL', 'gpt-image-1')
    logger.info('ai.usage where=marketer_image model=%s in=%s out=%s refs=%s', model,
                usage.get('input_tokens', 0), usage.get('output_tokens', 0), len(references))

    progress.stage(job, 'save')
    if post.image:
        post.image.delete(save=False)
    post.image.save(f'{uuid.uuid4().hex}.jpg', ContentFile(raw), save=False)
    post.image_source = 'ai'
    post.save(update_fields=['image', 'image_source', 'updated_at'])
    return 'ai'
