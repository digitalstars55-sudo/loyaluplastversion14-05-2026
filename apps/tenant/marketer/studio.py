"""
Студия видео AI-маркетолога (19.10.2026): ролик по заданию словами.

Как устроено:
  • задание + материалы сети → ИИ пишет СЦЕНАРИЙ из сцен (JSON), а не видео;
    если в задании не хватает факта (цены, предложения) — ИИ задаёт ОДИН
    вопрос, ролик ждёт ответа (статус `asking`);
  • цены в титрах сверяются с базой знаний сети: цены, которой нет в меню,
    в ролике быть не может — вместо сборки вопрос «такой цены нет в меню»;
  • каждая правка словами — новая версия со списком «что изменилось»; прежние
    версии остаются, вернуть — одна кнопка (готовые файлы у версии свои);
  • правка, похожая на правило бренда («логотип крупнее», «без музыки»), —
    ИИ предлагает «запомнить для бренда»; запомненное поднимает версию стиля,
    ролик помнит, по какой версии собран;
  • собирает отдельный процесс (`tools/studio-worker`, HyperFrames + FFmpeg):
    забирает версию из очереди через `studio_worker.py`, кладёт MP4 по
    форматам. Здесь только сценарий и состояние.
"""
from __future__ import annotations

import json
import logging
import re

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.tenant.marketer import studio_music
from apps.tenant.marketer.models import (
    StudioAsset,
    StudioProject,
    StudioStyle,
    StudioStyleVersion,
    StudioVersion,
    SEEN_BUSY, SEEN_FAILED,
)

log = logging.getLogger(__name__)

FORMATS = ('9x16', '1x1', '16x9', '6s')
MOTIONS = ('zoom_in', 'zoom_out', 'pan_left', 'pan_right', 'none')
# Монтаж «круче монтажника» (владелец 23.10.2026: «у него должны быть всевозможные скиллы»).
TRANSITIONS = ('cut', 'fade', 'zoom', 'slide_left', 'slide_up', 'whip', 'flash', 'blur')
CAPTION_STYLES = ('plate', 'pop', 'words', 'big')
CAPTION_POSITIONS = ('bottom', 'center', 'top')
GRADES = ('none', 'warm', 'cinematic', 'bright', 'vivid', 'bw')
MIN_SCENE = 0.6
MAX_SCENE = 8.0
MAX_SCENES = 24
MIN_DURATION = 6
MAX_DURATION = 60
_PRICE_RE = re.compile(r'(\d[\d\s ]{0,6}\d|\d)\s*(?:₽|р\.|руб)', re.IGNORECASE)


class StudioError(Exception):
    """ИИ ответил так, что сценарий собрать нельзя."""


# ── стиль бренда ─────────────────────────────────────────────────────────────

def style_obj() -> StudioStyle:
    style = StudioStyle.objects.order_by('pk').first()
    if style is None:
        style = StudioStyle.objects.create()
        StudioStyleVersion.objects.get_or_create(
            version=style.version, defaults={'snapshot': snapshot(style), 'note': 'Начальный стиль'})
    return style


def snapshot(style: StudioStyle) -> dict:
    """Стиль словами и именами файлов — то, что уходит в сценарий и сборщику."""
    return {
        'version': style.version,
        'palette': list(style.palette or []),
        'heading_font': style.heading_font,
        'body_font': style.body_font,
        'logo': style.logo.name if style.logo else '',
        'logo_dark': style.logo_dark.name if style.logo_dark else '',
        'music': style.music.name if style.music else '',
        'cut_pace': style.cut_pace,
        'captions': style.captions,
        'final_card': style.final_card,
        'forbidden': style.forbidden,
        'rules': [r.get('text', '') for r in (style.rules or []) if r.get('text')],
        'address': style.address,
    }


def bump(style: StudioStyle, note: str, by: str = '') -> StudioStyle:
    """Новая версия стиля: снимок кладётся рядом, ролики помнят свою."""
    with transaction.atomic():
        top = StudioStyleVersion.objects.aggregate(m=Max('version'))['m'] or 0
        style.version = max(style.version, top) + 1
        style.save()
        StudioStyleVersion.objects.create(version=style.version, snapshot=snapshot(style),
                                          note=note[:300], created_by=by[:150])
    return style


def remember(text: str, by: str = '') -> StudioStyle:
    """«Запомнить для бренда»: правило попадает во все следующие ролики."""
    text = (text or '').strip()[:300]
    if not text:
        raise ValueError('пустое правило')
    style = style_obj()
    rules = list(style.rules or [])
    if any((r.get('text') or '').strip().lower() == text.lower() for r in rules):
        return style
    rules.append({'text': text, 'version': style.version + 1, 'by': by[:150],
                  'at': timezone.now().isoformat()})
    style.rules = rules
    return bump(style, f'Запомнено: {text}', by)


def forget(index: int, by: str = '') -> StudioStyle:
    style = style_obj()
    rules = list(style.rules or [])
    if not 0 <= index < len(rules):
        raise ValueError('нет такого правила')
    gone = rules.pop(index)
    style.rules = rules
    return bump(style, f'Убрано правило: {gone.get("text", "")}', by)


def restore_style(version: int, by: str = '') -> StudioStyle:
    """Вернуть стиль к прежней версии (файлы логотипа и музыки — по именам из снимка)."""
    row = StudioStyleVersion.objects.filter(version=version).first()
    if row is None:
        raise ValueError('нет такой версии стиля')
    snap = row.snapshot or {}
    style = style_obj()
    for key in ('palette', 'heading_font', 'body_font', 'cut_pace', 'captions', 'final_card',
                'forbidden', 'address'):
        if key in snap:
            setattr(style, key, snap[key])
    for key in ('logo', 'logo_dark', 'music'):
        if key in snap:
            setattr(style, key, snap[key] or None)
    style.rules = [{'text': t, 'version': version, 'by': by[:150], 'at': timezone.now().isoformat()}
                   for t in snap.get('rules', [])]
    return bump(style, f'Возврат к версии {version}', by)


# ── сценарий ─────────────────────────────────────────────────────────────────

_PLAN_PROMPT = """Ты — режиссёр монтажа коротких роликов кафе и ресторанов для ВКонтакте, Телеграма и сторис.
Ты НЕ монтируешь сам: ты пишешь сценарий из сцен, по нему соберёт программа.

ЖЕЛЕЗНЫЕ ПРАВИЛА:
1. Факты (блюда, цены, акции, адреса, часы) — ТОЛЬКО из базы знаний сети и задания. Ничего не выдумывай.
2. Вопрос задавай ТОЛЬКО если без факта ролик соврёт (цена, условие акции, дата) — тогда НЕ пиши сценарий, задай ОДИН короткий вопрос: {"question": "..."}. Про логотип, музыку, шрифты, цвета, материалы и формат НЕ спрашивай никогда — работай с тем, что есть (нет логотипа — финал с названием сети).
3. Используй только переданные материалы (по их id). Видео режь кусками: "start" — с какой секунды файла, "length" — сколько секунд в ролике. Фото — "start": 0.
4. Ритм склеек, титры, финал, цвета и запреты — из СТИЛЯ БРЕНДА. Правила «Запомнено для бренда» обязательны.
5. Титр — до 6 слов, без хэштегов и эмодзи. Не на каждой сцене. Титр говорит о деле (блюдо, вкус, повод, предложение из базы знаний) — без пустых фраз вроде «Сделайте паузу».
8. Если у видео есть РАСКАДРОВКА (что на какой секунде) — бери лучшие моменты по ней, брак (размыто, темно, пальцы, пустой кадр) не бери. Первая сцена — самый аппетитный или неожиданный кадр.
9. Сцен столько, сколько даёт хороший материал: лучше 8 сильных, чем 20 случайных. Один и тот же кусок не повторяй.
10. Есть БРИФ РЕЖИССЁРА — выполняй его.
6. Сумма "length" всех сцен = длине ролика (±0,5 с), не считая финала. Финал — отдельно, 2–3 с.
7. "motion" — движение камеры по кадру: zoom_in, zoom_out, pan_left, pan_right, none. Для фото обязательно; для
   видео — лёгкий наезд оживляет статичный кадр, на динамичном видео — none.

МОНТАЖНЫЕ ПРИЁМЫ (ты — монтажёр уровня топовых Reels/TikTok, используй их со смыслом, не всё сразу):
- "transition" у сцены — как она входит: cut (резкая склейка, по умолчанию, держит ритм), fade (мягкое
  растворение — настроение, вкус), zoom (влёт с увеличением — акцент, «вау»), slide_left / slide_up (смена
  темы, «а ещё…»), whip (быстрый смаз — энергия), flash (белая вспышка — удар, раскрытие блюда), blur
  (размытие — мягкий переход времени). «Красивый переход» = разнообразные осмысленные переходы, не один и тот же.
- "caption_pos" у сцены: bottom / center / top — титр не должен закрывать главное в кадре (по раскадровке).
- "caption_style" ролика: plate (плашка снизу — спокойно), pop (выпрыгивает с отскоком — энергично),
  words (слова появляются по одному — кинетика), big (крупно по центру — хук и акценты).
- "grade" ролика — цвет: none, warm (тёплый, еда), cinematic (кино, контраст и виньетка), bright (светлый,
  кофейня), vivid (сочный), bw (чёрно-белый). Еду обычно warm или vivid.
- "sfx": true — звуки на переходах (вжух, удар) поверх музыки; для динамичного ролика.
- "music": "brand" — всегда, кроме прямой просьбы владельца «без музыки» ("none"). Нет трека бренда — программа
  сама сделает инструментал с лицензией по "music_mood": 8–20 слов ПО-АНГЛИЙСКИ — жанр, инструменты, темп
  (BPM под ритм склеек), настроение; без вокала. Пример: "warm acoustic pop, light claps, soft bass, 105 BPM, sunny".
- Хук: первые 1–1,5 с — самый сильный кадр, часто с zoom или flash и титром-интригой (big или pop).
- Ритм: склейки в такт — короткие 0,6–1,2 с в динамике, 2–3 с на «вкусном» кадре; финал не обрывай.

Ответ — строго JSON без markdown, ОДИН из двух:
{"question": "что уточнить"}
или
{"scenario": {"title": "...", "scenes": [{"asset_id": 1, "start": 0.0, "length": 1.2, "caption": "", "motion": "none", "transition": "cut", "caption_pos": "bottom"}], "final": {"brand": "название сети, как пишет сама сеть", "offer": "одно предложение", "address": "адрес"}, "music": "brand", "music_mood": "...", "caption_style": "plate", "grade": "warm", "sfx": false, "post": "текст поста к ролику для ВК и Телеграма: 200–600 знаков, по голосу бренда из базы знаний, те же факты, что в ролике"}}"""

_EDIT_PROMPT = """Ты — режиссёр монтажа коротких роликов кафе. Тебе дают сценарий ролика (JSON) и правку владельца словами.
Внеси ТОЛЬКО эту правку, остальное оставь как было. Факты — только из базы знаний и прежнего сценария; новых цен не придумывай.
Ты умеешь всё, что в сценарии: переход у каждой сцены ("transition": cut, fade, zoom, slide_left, slide_up, whip, flash,
blur), движение ("motion": zoom_in, zoom_out, pan_left, pan_right, none — и для видео), место титра ("caption_pos":
bottom, center, top), стиль титров ролика ("caption_style": plate, pop, words, big), цвет ("grade": none, warm,
cinematic, bright, vivid, bw), звуки на переходах ("sfx": true/false), музыку ("music": brand/none и
"music_mood" — настроение трека по-английски: жанр, инструменты, темп), порядок и длину сцен, финал.
«Красивые переходы / анимацию / динамичнее» — делай этими средствами, разнообразно и со смыслом. Не отказывай.
Если правка ОБЩАЯ («переделай», «нормально», «заново», «красиво», «динамичнее», «не нравится») — перестрой сценарий
целиком: лучшие моменты по раскадровке материалов, длина ролика из сценария, разный ритм сцен (короткие склейки
и 2–3 с на самом вкусном кадре), один кусок не повторяй, подпись — один раз, а не на каждой сцене.
Если правка касается одной сцены (указан номер) — меняй только её.
Если правка похожа на постоянное правило бренда (размер логотипа, музыка, шрифт, цвет плашек, «всегда/никогда»), предложи запомнить её одной короткой фразой в "remember"; иначе "remember": "".
Если правку нельзя выполнить без факта, которого нет, — {"question": "..."}.

Ответ — строго JSON без markdown:
{"scenario": {...тот же вид...}, "changelog": ["что изменилось, коротко", "..."], "remember": ""}
или {"question": "..."}"""


_DIRECTOR_PROMPT = """Ты — режиссёр коротких роликов кафе и ресторанов (ВКонтакте, Телеграм, сторис).
Владелец пишет идею в двух словах — ты превращаешь её в чёткий бриф для монтажа, чтобы вышел сильный ролик.
Опирайся только на материалы (их раскадровку) и базу знаний сети; факты (цены, акции) не выдумывай.
Бриф по-русски, 6–9 коротких строк:
Цель: что зритель должен почувствовать и сделать.
Хук (0–1,5 с): какой кадр первым (материал и секунды) и почему цепляет.
Сюжет: последовательность сцен по смыслу (какие материалы и моменты).
Ритм и переходы: где быстрее, где задержаться; эффекты без перегруза.
Титры: 2–4 титра по делу, о чём каждый.
Музыка и настроение.
Финал: предложение из базы знаний и призыв.
Пожелание владельца — главное: не спорь с ним, а доводи до лучшего исполнения."""


def direct(project: StudioProject, assets, snap: dict, kb: str) -> str:
    """Бриф режиссёра из идеи владельца (владелец 23.10.2026: «написал идею — ИИ обернул в промт, и вышла
    пушка»). Ошибка модели не роняет сценарий — он пишется по заданию как раньше."""
    from apps.tenant.marketer.generator import _ask
    parts = [f'РОЛИК: {project.get_kind_display()}, длина {project.duration} с.',
             'ИДЕЯ ВЛАДЕЛЬЦА:\n' + (project.brief.strip()[:3000] or '(нет — реши сам)'),
             'МАТЕРИАЛЫ:\n' + _asset_lines(assets), 'СТИЛЬ БРЕНДА:\n' + _style_text(snap)]
    if kb.strip():
        parts.append('БАЗА ЗНАНИЙ СЕТИ:\n' + kb)
    try:
        raw, _model = _ask(_DIRECTOR_PROMPT, '\n\n'.join(parts), 900)
    except Exception as e:  # noqa: BLE001
        log.warning('studio: бриф режиссёра не составлен: %s', e)
        return ''
    return (raw or '').strip()[:2500]


def improve(text: str, kind: str = '') -> str:
    """Кнопка «Улучшить»: идея словами → задание для ролика (без сборки)."""
    from apps.tenant.marketer.generator import _ask
    kb = _kb_text()
    msg = (f'ВИД: {kind or "ролик"}\nИДЕЯ ВЛАДЕЛЬЦА:\n{text.strip()[:2000]}'
           + (f'\n\nБАЗА ЗНАНИЙ СЕТИ:\n{kb}' if kb.strip() else ''))
    raw, _model = _ask(_DIRECTOR_PROMPT + '\nВерни только бриф, без вступлений.', msg, 900)
    return (raw or '').strip()[:2500]


_SEE_PROMPT = """Перед тобой лист кадров одного видео для ролика кафе: кадры слева направо, сверху вниз,
на каждом подписана секунда видео. Напиши раскадровку по-русски, строками «с–с: что в кадре» (крупность,
блюдо/люди/действие, движение камеры). Отметь брак: размыто, темно, пальцы на объективе, пустой кадр.
Последней строкой: «Лучшее: с–с, с–с» — самые аппетитные, живые или неожиданные моменты.
Не больше 12 строк, без вступлений."""


def describe_sheet(jpeg: bytes, times: list[float], duration: float) -> str:
    """Лист кадров от сборщика → раскадровка видео (модель со зрением)."""
    import base64
    import os
    from django.conf import settings
    from apps.shared.ai import openai_compat as anthropic
    from apps.shared.ai.usage import log_usage
    api_key = getattr(settings, 'OPENAI_API_KEY', None)
    if not api_key:
        raise RuntimeError('OPENAI_API_KEY не задан')
    base_url = os.getenv('OPENAI_BASE_URL', '')
    client = anthropic.Anthropic(api_key=api_key, base_url=base_url) if base_url else anthropic.Anthropic(api_key=api_key)
    from apps.tenant.marketer.generator import DEFAULT_MODEL
    подписи = ', '.join(f'{t:.1f}' for t in times)
    message = client.messages.create(
        model=getattr(settings, 'MARKETER_AI_MODEL', DEFAULT_MODEL), max_tokens=700, system=_SEE_PROMPT,
        messages=[{'role': 'user', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg',
                                         'data': base64.b64encode(jpeg).decode()}},
            {'type': 'text', 'text': f'Длина видео {duration:.1f} с. Секунды кадров по порядку: {подписи}.'},
        ]}])
    log_usage('marketer', message)
    текст = ''.join(getattr(b, 'text', '') for b in message.content if getattr(b, 'type', '') == 'text')
    return текст.strip()[:2000]


#: Сколько сценарий ждёт раскадровку новых видео от сборщика.
SEEN_WAIT_SECONDS = 120


def wait_seen(assets, timeout: float = SEEN_WAIT_SECONDS, tick: float = 3.0) -> list:
    """Ждёт, пока сборщик «посмотрит» новые видео. Не дождался — сценарий пишется вслепую, как раньше."""
    import time
    from django.conf import settings
    if not getattr(settings, 'STUDIO_WORKER_TOKEN', ''):
        return list(assets)            # сборщика нет — смотреть некому
    ids = [a.pk for a in assets if a.kind == StudioAsset.Kind.VIDEO and a.seen in ('', SEEN_BUSY)]
    end = time.monotonic() + timeout
    while ids and time.monotonic() < end:
        time.sleep(tick)
        ids = list(StudioAsset.objects.filter(pk__in=ids, seen__in=('', SEEN_BUSY)).values_list('pk', flat=True))
    return list(StudioAsset.objects.filter(pk__in=[a.pk for a in assets]).order_by('pk'))


def _assets_for(project: StudioProject) -> list[StudioAsset]:
    return list(StudioAsset.objects.filter(project=project).order_by('pk'))


def _asset_lines(assets) -> str:
    rows = []
    for a in assets:
        d = f', {a.duration:.1f} с' if a.duration else ''
        rows.append(f'- id {a.pk}: {a.get_kind_display().lower()}{d} — {a.title or "без подписи"}')
        seen = (a.seen or '').strip()
        if seen and seen not in (SEEN_BUSY, SEEN_FAILED):
            rows.append('  РАСКАДРОВКА:\n' + '\n'.join('    ' + r for r in seen.splitlines()))
    return '\n'.join(rows) or '- материалов нет: ролик только из титров на фоне цветов бренда'


def _style_text(snap: dict) -> str:
    rules = '\n'.join(f'  • {r}' for r in snap.get('rules') or []) or '  —'
    return (f"Цвета: {', '.join(snap.get('palette') or []) or 'по умолчанию'}\n"
            f"Шрифты: {snap.get('heading_font')} / {snap.get('body_font')}\n"
            f"Ритм склеек: {snap.get('cut_pace')}\n"
            f"Титры: {snap.get('captions')}\n"
            f"Финал: {snap.get('final_card')}\n"
            f"Адрес для финала: {snap.get('address') or 'нет'}\n"
            f"Нельзя:\n{snap.get('forbidden')}\n"
            f"Запомнено для бренда:\n{rules}")


def _kb_text() -> str:
    from apps.tenant.analytics.ai_service import get_knowledge_text
    from apps.tenant.marketer.generator import KB_LIMIT
    return (get_knowledge_text('content') or '')[:KB_LIMIT]


def _parse(raw: str) -> dict:
    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', raw, re.S)
        if not m:
            raise StudioError('ИИ ответил не сценарием')
        try:
            data = json.loads(m.group(0), strict=False)
        except json.JSONDecodeError as exc:
            raise StudioError('ИИ ответил не сценарием') from exc
    if not isinstance(data, dict):
        raise StudioError('ИИ ответил не сценарием')
    return data


def _num(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def clean_scenario(raw: dict, assets, duration: int, address: str = '') -> dict:
    """Сценарий от ИИ → проверенный: только свои материалы, длины в пределах, финал на месте."""
    by_id = {a.pk: a for a in assets}
    scenes = []
    for item in (raw.get('scenes') or [])[:MAX_SCENES]:
        if not isinstance(item, dict):
            continue
        aid = item.get('asset_id')
        try:
            aid = int(aid) if aid not in (None, '') else None
        except (TypeError, ValueError):
            aid = None
        asset = by_id.get(aid) if aid else None
        if aid and asset is None:
            continue  # чужой или выдуманный материал
        if asset is not None and asset.kind == StudioAsset.Kind.MUSIC:
            continue  # музыка — дорожка, а не кадр
        length = min(max(_num(item.get('length'), 1.0), MIN_SCENE), MAX_SCENE)
        start = max(_num(item.get('start'), 0.0), 0.0)
        if asset and asset.kind == StudioAsset.Kind.VIDEO and asset.duration:
            start = min(start, max(asset.duration - MIN_SCENE, 0.0))
            length = min(length, max(asset.duration - start, MIN_SCENE))
        if asset and asset.kind != StudioAsset.Kind.VIDEO:
            start = 0.0
        motion = item.get('motion') if item.get('motion') in MOTIONS else 'none'
        if asset and asset.kind == StudioAsset.Kind.PHOTO and motion == 'none':
            motion = 'zoom_in'
        scenes.append({
            'n': len(scenes) + 1,
            'asset_id': asset.pk if asset else None,
            'kind': asset.kind if asset else 'title',
            'start': round(start, 2),
            'length': round(length, 2),
            'caption': str(item.get('caption') or '').strip()[:80],
            'motion': motion if asset else 'none',
            'transition': item.get('transition') if item.get('transition') in TRANSITIONS else 'cut',
            'caption_pos': item.get('caption_pos') if item.get('caption_pos') in CAPTION_POSITIONS else 'bottom',
        })
    scenes = _merge_runs(scenes)
    if not scenes:
        raise StudioError('в сценарии нет ни одной сцены из ваших материалов')
    final = raw.get('final') if isinstance(raw.get('final'), dict) else {}
    return {
        'title': str(raw.get('title') or '').strip()[:120],
        'duration': duration,
        'scenes': scenes,
        'final': {
            'brand': str(final.get('brand') or '').strip()[:40],
            'offer': str(final.get('offer') or '').strip()[:120],
            'address': str(final.get('address') or address or '').strip()[:200],
            'length': 2.5,
        },
        'music': 'none' if raw.get('music') == 'none' else 'brand',
        'music_mood': re.sub(r'\s+', ' ', str(raw.get('music_mood') or '')).strip()[:200],
        'post': str(raw.get('post') or '').strip()[:1500],
        'caption_style': raw.get('caption_style') if raw.get('caption_style') in CAPTION_STYLES else 'plate',
        'grade': raw.get('grade') if raw.get('grade') in GRADES else 'none',
        'sfx': bool(raw.get('sfx')),
    }


def _merge_runs(scenes: list[dict]) -> list[dict]:
    """Куски одного видео подряд, встык и без перехода — одна сцена.

    24.10.2026: ИИ резал клип по секунде (2,1–3,1 и 3,1–4,1) и каждому куску давал ту же подпись —
    на каждой склейке подпись всплывала заново («каждый субтитр дважды»), а склейка внутри
    непрерывного кадра выглядела как сбой. Подпись остаётся первая непустая.
    """
    out: list[dict] = []
    for s in scenes:
        prev = out[-1] if out else None
        if (prev and s['asset_id'] and s['asset_id'] == prev['asset_id'] and s['kind'] == 'video'
                and s['transition'] == 'cut' and abs(prev['start'] + prev['length'] - s['start']) <= 0.15
                and (not s['caption'] or not prev['caption'] or s['caption'] == prev['caption'])
                and prev['length'] + s['length'] <= MAX_SCENE):
            prev['length'] = round(s['start'] + s['length'] - prev['start'], 2)
            prev['caption'] = prev['caption'] or s['caption']
            if prev['motion'] == 'none':
                prev['motion'] = s['motion']
            continue
        out.append(dict(s))
    for i, s in enumerate(out, 1):
        s['n'] = i
    return out


def _занято(scenes: list[dict], asset_id) -> list[tuple[float, float]]:
    return sorted((x['start'], x['start'] + x['length']) for x in scenes
                  if x['asset_id'] == asset_id and x['kind'] == 'video')


def _без_повторов(scenes: list[dict], by_id: dict) -> list[dict]:
    """Кусок, который уже был в ролике, — на свободное место того же видео; места нет — сцену убираем.

    24.10.2026: модель после переделки всё равно показывала коробку дважды (3,85–7,35 и 3,9–4,7 с).
    """
    out: list[dict] = []
    for s in scenes:
        if s['kind'] != 'video' or not s['asset_id']:
            out.append(s)
            continue
        было = _занято(out, s['asset_id'])
        a0, a1 = s['start'], s['start'] + s['length']
        for b0, b1 in было:            # краешек чужого куска — обрезаем (живой прогон: общие 0,9 с)
            if b0 <= a0 < b1 <= a1 - MIN_SCENE:
                a0 = b1
            elif a0 + MIN_SCENE <= b0 < a1 <= b1:
                a1 = b0
        if not any(min(a1, b1) - max(a0, b0) > 0.05 for b0, b1 in было):
            out.append(dict(s, start=round(a0, 2), length=round(a1 - a0, 2)))
            continue
        конец = float(getattr(by_id.get(s['asset_id']), 'duration', 0) or 0)
        место, t = None, 0.0
        for b0, b1 in было + [(конец, конец)]:
            if b0 - t >= s['length']:
                место = t
                break
            t = max(t, b1)
        if место is not None:
            out.append(dict(s, start=round(место, 2)))
    for i, s in enumerate(out, 1):
        s['n'] = i
    return out


def _дотянуть(scenes: list[dict], by_id: dict, duration: int) -> list[dict]:
    """Не хватает длины до заказанной — дотягиваем сцены, где у материала есть запас (без новых повторов)."""
    нехватка = duration - sum(float(s['length']) for s in scenes)
    for _ in range(4):
        if нехватка <= 0.5 or not scenes:
            break
        запас = {}
        for s in scenes:
            if s['kind'] == 'video':
                конец = float(getattr(by_id.get(s['asset_id']), 'duration', 0) or 0)
                дальше = min([b0 for b0, _b1 in _занято(scenes, s['asset_id']) if b0 > s['start'] + 0.01]
                             + [конец])
                запас[s['n']] = max(min(дальше - s['start'] - s['length'], MAX_SCENE - s['length']), 0.0)
            else:
                запас[s['n']] = max(MAX_SCENE - s['length'], 0.0)
        всего = sum(запас.values())
        if всего < 0.05:
            break
        доля = min(нехватка / всего, 1.0)
        for s in scenes:
            прибавка = round(запас[s['n']] * доля, 2)
            s['length'] = round(s['length'] + прибавка, 2)
            нехватка -= прибавка
    return scenes


def досборка(scn: dict, assets, duration: int) -> dict:
    """Сценарий целиком: убрать повторы кусков и добрать длину до заказанной — без лишнего вызова модели."""
    by_id = {a.pk: a for a in assets}
    scn['scenes'] = _дотянуть(_без_повторов(scn.get('scenes') or [], by_id), by_id, duration)
    return scn


def замечания(scn: dict, duration: int) -> list[str]:
    """Чем сценарий плох как монтаж — для одной переделки моделью (24.10.2026).

    Проект 2 LevOne: 20 сцен ровно по 1,0 с вперемешку из двух клипов, сумма 9 с при заказанных 20 —
    «склейки ради склеек»; правка «переделай нормально» эту нарезку сохранила.
    """
    out = []
    scenes = scn.get('scenes') or []
    видео = [x for x in scenes if x.get('kind') == 'video']
    сумма = sum(float(x['length']) for x in scenes)
    if duration and (сумма < duration * 0.75 or сумма > duration * 1.25):
        out.append(f'сумма длин сцен {сумма:.1f} с, а ролик должен быть {duration} с (±0,5 с)')
    if len(видео) >= 4:
        длины = [round(float(x['length']), 1) for x in видео]
        if max(длины) - min(длины) < 0.25:
            out.append(f'все сцены одной длины ({длины[0]} с) — нет ритма: короткие склейки в динамике, '
                       '2–3 с на самом вкусном кадре')
    for i, x in enumerate(видео):
        for y in видео[i + 1:]:
            if x['asset_id'] != y['asset_id']:
                continue
            a0, a1 = x['start'], x['start'] + x['length']
            b0, b1 = y['start'], y['start'] + y['length']
            if min(a1, b1) - max(a0, b0) > 0.5 * min(x['length'], y['length']):
                out.append(f'сцены {x["n"]} и {y["n"]} показывают один и тот же кусок материала {x["asset_id"]}')
                break
    подряд = [x['n'] for p, x in zip(scenes, scenes[1:]) if x.get('caption') and x.get('caption') == p.get('caption')]
    if подряд:
        out.append('одна и та же подпись на соседних сценах ' + ', '.join(map(str, подряд)) + ' — ставь её один раз')
    return out


def _дожать(system: str, message: str, scn: dict, data: dict, assets, duration: int, address: str) -> tuple[dict, dict]:
    """Один повтор у модели с перечнем ошибок сценария. Хуже не стал — берём новый."""
    плохо = замечания(scn, duration)
    if not плохо:
        return scn, data
    повтор = _ask_ai(system, message + '\n\nТВОЙ ПРЕЖНИЙ ОТВЕТ:\n' + json.dumps({'scenario': scn}, ensure_ascii=False)
                     + '\n\nОШИБКИ МОНТАЖА — исправь их и пришли сценарий заново целиком:\n- ' + '\n- '.join(плохо))
    if not повтор.get('scenario'):
        return scn, data
    try:
        новый = clean_scenario(повтор['scenario'], assets, duration, address)
    except StudioError:
        return scn, data
    if len(замечания(новый, duration)) <= len(плохо):
        return новый, повтор
    return scn, data


def scenario_length(scn: dict) -> float:
    return round(sum(s['length'] for s in scn.get('scenes', [])) + (scn.get('final') or {}).get('length', 0), 2)


def prices_in(scn: dict) -> list[str]:
    texts = ([s.get('caption', '') for s in scn.get('scenes', [])]
             + [(scn.get('final') or {}).get('offer', ''), scn.get('post', '')])
    found = []
    for t in texts:
        for m in _PRICE_RE.finditer(t or ''):
            digits = re.sub(r'\D', '', m.group(1))
            if digits and digits not in found:
                found.append(digits)
    return found


def unknown_prices(scn: dict, kb_text: str, extra: str = '') -> list[str]:
    """Цены из титров, которых нет ни в базе знаний, ни в задании и ответе владельца."""
    pool = re.sub(r'[\s ]', '', (kb_text or '') + ' ' + (extra or ''))
    return [p for p in prices_in(scn) if p not in pool]


def _fmt_price(digits: str) -> str:
    return f'{int(digits):,}'.replace(',', ' ') + ' ₽'


def _new_version(project: StudioProject, scn: dict, *, instruction: str = '', scene=None,
                 changelog=None, remember: str = '') -> StudioVersion:
    style = style_obj()
    with transaction.atomic():
        top = project.versions.aggregate(m=Max('number'))['m'] or 0
        version = StudioVersion.objects.create(
            project=project, number=top + 1, instruction=instruction[:2000], scene=scene,
            scenario=scn, changelog=list(changelog or [])[:12], style_version=style.version,
            style_snapshot=snapshot(style), remember_suggestion=(remember or '')[:300],
            stage='В очереди на сборку')
        project.current_version = version
        project.status = StudioProject.Status.QUEUED
        project.style_version = style.version
        project.question = ''
        project.error = ''
        if scn.get('post') and not project.post_text:
            project.post_text = scn['post']
        if project.approval == StudioProject.Approval.APPROVED:
            project.approval = StudioProject.Approval.NONE  # новая версия — согласовать заново
            project.approved_by = ''
            project.approved_at = None
        project.save()
    return version


def _ask_ai(system: str, message: str) -> dict:
    from apps.tenant.marketer.generator import _ask
    raw, _model = _ask(system, message, 2500)
    return _parse(raw)


def _set_question(project: StudioProject, question: str) -> StudioProject:
    project.question = question.strip()[:1000]
    project.status = StudioProject.Status.ASKING
    project.error = ''
    project.save(update_fields=['question', 'status', 'error', 'updated_at'])
    return project


def _остановлен(project: StudioProject, было: str) -> bool:
    """Пока ИИ думал, нажали «Остановить» (22.10.2026) — ответ ИИ не применяем.
    Признак — статус ролика сменился за время ответа (остановка ставит готово/ошибку)."""
    status = StudioProject.objects.filter(pk=project.pk).values_list('status', flat=True).first()
    if status != было:
        project.refresh_from_db()
        return True
    return False


def plan(project: StudioProject) -> StudioProject:
    """Первый сценарий (или после ответа на вопрос). Итог — версия в очереди или вопрос."""
    style = style_obj()
    snap = snapshot(style)
    assets = wait_seen(_assets_for(project))
    kb = _kb_text()
    бриф = direct(project, assets, snap, kb)
    parts = [
        f'РОЛИК: {project.get_kind_display()}, длина {project.duration} с.',
        'ЗАДАНИЕ ВЛАДЕЛЬЦА:\n' + project.brief.strip()[:3000],
    ]
    if бриф:
        parts.append('БРИФ РЕЖИССЁРА:\n' + бриф)
    if project.answer.strip():
        parts.append(f'ТЫ СПРАШИВАЛ: {project.question}\nОТВЕТ ВЛАДЕЛЬЦА: {project.answer.strip()[:1000]}')
    parts += ['МАТЕРИАЛЫ:\n' + _asset_lines(assets), 'СТИЛЬ БРЕНДА:\n' + _style_text(snap)]
    if kb.strip():
        parts.append('БАЗА ЗНАНИЙ СЕТИ (меню, цены, правила):\n' + kb)
    было = project.status
    data = _ask_ai(_PLAN_PROMPT, '\n\n'.join(parts))
    if _остановлен(project, было):
        return project
    if data.get('question') and not data.get('scenario'):
        return _set_question(project, str(data['question']))
    scn = clean_scenario(data.get('scenario') or {}, assets, project.duration, snap.get('address', ''))
    scn, data = _дожать(_PLAN_PROMPT, '\n\n'.join(parts), scn, data, assets, project.duration, snap.get('address', ''))
    scn = досборка(scn, assets, project.duration)
    if _остановлен(project, было):
        return project
    bad = unknown_prices(scn, kb, project.brief + ' ' + project.answer)
    if bad:
        return _set_question(project, 'В меню нет цены ' + ', '.join(_fmt_price(p) for p in bad)
                             + '. Какую цену поставить в ролик — или убрать её?')
    if not scn['title']:
        scn['title'] = project.title
    if бриф:
        scn['director'] = бриф
    studio_music.подобрать(project, scn, studio_music.своя_музыка(project, snap))
    if _остановлен(project, было):
        return project
    _new_version(project, scn, changelog=['Первая версия по брифу режиссёра' if бриф else 'Первая версия по заданию'])
    return project


def edit(project: StudioProject, instruction: str, scene: int | None = None, by: str = '') -> StudioProject:
    """Правка словами → новая версия (прежняя остаётся)."""
    base = project.current_version or project.versions.order_by('-number').first()
    if base is None:
        raise StudioError('у ролика ещё нет версии — сначала сценарий')
    assets = _assets_for(project)
    snap = snapshot(style_obj())
    kb = _kb_text()
    parts = [
        'СЦЕНАРИЙ:\n' + json.dumps({k: v for k, v in base.scenario.items() if k != 'director'}, ensure_ascii=False),
        *(['БРИФ РЕЖИССЁРА (держись его, если правка не говорит иное):\n' + base.scenario['director']]
          if base.scenario.get('director') else []),
        'ПРАВКА ВЛАДЕЛЬЦА' + (f' (только сцена {scene})' if scene else '') + ':\n' + instruction.strip()[:2000],
        'МАТЕРИАЛЫ:\n' + _asset_lines(assets),
        'СТИЛЬ БРЕНДА:\n' + _style_text(snap),
    ]
    if kb.strip():
        parts.append('БАЗА ЗНАНИЙ СЕТИ:\n' + kb)
    было = project.status
    data = _ask_ai(_EDIT_PROMPT, '\n\n'.join(parts))
    if _остановлен(project, было):
        return project
    if data.get('question') and not data.get('scenario'):
        return _set_question(project, str(data['question']))
    scn = clean_scenario(data.get('scenario') or {}, assets, project.duration, snap.get('address', ''))
    if not scene:
        scn, data = _дожать(_EDIT_PROMPT, '\n\n'.join(parts), scn, data, assets, project.duration,
                            snap.get('address', ''))
        scn = досборка(scn, assets, project.duration)
        if _остановлен(project, было):
            return project
    if scene and 1 <= scene <= len(base.scenario.get('scenes', [])):
        # Правка одной сцены: остальные — ровно как были, что бы ни прислал ИИ.
        old = [dict(s) for s in base.scenario.get('scenes', [])]
        new_scene = next((s for s in scn['scenes'] if s['n'] == scene), None)
        if new_scene is not None:
            old[scene - 1] = dict(new_scene, n=scene)
        scn['scenes'] = old
        scn['final'] = base.scenario.get('final', scn['final'])
    bad = unknown_prices(scn, kb, project.brief + ' ' + project.answer + ' ' + instruction)
    if bad:
        return _set_question(project, 'В меню нет цены ' + ', '.join(_fmt_price(p) for p in bad)
                             + '. Какую цену поставить в ролик — или убрать её?')
    scn['title'] = scn['title'] or base.scenario.get('title') or project.title
    if base.scenario.get('director'):
        scn['director'] = base.scenario['director']
    if scene:
        for key in ('music', 'music_mood'):
            scn[key] = base.scenario.get(key, scn.get(key))
    studio_music.подобрать(project, scn, studio_music.своя_музыка(project, snap), previous=base.scenario)
    changelog = [str(c).strip()[:200] for c in (data.get('changelog') or []) if str(c).strip()]
    _new_version(project, scn, instruction=instruction, scene=scene,
                 changelog=changelog or [instruction.strip()[:200]],
                 remember=str(data.get('remember') or '').strip())
    return project


def restore(project: StudioProject, number: int) -> StudioVersion:
    """Вернуть прежнюю версию: её файлы готовы, пересборки не нужно."""
    version = project.versions.filter(number=number).first()
    if version is None:
        raise ValueError('нет такой версии')
    project.current_version = version
    project.status = {
        StudioVersion.Status.READY: StudioProject.Status.READY,
        StudioVersion.Status.FAILED: StudioProject.Status.FAILED,
        StudioVersion.Status.RENDERING: StudioProject.Status.RENDERING,
    }.get(version.status, StudioProject.Status.QUEUED)
    project.save(update_fields=['current_version', 'status', 'updated_at'])
    return version


def rerender(project: StudioProject) -> StudioVersion:
    """Собрать текущий сценарий заново (упало у сборщика) — новой версией."""
    base = project.current_version
    if base is None:
        raise ValueError('у ролика нет версии')
    return _new_version(project, base.scenario, instruction='Собрать заново',
                        changelog=['Сборка заново, сценарий тот же'])
