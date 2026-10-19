"""
Публикация роликов студии (19.10.2026): ВКонтакте — видео в пост на стене
сообщества, Телеграм — видео в канал сети.

Правила:
  • публикуется только СОГЛАСОВАННЫЙ ролик и только его текущая версия;
  • план публикации живёт в `StudioProject.publish_plan` строками
    `{id, channel, fmt, at, status, post_id, error}`; статусы:
    scheduled → published | failed; повтор упавшей строки — тем же планом;
  • строку берёт тик `publish_due_marketer_posts_task` (раз в 5 минут) под
    `select_for_update` — два тика не опубликуют ролик дважды;
  • ВК: `video.save` (видео в альбом сообщества, без автопоста) → загрузка →
    `wall.post` с вложением. Нужен токен стены с правом «Видео»; без него —
    текст ошибки ВК в строке плана, ролик и план целы;
  • Телеграм: `sendVideo` в канал из настроек (`tg_channel`) ботом гостей,
    подпись до 1024 знаков. Бот должен быть администратором канала.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, time, timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import MarketerPost, MarketerPostStatus, MarketerSettings, StudioProject
from .publisher import VK_API_VERSION, _resolve_group_id

logger = logging.getLogger(__name__)

CHANNELS = ('vk', 'tg')
DEFAULT_FMT = {'vk': '9x16', 'tg': '1x1'}
SLOT_HOUR = 18
TG_CAPTION = 1024


class PublishError(Exception):
    pass


def _файл(project: StudioProject, fmt: str):
    v = project.current_version
    if v is None:
        raise PublishError('у ролика нет версии')
    out = v.files.filter(fmt=fmt).first() or v.files.order_by('pk').first()
    if out is None:
        raise PublishError('у текущей версии нет готовых файлов')
    return out


def в_вк(project: StudioProject, fmt: str, text: str) -> str:
    cfg = MarketerSettings.objects.first()
    if cfg is None or not cfg.vk_wall_token:
        raise PublishError('не задан токен стены ВК (админка маркетолога)')
    group_id = _resolve_group_id(cfg)
    if not group_id:
        raise PublishError('не удалось определить сообщество ВК')
    out = _файл(project, fmt)
    base = {'access_token': cfg.vk_wall_token, 'v': VK_API_VERSION}
    data = requests.post('https://api.vk.com/method/video.save', data={
        **base, 'group_id': group_id, 'name': (project.title or 'Ролик')[:128],
        'description': text[:5000], 'wallpost': 0, 'no_comments': 0,
    }, timeout=20).json()
    if 'error' in data:
        e = data['error']
        raise PublishError(f"VK video.save {e.get('error_code')}: {e.get('error_msg')}")
    r = data['response']
    with out.file.open('rb') as fh:
        up = requests.post(r['upload_url'], files={'video_file': (out.file.name.rsplit('/', 1)[-1], fh, 'video/mp4')},
                           timeout=600)
    try:
        up_json = up.json()
    except ValueError:
        up_json = {}
    if up.status_code >= 400 or up_json.get('error'):
        raise PublishError(f'ВК не принял файл: {up.status_code} {str(up_json)[:200]}')
    owner = r.get('owner_id') or -group_id
    attachment = f"video{owner}_{r['video_id']}"
    data = requests.post('https://api.vk.com/method/wall.post', data={
        **base, 'owner_id': -group_id, 'from_group': 1, 'message': text, 'attachments': attachment,
    }, timeout=20).json()
    if 'error' in data:
        e = data['error']
        raise PublishError(f"VK wall.post {e.get('error_code')}: {e.get('error_msg')}")
    return str(data.get('response', {}).get('post_id', ''))


def в_телеграм(project: StudioProject, fmt: str, text: str) -> str:
    cfg = MarketerSettings.objects.first()
    channel = (getattr(cfg, 'tg_channel', '') or '').strip() if cfg else ''
    if not channel:
        raise PublishError('не указан Телеграм-канал сети (настройки маркетолога)')
    token = getattr(settings, 'TELEGRAM_GUEST_BOT_TOKEN', '') or ''
    if not token:
        raise PublishError('бот гостей не настроен')
    out = _файл(project, fmt)
    with out.file.open('rb') as fh:
        files = {'video': (out.file.name.rsplit('/', 1)[-1], fh, 'video/mp4')}
        data = {'chat_id': channel, 'caption': text[:TG_CAPTION], 'supports_streaming': 'true'}
        if out.width and out.height:
            data.update(width=out.width, height=out.height, duration=int(out.duration or 0))
        r = requests.post(f'https://api.telegram.org/bot{token}/sendVideo', data=data, files=files, timeout=600)
    try:
        payload = r.json()
    except ValueError:
        raise PublishError(f'Телеграм ответил {r.status_code}')
    if not payload.get('ok'):
        raise PublishError(f"Телеграм: {payload.get('description') or payload.get('error_code')}")
    return str(payload.get('result', {}).get('message_id', ''))


def свободный_слот(now=None) -> datetime:
    """Ближайший день без запланированного поста или ролика, 18:00 по Москве."""
    now = timezone.localtime(now or timezone.now())
    занято = set()
    for p in MarketerPost.objects.filter(status=MarketerPostStatus.SCHEDULED, scheduled_at__gte=now):
        занято.add(timezone.localtime(p.scheduled_at).date())
    for pr in StudioProject.objects.exclude(publish_plan=[]):
        for row in pr.publish_plan or []:
            if row.get('status') == 'scheduled' and row.get('at'):
                try:
                    занято.add(timezone.localtime(datetime.fromisoformat(row['at'])).date())
                except ValueError:
                    pass
    day = now.date() + timedelta(days=1 if now.hour >= SLOT_HOUR - 1 else 0)
    for _ in range(60):
        if day not in занято:
            break
        day += timedelta(days=1)
    return timezone.make_aware(datetime.combine(day, time(SLOT_HOUR, 0)))


def запланировать(project: StudioProject, rows: list[dict], text: str) -> StudioProject:
    """Добавить строки плана. rows: [{channel, fmt?, at?}] — at пусто = сейчас."""
    if project.approval != StudioProject.Approval.APPROVED:
        raise PublishError('ролик не согласован')
    if project.status != StudioProject.Status.READY:
        raise PublishError('ролик ещё не собран')
    now = timezone.now()
    plan = list(project.publish_plan or [])
    for row in rows:
        ch = row.get('channel')
        if ch not in CHANNELS:
            raise PublishError('channel: vk или tg')
        at = row.get('at')
        if at:
            try:
                at_dt = datetime.fromisoformat(str(at).replace('Z', '+00:00'))
            except ValueError:
                raise PublishError('at: дата и время ISO')
            if timezone.is_naive(at_dt):
                at_dt = timezone.make_aware(at_dt)
        else:
            at_dt = now
        plan.append({'id': uuid.uuid4().hex[:10], 'channel': ch, 'fmt': row.get('fmt') or DEFAULT_FMT[ch],
                     'at': at_dt.isoformat(), 'status': 'scheduled', 'post_id': '', 'error': '',
                     'version': project.current_version.number if project.current_version else None})
    project.publish_plan = plan
    if text is not None:
        project.post_text = text.strip()[:5000]
    project.save(update_fields=['publish_plan', 'post_text', 'updated_at'])
    return project


def отменить(project: StudioProject, row_id: str) -> StudioProject:
    plan = list(project.publish_plan or [])
    for row in plan:
        if row.get('id') == row_id and row.get('status') in ('scheduled', 'failed'):
            row['status'] = 'cancelled'
    project.publish_plan = plan
    project.save(update_fields=['publish_plan', 'updated_at'])
    return project


def опубликовать_назревшее(now=None) -> dict:
    """Все строки планов этой схемы, чьё время пришло. Зовёт тик маркетолога."""
    now = now or timezone.now()
    итог = {'published': 0, 'failed': 0}
    ids = [p.pk for p in StudioProject.objects.exclude(publish_plan=[]).only('pk', 'publish_plan')
           if any(r.get('status') == 'scheduled' for r in (p.publish_plan or []))]
    for pk in ids:
        with transaction.atomic():
            project = (StudioProject.objects.select_for_update(of=('self',)).select_related('current_version')
                       .filter(pk=pk).first())
            if project is None:
                continue
            plan = list(project.publish_plan or [])
            менялось = False
            for row in plan:
                if row.get('status') != 'scheduled':
                    continue
                try:
                    at = datetime.fromisoformat(row['at'])
                except (KeyError, ValueError):
                    at = now
                if at > now:
                    continue
                if project.approval != StudioProject.Approval.APPROVED:
                    row.update(status='failed', error='ролик больше не согласован (новая версия?)')
                    итог['failed'] += 1
                    менялось = True
                    continue
                try:
                    fn = в_вк if row['channel'] == 'vk' else в_телеграм
                    row['post_id'] = fn(project, row.get('fmt') or DEFAULT_FMT[row['channel']], project.post_text or '')
                    row.update(status='published', error='', published_at=timezone.now().isoformat())
                    итог['published'] += 1
                except Exception as exc:  # сеть, ВК, Телеграм, битый файл — строка падает, план цел
                    if exc.__class__.__name__ == 'SoftTimeLimitExceeded':
                        raise
                    row.update(status='failed', error=str(exc)[:500])
                    итог['failed'] += 1
                    logger.warning('studio publish %s/%s failed: %s', project.pk, row.get('channel'), exc)
                менялось = True
            if менялось:
                project.publish_plan = plan
                project.save(update_fields=['publish_plan', 'updated_at'])
    return итог
