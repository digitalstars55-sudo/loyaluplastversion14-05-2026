"""
Публикация постов маркетолога на стену VK-сообщества.

Подходит токен сообщества с правом «Стена» (проверено вживую 23.08.2026:
scope wall, mask 8192) либо пользовательский токен админа. Токен хранится
в MarketerSettings.vk_wall_token — отдельный от senler-каналов (у тех
только «Сообщения»: спам-флаг одного канала не роняет другой).

Пока токена нет — publish_post честно падает в status=failed с понятной
ошибкой, черновик не теряется.
"""
from __future__ import annotations

import logging

import requests
from django.utils import timezone

from .models import MarketerPost, MarketerPostStatus, MarketerSettings

logger = logging.getLogger(__name__)

VK_API_VERSION = '5.131'


def _resolve_group_id(cfg: MarketerSettings) -> int | None:
    if cfg.vk_group_id:
        return int(cfg.vk_group_id)
    try:
        from apps.tenant.senler.models import SenlerConfig
        sc = SenlerConfig.objects.filter(is_active=True).order_by('pk').first()
        return int(sc.vk_group_id) if sc else None
    except Exception:
        logger.exception('marketer: group id resolve failed')
        return None


def publish_post(post: MarketerPost) -> bool:
    """
    Публикует пост на стену. Возвращает True при успехе.

    Статусы: → PUBLISHED (+vk_post_id) либо → FAILED (+error, текст цел).
    Публикуются только DRAFT/FAILED (повтор после ошибки разрешён).
    """
    if post.status not in (MarketerPostStatus.DRAFT, MarketerPostStatus.FAILED):
        post.error = f'Пост в статусе «{post.get_status_display()}» — публикация не выполнена.'
        post.save(update_fields=['error', 'updated_at'])
        return False

    cfg = MarketerSettings.objects.first()

    def _fail(msg: str) -> bool:
        post.status = MarketerPostStatus.FAILED
        post.error = msg
        post.save(update_fields=['status', 'error', 'updated_at'])
        logger.warning('marketer publish failed post=%s: %s', post.pk, msg)
        return False

    if cfg is None or not cfg.is_enabled:
        return _fail('AI-маркетолог выключен в настройках.')
    if not cfg.vk_wall_token:
        return _fail('Не задан токен для стены (нужен пользовательский токен админа сообщества, права wall).')
    group_id = _resolve_group_id(cfg)
    if not group_id:
        return _fail('Не удалось определить ID группы VK (ни в настройках, ни в SenlerConfig).')

    try:
        resp = requests.post(
            'https://api.vk.com/method/wall.post',
            data={
                'owner_id': -group_id,
                'from_group': 1,
                'message': post.text,
                'access_token': cfg.vk_wall_token,
                'v': VK_API_VERSION,
            },
            timeout=15,
        )
        payload = resp.json()
    except Exception as e:
        return _fail(f'Сетевая ошибка VK API: {e}')

    if 'error' in payload:
        err = payload['error']
        return _fail(f"VK error {err.get('error_code')}: {err.get('error_msg')}")

    post.status = MarketerPostStatus.PUBLISHED
    post.published_at = timezone.now()
    post.vk_post_id = str(payload.get('response', {}).get('post_id', ''))
    post.error = ''
    post.save(update_fields=['status', 'published_at', 'vk_post_id', 'error', 'updated_at'])
    logger.info('marketer: post %s published, vk_post_id=%s', post.pk, post.vk_post_id)
    return True
