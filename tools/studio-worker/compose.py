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

    body, tweens = [], []
    for s in scenes:
        n, at, ln = s['n'], s['at'], float(s['length'])
        key = f"asset-{s.get('asset_id')}" if s.get('asset_id') else ''
        src = files.get(key, '')
        if s.get('kind') == 'video' and src:
            body.append(
                f'<video id="s{n}" class="clip media" src="{html.escape(src)}" muted playsinline '
                f'data-start="{at}" data-duration="{ln}" data-media-start="{float(s.get("start") or 0)}" '
                f'data-track-index="0"></video>')
        elif s.get('kind') == 'photo' and src:
            body.append(
                f'<div id="s{n}" class="clip" data-start="{at}" data-duration="{ln}" data-track-index="0">'
                f'<img id="s{n}-img" class="photo" src="{html.escape(src)}" alt=""></div>')
            motion = s.get('motion') or 'zoom_in'
            frm, to = {
                'zoom_in': ({'scale': 1.0}, {'scale': 1.12}),
                'zoom_out': ({'scale': 1.12}, {'scale': 1.0}),
                'pan_left': ({'scale': 1.12, 'xPercent': 3}, {'scale': 1.12, 'xPercent': -3}),
                'pan_right': ({'scale': 1.12, 'xPercent': -3}, {'scale': 1.12, 'xPercent': 3}),
            }.get(motion, ({'scale': 1.0}, {'scale': 1.06}))
            tweens.append(f'tl.fromTo("#s{n}-img", {json.dumps(frm)}, '
                          f'{json.dumps(dict(to, duration=ln, ease="none"))}, {at});')
        else:
            body.append(
                f'<div id="s{n}" class="clip title" data-start="{at}" data-duration="{ln}" data-track-index="0">'
                f'<div id="s{n}-t" class="title-text">{html.escape(s.get("caption") or "")}</div></div>')
            tweens.append(f'tl.fromTo("#s{n}-t", {{"y": 40, "opacity": 0}}, '
                          f'{{"y": 0, "opacity": 1, "duration": 0.45, "ease": "power3.out"}}, {at});')
            continue
        cap = (s.get('caption') or '').strip()
        if cap:
            body.append(
                f'<div id="c{n}" class="clip cap" data-start="{at}" data-duration="{ln}" data-track-index="2">'
                f'<span id="c{n}-p" class="plate">{html.escape(cap)}</span></div>')
            tweens.append(f'tl.fromTo("#c{n}-p", {{"y": 24, "opacity": 0}}, '
                          f'{{"y": 0, "opacity": 1, "duration": 0.3, "ease": "power2.out"}}, {at + 0.05:.2f});')

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
