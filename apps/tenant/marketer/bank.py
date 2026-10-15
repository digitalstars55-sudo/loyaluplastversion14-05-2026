"""
Фотобанк маркетолога (16.10.2026): подобрать настоящее фото к посту.

Подбор простой и предсказуемый: слова из названия и меток фото ищутся в тексте
поста и в «что на картинке». Совпало больше слов — то фото и берём; при
равенстве — то, которое ставили реже. Ничего не совпало — фото нет, рисует ИИ.
"""
from __future__ import annotations

import re

_WORD = re.compile(r'[а-яёa-z0-9]+', re.IGNORECASE)
# Короткие и общие слова совпадают со всем подряд — их не считаем.
_STOP = {'фото', 'блюдо', 'блюда', 'кафе', 'наше', 'наши', 'наша', 'зала', 'день', 'levone',
         'левон', 'меню', 'гости', 'гость', 'вкусно', 'очень', 'свой', 'своё', 'новое'}


def _stem(word: str) -> str:
    """Грубая основа: «хачапури/хачапурями» и «круассан/круассаны» совпадают."""
    w = word.lower().replace('ё', 'е')
    return w[:6] if len(w) > 6 else w


def words(text: str) -> set[str]:
    return {_stem(w) for w in _WORD.findall(text or '') if len(w) >= 4 and w.lower() not in _STOP}


def photo_words(title: str, tags: str) -> set[str]:
    return words(f'{title} {(tags or "").replace(",", " ")}')


def best_match(post_text: str, visual: str, photos) -> object | None:
    """photos — итерируемое с полями title, tags, used_count. Возвращает фото или None."""
    in_post = words(f'{post_text} {visual}')
    best, best_key = None, None
    for ph in photos:
        hit = len(photo_words(ph.title, ph.tags) & in_post)
        if not hit:
            continue
        key = (hit, -(ph.used_count or 0))
        if best_key is None or key > best_key:
            best, best_key = ph, key
    return best


def copy_to_post(photo, post) -> None:
    """Кладёт копию фото в пост: удаление фото из банка не ломает пост."""
    import os
    import uuid

    from django.core.files.base import ContentFile
    from django.db.models import F

    photo.image.open('rb')
    try:
        raw = photo.image.read()
    finally:
        photo.image.close()
    ext = os.path.splitext(photo.image.name)[1].lower() or '.jpg'
    if post.image:
        post.image.delete(save=False)
    post.image.save(f'{uuid.uuid4().hex}{ext}', ContentFile(raw), save=False)
    post.image_source = 'bank'
    post.save(update_fields=['image', 'image_source', 'updated_at'])
    type(photo).objects.filter(pk=photo.pk).update(used_count=F('used_count') + 1)
