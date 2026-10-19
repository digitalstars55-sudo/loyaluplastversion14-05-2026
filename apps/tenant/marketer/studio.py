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

from apps.tenant.marketer.models import (
    StudioAsset,
    StudioProject,
    StudioStyle,
    StudioStyleVersion,
    StudioVersion,
)

log = logging.getLogger(__name__)

FORMATS = ('9x16', '1x1', '16x9', '6s')
MOTIONS = ('zoom_in', 'zoom_out', 'pan_left', 'pan_right', 'none')
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
2. Если для ролика не хватает факта, без которого он врёт (цена, условие акции, дата), — НЕ пиши сценарий, задай ОДИН короткий вопрос: {"question": "..."}.
3. Используй только переданные материалы (по их id). Видео режь кусками: "start" — с какой секунды файла, "length" — сколько секунд в ролике. Фото — "start": 0.
4. Ритм склеек, титры, финал, цвета и запреты — из СТИЛЯ БРЕНДА. Правила «Запомнено для бренда» обязательны.
5. Титр — до 6 слов, без хэштегов и эмодзи. Не на каждой сцене.
6. Сумма "length" всех сцен = длине ролика (±0,5 с), не считая финала. Финал — отдельно, 2–3 с.
7. "motion" для фото: zoom_in, zoom_out, pan_left, pan_right; для видео — none.

Ответ — строго JSON без markdown, ОДИН из двух:
{"question": "что уточнить"}
или
{"scenario": {"title": "...", "scenes": [{"asset_id": 1, "start": 0.0, "length": 1.2, "caption": "", "motion": "none"}], "final": {"offer": "одно предложение", "address": "адрес"}, "music": "brand" | "none"}}"""

_EDIT_PROMPT = """Ты — режиссёр монтажа коротких роликов кафе. Тебе дают сценарий ролика (JSON) и правку владельца словами.
Внеси ТОЛЬКО эту правку, остальное оставь как было. Факты — только из базы знаний и прежнего сценария; новых цен не придумывай.
Если правка касается одной сцены (указан номер) — меняй только её.
Если правка похожа на постоянное правило бренда (размер логотипа, музыка, шрифт, цвет плашек, «всегда/никогда»), предложи запомнить её одной короткой фразой в "remember"; иначе "remember": "".
Если правку нельзя выполнить без факта, которого нет, — {"question": "..."}.

Ответ — строго JSON без markdown:
{"scenario": {...тот же вид...}, "changelog": ["что изменилось, коротко", "..."], "remember": ""}
или {"question": "..."}"""


def _assets_for(project: StudioProject) -> list[StudioAsset]:
    return list(StudioAsset.objects.filter(project=project).order_by('pk'))


def _asset_lines(assets) -> str:
    rows = []
    for a in assets:
        d = f', {a.duration:.1f} с' if a.duration else ''
        rows.append(f'- id {a.pk}: {a.get_kind_display().lower()}{d} — {a.title or "без подписи"}')
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
        })
    if not scenes:
        raise StudioError('в сценарии нет ни одной сцены из ваших материалов')
    final = raw.get('final') if isinstance(raw.get('final'), dict) else {}
    return {
        'title': str(raw.get('title') or '').strip()[:120],
        'duration': duration,
        'scenes': scenes,
        'final': {
            'offer': str(final.get('offer') or '').strip()[:120],
            'address': str(final.get('address') or address or '').strip()[:200],
            'length': 2.5,
        },
        'music': 'none' if raw.get('music') == 'none' else 'brand',
    }


def scenario_length(scn: dict) -> float:
    return round(sum(s['length'] for s in scn.get('scenes', [])) + (scn.get('final') or {}).get('length', 0), 2)


def prices_in(scn: dict) -> list[str]:
    texts = [s.get('caption', '') for s in scn.get('scenes', [])] + [(scn.get('final') or {}).get('offer', '')]
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


def plan(project: StudioProject) -> StudioProject:
    """Первый сценарий (или после ответа на вопрос). Итог — версия в очереди или вопрос."""
    style = style_obj()
    snap = snapshot(style)
    assets = _assets_for(project)
    kb = _kb_text()
    parts = [
        f'РОЛИК: {project.get_kind_display()}, длина {project.duration} с.',
        'ЗАДАНИЕ ВЛАДЕЛЬЦА:\n' + project.brief.strip()[:3000],
    ]
    if project.answer.strip():
        parts.append(f'ТЫ СПРАШИВАЛ: {project.question}\nОТВЕТ ВЛАДЕЛЬЦА: {project.answer.strip()[:1000]}')
    parts += ['МАТЕРИАЛЫ:\n' + _asset_lines(assets), 'СТИЛЬ БРЕНДА:\n' + _style_text(snap)]
    if kb.strip():
        parts.append('БАЗА ЗНАНИЙ СЕТИ (меню, цены, правила):\n' + kb)
    data = _ask_ai(_PLAN_PROMPT, '\n\n'.join(parts))
    if data.get('question') and not data.get('scenario'):
        return _set_question(project, str(data['question']))
    scn = clean_scenario(data.get('scenario') or {}, assets, project.duration, snap.get('address', ''))
    bad = unknown_prices(scn, kb, project.brief + ' ' + project.answer)
    if bad:
        return _set_question(project, 'В меню нет цены ' + ', '.join(_fmt_price(p) for p in bad)
                             + '. Какую цену поставить в ролик — или убрать её?')
    if not scn['title']:
        scn['title'] = project.title
    _new_version(project, scn, changelog=['Первая версия по заданию'])
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
        'СЦЕНАРИЙ:\n' + json.dumps(base.scenario, ensure_ascii=False),
        'ПРАВКА ВЛАДЕЛЬЦА' + (f' (только сцена {scene})' if scene else '') + ':\n' + instruction.strip()[:2000],
        'МАТЕРИАЛЫ:\n' + _asset_lines(assets),
        'СТИЛЬ БРЕНДА:\n' + _style_text(snap),
    ]
    if kb.strip():
        parts.append('БАЗА ЗНАНИЙ СЕТИ:\n' + kb)
    data = _ask_ai(_EDIT_PROMPT, '\n\n'.join(parts))
    if data.get('question') and not data.get('scenario'):
        return _set_question(project, str(data['question']))
    scn = clean_scenario(data.get('scenario') or {}, assets, project.duration, snap.get('address', ''))
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
