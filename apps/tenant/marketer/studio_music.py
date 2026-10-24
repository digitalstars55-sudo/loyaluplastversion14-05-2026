"""
Музыка ИИ для ролика студии (24.10.2026, владелец: «монтаж доведи до идеала»).

У сети нет трека бренда — ролик выходил немым. Теперь под каждый сценарий
делаем инструментал ElevenLabs Music под его настроение (`music_mood`) и длину.
Трек ложится материалом проекта (вид «Музыка», подпись «Музыка ИИ: …»), сценарий
ссылается на него `music_asset`; правка с тем же настроением берёт прежний трек.

Платный тариф ElevenLabs разрешает коммерческое использование. С этого сервера
ElevenLabs закрыт по гео — идём тем же прокси, что и к OpenAI. Ключ: переменная
`ELEVENLABS_API_KEY` или файл `.env/elevenlabs.key` рядом с `.env.prod`.
Сбой — ролик собирается без музыки, как раньше.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import httpx
from django.conf import settings
from django.core.files.base import ContentFile

log = logging.getLogger(__name__)

URL = 'https://api.elevenlabs.io/v1/music?output_format=mp3_44100_128'
TIMEOUT = 90
PAUSES = (4, 12, None)   # повторы при перегрузке ElevenLabs
DEFAULT_MOOD = 'warm modern cafe groove, light percussion, soft bass, cozy and appetizing'
PREFIX = 'Музыка ИИ: '
AUTHOR = 'ИИ'


def _key() -> str:
    key = (os.getenv('ELEVENLABS_API_KEY') or '').strip()
    if key:
        return key
    path = Path(settings.BASE_DIR) / '.env' / 'elevenlabs.key'
    try:
        return path.read_text().strip()
    except OSError:
        return ''


def дорожка(mood: str, seconds: float) -> bytes:
    """MP3 инструментала заданной длины. Ошибка — исключение."""
    key = _key()
    if not key:
        raise RuntimeError('нет ключа ElevenLabs')
    prompt = (f'Instrumental background music for a {round(seconds)}-second restaurant social media reel: '
              f'{mood.strip() or DEFAULT_MOOD}. Clean start, steady groove, natural ending on the last bar, no vocals.')
    proxy = (os.getenv('OPENAI_PROXY_URL') or '').strip() or None
    body = {'prompt': prompt[:900], 'music_length_ms': int(min(max(seconds, 10), 120) * 1000),
            'force_instrumental': True}
    for пауза in PAUSES:
        resp = httpx.post(URL, headers={'xi-api-key': key}, proxy=proxy, timeout=TIMEOUT, json=body)
        if resp.status_code not in (429, 500, 502, 503, 504) or пауза is None:
            break
        time.sleep(пауза)   # 24.10.2026: «system_busy» 429 в первом же живом прогоне
    if resp.status_code != 200 or not resp.content.startswith((b'ID3', b'\xff')):
        raise RuntimeError(f'ElevenLabs music: HTTP {resp.status_code} {resp.text[:120] if resp.status_code != 200 else ""}')
    return resp.content


def своя_музыка(project, snap: dict) -> bool:
    """Трек бренда в стиле или трек, загруженный в проект людьми, — тогда ИИ музыку не делает."""
    from apps.tenant.marketer.models import StudioAsset
    return bool(snap.get('music')) or StudioAsset.objects.filter(
        project=project, kind=StudioAsset.Kind.MUSIC).exclude(created_by=AUTHOR).exists()


def файл(version):
    """Что играет под версией: трек бренда (снимок стиля, затем текущий), трек проекта от людей, трек ИИ."""
    from apps.tenant.marketer.models import StudioAsset, StudioStyle
    name = (version.style_snapshot or {}).get('music')
    if name:
        return StudioStyle._meta.get_field('music').storage.open(name, 'rb')
    style = StudioStyle.objects.order_by('pk').first()
    if style is not None and style.music:
        return style.music
    свои = StudioAsset.objects.filter(project=version.project, kind=StudioAsset.Kind.MUSIC)
    asset = свои.exclude(created_by=AUTHOR).order_by('-pk').first()
    if asset is None and (version.scenario or {}).get('music_asset'):
        asset = свои.filter(pk=version.scenario['music_asset']).first()
    return asset.file if asset is not None and asset.file else None


def подобрать(project, scn: dict, brand_music: bool, previous: dict | None = None) -> None:
    """Положить в сценарий `music_asset` — трек ИИ под настроение, если своей музыки нет."""
    from apps.tenant.marketer.models import StudioAsset

    scn.pop('music_asset', None)
    if brand_music or scn.get('music') == 'none':
        return
    mood = (scn.get('music_mood') or '').strip() or DEFAULT_MOOD
    seconds = sum(float(s.get('length') or 0) for s in scn.get('scenes') or []) \
        + float((scn.get('final') or {}).get('length') or 2.5)
    prev_id = (previous or {}).get('music_asset')
    if prev_id and ((previous or {}).get('music_mood') or DEFAULT_MOOD).strip() == mood:
        прежний = StudioAsset.objects.filter(pk=prev_id, project=project, kind=StudioAsset.Kind.MUSIC).first()
        if прежний is not None and (прежний.duration or 0) >= seconds - 0.5:
            scn['music_asset'] = прежний.pk
            return
    try:
        mp3 = дорожка(mood, seconds + 0.5)
    except Exception as e:  # noqa: BLE001 — без музыки ролик всё равно собирается
        log.warning('studio: музыка ИИ не сделана (проект %s): %s', project.pk, e)
        return
    asset = StudioAsset(project=project, kind=StudioAsset.Kind.MUSIC, title=(PREFIX + mood)[:200],
                        duration=round(seconds + 0.5, 1), size=len(mp3), created_by=AUTHOR)
    asset.file.save(f'music_ai_{project.pk}.mp3', ContentFile(mp3), save=True)
    scn['music_asset'] = asset.pk
