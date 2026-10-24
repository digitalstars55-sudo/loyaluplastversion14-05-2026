"""
Сценарий студии → композиция HyperFrames (один index.html на формат).

Без сети и без случайности: одна и та же версия сценария даёт один и тот же
кадр. Видео — клипы с `data-media-start` (кусок файла), фото — медленный наезд
или проезд, титры — на плашке снизу (не закрывают лицо и блюдо), финал —
логотип, одно предложение и адрес. Музыка — отдельная дорожка с затуханием.

Формат «6s» — короткая сторис: первые сцены до ~4,5 с и финал 1,5 с.
"""
from __future__ import annotations

import html
import json

FORMATS = {
    '9x16': (1080, 1920),
    '1x1': (1080, 1080),
    '16x9': (1920, 1080),
    '6s': (1080, 1920),
}
SHORT_BODY = 4.5
SHORT_FINAL = 1.5
DEFAULT_PALETTE = ['#1B2838', '#C9A227', '#F6F3EA']


def _hex(c: str) -> str:
    c = c.strip()
    if len(c) == 4:  # #abc → #aabbcc, чтобы дописывать прозрачность
        c = '#' + ''.join(ch * 2 for ch in c[1:])
    return c.upper()


def _pal(style: dict) -> list[str]:
    pal = [_hex(c) for c in (style.get('palette') or [])
           if isinstance(c, str) and c.startswith('#') and len(c.strip()) in (4, 7)]
    return (pal + DEFAULT_PALETTE[len(pal):])[:3]


def timeline(scenario: dict, fmt: str) -> tuple[list[dict], float, float]:
    """Сцены с абсолютным временем, начало финала и общая длина."""
    scenes = [dict(s) for s in scenario.get('scenes') or []]
    final_len = float((scenario.get('final') or {}).get('length') or 2.5)
    if fmt == '6s':
        out, t = [], 0.0
        for s in scenes:
            if t >= SHORT_BODY - 0.3:
                break
            s['length'] = round(min(float(s['length']), SHORT_BODY - t), 2)
            out.append(s)
            t += s['length']
        scenes, final_len = out, SHORT_FINAL
    t = 0.0
    for s in scenes:
        s['at'] = round(t, 2)
        t += float(s['length'])
    return scenes, round(t, 2), round(t + final_len, 2)


#: Длительность перехода (с): входящая сцена наезжает на предыдущую, та держится под ней.
TRANSITION_LEN = {'fade': 0.45, 'zoom': 0.45, 'slide_left': 0.4, 'slide_up': 0.4, 'whip': 0.3,
                  'flash': 0.3, 'blur': 0.45}
#: Цвет ролика — CSS-фильтр на слое картинки (титры не трогает).
GRADES = {
    'warm': 'saturate(1.15) sepia(0.12) contrast(1.05)',
    'cinematic': 'contrast(1.12) saturate(0.88) brightness(0.97)',
    'bright': 'brightness(1.07) saturate(1.12)',
    'vivid': 'saturate(1.35) contrast(1.06)',
    'bw': 'grayscale(1) contrast(1.1)',
}


def _tw(sel: str, frm: dict, to: dict, at: float) -> str:
    return f'tl.fromTo("{sel}", {json.dumps(frm)}, {json.dumps(to)}, {at:.2f});'


def _transition(sel: str, prev_sel: str | None, kind: str, at: float) -> list[str]:
    """Вход сцены `sel` в момент `at`. Перекрытие: предыдущая сцена продлена на длину перехода."""
    d = TRANSITION_LEN.get(kind)
    if not d:
        return []
    if kind == 'fade':
        return [_tw(sel, {'opacity': 0}, {'opacity': 1, 'duration': d, 'ease': 'power1.inOut'}, at)]
    if kind == 'zoom':
        return [_tw(sel, {'opacity': 0, 'scale': 1.35}, {'opacity': 1, 'scale': 1, 'duration': d,
                                                         'ease': 'power3.out'}, at)]
    if kind == 'slide_left':
        return [_tw(sel, {'xPercent': 100}, {'xPercent': 0, 'duration': d, 'ease': 'power3.inOut'}, at)]
    if kind == 'slide_up':
        return [_tw(sel, {'yPercent': 100}, {'yPercent': 0, 'duration': d, 'ease': 'power3.inOut'}, at)]
    if kind == 'whip':
        out = [_tw(sel, {'xPercent': 100, 'filter': 'blur(18px)'},
                   {'xPercent': 0, 'filter': 'blur(0px)', 'duration': d, 'ease': 'power4.out'}, at)]
        if prev_sel:
            out.append(_tw(prev_sel, {'xPercent': 0, 'filter': 'blur(0px)'},
                           {'xPercent': -100, 'filter': 'blur(18px)', 'duration': d, 'ease': 'power4.in'}, at))
        return out
    if kind == 'blur':
        return [_tw(sel, {'opacity': 0, 'filter': 'blur(24px)'},
                    {'opacity': 1, 'filter': 'blur(0px)', 'duration': d, 'ease': 'power2.out'}, at)]
    return []  # flash — белым слоем отдельно


def _caption(n: int, cap: str, style: str, pos: str, at: float, ln: float) -> tuple[str, list[str]]:
    """Титр сцены: plate — плашка, pop — с отскоком, words — по слову, big — крупно по центру."""
    pos = pos if pos in ('bottom', 'center', 'top') else 'bottom'
    if style == 'big':
        pos = 'center' if pos == 'bottom' else pos
    cls = f'clip cap cap-{pos} cap-{style}'
    head = f'<div id="c{n}" class="{cls}" data-start="{at}" data-duration="{ln}" data-track-index="2">'
    if style == 'words':
        слова = ''.join(f'<span class="w" id="c{n}-w{i}">{html.escape(w)}</span> '
                        for i, w in enumerate(cap.split()))
        return (head + f'<span id="c{n}-p" class="words">{слова}</span></div>',
                [f'tl.fromTo("#c{n} .w", {{"y": 36, "opacity": 0}}, {{"y": 0, "opacity": 1, "duration": 0.28, '
                 f'"ease": "back.out(2)", "stagger": 0.09}}, {at + 0.05:.2f});'])
    body = head + f'<span id="c{n}-p" class="plate">{html.escape(cap)}</span></div>'
    if style == 'pop':
        return body, [_tw(f'#c{n}-p', {'scale': 0.4, 'opacity': 0},
                          {'scale': 1, 'opacity': 1, 'duration': 0.42, 'ease': 'back.out(2.6)'}, at + 0.05)]
    if style == 'big':
        return body, [_tw(f'#c{n}-p', {'scale': 1.5, 'opacity': 0},
                          {'scale': 1, 'opacity': 1, 'duration': 0.35, 'ease': 'power4.out'}, at + 0.03)]
    return body, [_tw(f'#c{n}-p', {'y': 24, 'opacity': 0},
                      {'y': 0, 'opacity': 1, 'duration': 0.3, 'ease': 'power2.out'}, at + 0.05)]


def build(job: dict, fmt: str, files: dict) -> str:
    """HTML композиции. files: {'asset-<id>': 'assets/…', 'logo': …, 'music': …} — пути внутри папки."""
    w, h = FORMATS[fmt]
    style = job.get('style') or {}
    bg, accent, light = _pal(style)
    scn = job.get('scenario') or {}
    scenes, final_at, total = timeline(scn, fmt)
    final = scn.get('final') or {}
    unit = min(w, h)
    cap_size = round(unit * (0.058 if fmt != '16x9' else 0.05))
    offer_size = round(unit * 0.075)
    addr_size = round(unit * 0.04)
    cap_bottom = '17%' if h > w else '9%'

    cap_style = scn.get('caption_style') or 'plate'
    grade = GRADES.get(scn.get('grade') or '', '')
    sfx = bool(scn.get('sfx')) and files.get('sfx_whoosh')
    media, caps, overlays, tweens, sounds = [], [], [], [], []
    prev_sel = None
    for i, s in enumerate(scenes):
        n, at, ln = s['n'], s['at'], float(s['length'])
        nxt = scenes[i + 1] if i + 1 < len(scenes) else None
        # Переход следующей сцены наезжает на эту — эта держится под ним.
        tail = TRANSITION_LEN.get((nxt or {}).get('transition') or 'cut', 0) if nxt else 0
        shown = round(ln + tail, 2)
        tr = s.get('transition') or 'cut'
        motion = s.get('motion') or ('zoom_in' if s.get('kind') == 'photo' else 'none')
        key = f"asset-{s.get('asset_id')}" if s.get('asset_id') else ''
        src = files.get(key, '')
        pan = round(w * 0.03)
        moves = {
            'zoom_in': ({'scale': 1.0}, {'scale': 1.12}),
            'zoom_out': ({'scale': 1.12}, {'scale': 1.0}),
            'pan_left': ({'scale': 1.12, 'x': pan}, {'scale': 1.12, 'x': -pan}),
            'pan_right': ({'scale': 1.12, 'x': -pan}, {'scale': 1.12, 'x': pan}),
        }
        if s.get('kind') == 'video' and src:
            sel = f'#s{n}'
            media.append(
                f'<video id="s{n}" class="clip media" src="{html.escape(src)}" muted playsinline '
                f'data-start="{at}" data-duration="{shown}" data-media-start="{float(s.get("start") or 0)}" '
                f'data-track-index="0"></video>')
            move_sel = sel
        elif s.get('kind') == 'photo' and src:
            sel = f'#s{n}'
            media.append(
                f'<div id="s{n}" class="clip" data-start="{at}" data-duration="{shown}" data-track-index="0">'
                f'<img id="s{n}-img" class="photo" src="{html.escape(src)}" alt=""></div>')
            move_sel = f'#s{n}-img'
        else:
            sel = f'#s{n}'
            media.append(
                f'<div id="s{n}" class="clip title" data-start="{at}" data-duration="{shown}" data-track-index="0">'
                f'<div id="s{n}-t" class="title-text">{html.escape(s.get("caption") or "")}</div></div>')
            tweens.append(_tw(f'#s{n}-t', {'y': 40, 'opacity': 0},
                              {'y': 0, 'opacity': 1, 'duration': 0.45, 'ease': 'power3.out'}, at))
            move_sel = None
        if i > 0:
            tweens += _transition(sel, prev_sel, tr, at)
            if tr == 'flash':
                overlays.append(f'<div id="fl{n}" class="clip flash" data-start="{max(at - 0.06, 0):.2f}" '
                                f'data-duration="0.4" data-track-index="5"><div id="fl{n}-in" class="flash-in"></div></div>')
                tweens.append(_tw(f'#fl{n}-in', {'opacity': 1}, {'opacity': 0, 'duration': 0.36, 'ease': 'power2.out'},
                                  max(at - 0.06, 0)))
            if sfx and tr != 'cut':
                звук = files.get('sfx_hit') if tr in ('flash', 'zoom') else files.get('sfx_whoosh')
                sounds.append(f'<audio id="sfx{n}" src="{html.escape(звук)}" data-start="{max(at - 0.12, 0):.2f}" '
                              f'data-duration="0.6" data-track-index="4" data-volume="0.55"></audio>')
        if move_sel and motion in moves:
            frm, to = moves[motion]
            # Наезд-переход и движение спорят за scale: движение стартует после перехода.
            d0 = TRANSITION_LEN.get(tr, 0) if (tr == 'zoom' and move_sel == sel and i > 0) else 0
            tweens.append(_tw(move_sel, frm, dict(to, duration=round(max(shown - d0, 0.3), 2), ease='none'), at + d0))
        cap = (s.get('caption') or '').strip()
        prev_cap = ((scenes[i - 1].get('caption') or '').strip() if i > 0 else '')
        if cap and cap == prev_cap:
            cap = ''  # та же подпись, что у прошлой сцены, — держится одним титром (24.10: «всплывает дважды»)
        if cap and s.get('kind') in ('video', 'photo') and src:
            # Одна подпись на несколько сцен подряд — титр идёт до конца последней из них.
            span = ln
            for nx in scenes[i + 1:]:
                if (nx.get('caption') or '').strip() != cap:
                    break
                span += float(nx['length'])
            html_cap, tw = _caption(n, cap, cap_style, s.get('caption_pos') or 'bottom', at, round(span, 2))
            caps.append(html_cap)
            tweens += tw
        prev_sel = sel

    body = [f'<div class="grade">{chr(10).join(media)}</div>']
    if (scn.get('grade') or '') == 'cinematic':
        body.append('<div class="vignette"></div>')
    body += overlays + caps + sounds
    logo = files.get('logo_dark') or files.get('logo') or ''
    final_len = round(total - final_at, 2)
    parts = []
    if logo:
        parts.append(f'<img id="f-logo" class="logo" src="{html.escape(logo)}" alt="">')
    if final.get('offer'):
        parts.append(f'<div id="f-offer" class="offer">{html.escape(final["offer"])}</div>')
    if final.get('address'):
        parts.append(f'<div id="f-addr" class="addr">{html.escape(final["address"])}</div>')
    body.append(f'<div id="final" class="clip final" data-start="{final_at}" data-duration="{final_len}" '
                f'data-track-index="1"><div class="final-in">{"".join(parts)}</div></div>')
    for i, el in enumerate(('f-logo', 'f-offer', 'f-addr')):
        if any(f'id="{el}"' in p for p in parts):
            tweens.append(f'tl.fromTo("#{el}", {{"y": 30, "opacity": 0}}, '
                          f'{{"y": 0, "opacity": 1, "duration": 0.4, "ease": "power3.out"}}, '
                          f'{final_at + 0.1 + i * 0.15:.2f});')

    music = files.get('music') if scn.get('music') != 'none' else ''
    if music:
        fade_from = max(total - 1.0, 0.1)
        auto = json.dumps({'version': 1, 'lanes': [{'target': 'volume', 'points': [
            {'t': 0, 'v': 0}, {'t': 0.4, 'v': 1}, {'t': round(fade_from, 2), 'v': 1}, {'t': total, 'v': 0}]}]})
        body.append(f'<audio id="music" src="{html.escape(music)}" data-start="0" data-duration="{total}" '
                    f'data-track-index="3" data-volume="0.6" data-automation=\'{auto}\'></audio>')

    css = f"""
@font-face {{ font-family: "Brand"; src: url("fonts/Manrope.ttf") format("truetype"); font-weight: 200 800; }}
body {{ margin: 0; background: {bg}; }}
#root {{ position: relative; width: 100%; height: 100%; overflow: hidden; background: {bg};
        font-family: "Brand", sans-serif; }}
.clip {{ position: absolute; inset: 0; overflow: hidden; }}
.media {{ width: 100%; height: 100%; object-fit: cover; }}
.photo {{ display: block; width: 100%; height: 100%; object-fit: cover; }}
.title {{ display: flex; align-items: center; justify-content: center; background: {bg}; }}
.title-text {{ max-width: 80%; color: {light}; font-size: {offer_size}px; font-weight: 800; line-height: 1.12;
              text-align: center; text-wrap: balance; }}
.cap {{ display: flex; align-items: flex-end; justify-content: center; padding-bottom: {cap_bottom};
       box-sizing: border-box; }}
.plate {{ display: block; max-width: 84%; padding: {round(cap_size * 0.35)}px {round(cap_size * 0.6)}px;
         border-radius: {round(cap_size * 0.35)}px; background: {bg}e6; color: {light};
         font-size: {cap_size}px; font-weight: 700; line-height: 1.18; text-align: center;
         text-wrap: balance; border-bottom: {max(4, round(cap_size * 0.08))}px solid {accent}; }}
.grade {{ position: absolute; inset: 0; {('filter: ' + grade + ';') if grade else ''} }}
.vignette {{ position: absolute; inset: 0; pointer-events: none;
            background: radial-gradient(ellipse at center, transparent 55%, rgba(0,0,0,0.45) 100%); }}
.flash {{ pointer-events: none; }}
.flash-in {{ position: absolute; inset: 0; background: #fff; }}
.cap-center {{ align-items: center; padding-bottom: 0; }}
.cap-top {{ align-items: flex-start; padding-bottom: 0; padding-top: {cap_bottom}; }}
.cap-big .plate {{ background: transparent; border-bottom: 0; font-size: {round(cap_size * 1.7)}px;
                  font-weight: 800; text-shadow: 0 4px 24px rgba(0,0,0,0.55); }}
.cap-pop .plate {{ background: {accent}; color: {bg}; border-bottom: 0; }}
.words {{ display: block; max-width: 86%; text-align: center; color: #fff; font-size: {round(cap_size * 1.25)}px;
         font-weight: 800; line-height: 1.15; text-shadow: 0 3px 18px rgba(0,0,0,0.6); }}
.words .w {{ display: inline-block; }}
.final {{ display: flex; align-items: center; justify-content: center; background: {bg}; }}
.final-in {{ display: flex; flex-direction: column; align-items: center; gap: {round(unit * 0.04)}px;
            max-width: 82%; text-align: center; }}
.logo {{ display: block; max-width: {round(unit * 0.5)}px; max-height: {round(unit * 0.22)}px; object-fit: contain; }}
.offer {{ color: {light}; font-size: {offer_size}px; font-weight: 800; line-height: 1.1; text-wrap: balance; }}
.addr {{ color: {accent}; font-size: {addr_size}px; font-weight: 600; }}
"""
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width={w}, height={h}">
<title>{html.escape(scn.get('title') or job.get('title') or 'Ролик')}</title>
<script src="assets/gsap.min.js"></script>
<style>{css}</style>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-width="{w}" data-height="{h}" data-duration="{total}">
{chr(10).join(body)}
</div>
<script>
const tl = gsap.timeline({{ paused: true }});
{chr(10).join(tweens)}
window.__timelines["main"] = tl;
</script>
</body>
</html>
"""
