"""
Брендирование Django-админки тенанта цветами сети (14.09.2026).

Зачем: у сетей с собственным брендом (Автосуши и т.п.) мини-апп уже красится
по `ClientConfig.brand_color`, а админка у всех была фиолетовой — цвета
зашиты в templates/admin/base_site.html. Владелец попросил красить и админку.

Правила:
• Только при включённом пер-тенантном флаге `ClientConfig.admin_brand_enabled`
  (по умолчанию ВЫКЛ — шаблон отдаёт байт-в-байт прежний фиолет).
• Цвета берутся из тех же полей, что и для мини-аппа: `brand_color` (основной)
  и `brand_color_secondary` (акцент). Производные оттенки считаются здесь.
• Некорректный HEX → брендирования нет (не падаем, не красим).
• Светлый основной цвет (жёлтый и т.п.) → текст на нём тёмный, иначе белые
  надписи шапки/кнопок сливаются.
"""
from __future__ import annotations

import re

_HEX_RE = re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$')

DEFAULT_ACCENT = '#a3e635'   # лайм из base_site.html
INK_LIGHT = '#ffffff'
INK_DARK = '#1f2937'


def parse_hex(value) -> tuple[int, int, int] | None:
    """'#RGB' | '#RRGGBB' → (r, g, b); иначе None."""
    s = str(value or '').strip()
    if not _HEX_RE.match(s):
        return None
    h = s[1:]
    if len(h) == 3:
        h = ''.join(ch * 2 for ch in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def to_hex(rgb) -> str:
    r, g, b = (max(0, min(255, int(round(x)))) for x in rgb)
    return '#%02x%02x%02x' % (r, g, b)


def mix(rgb, other, weight: float):
    """Смешать rgb с other: weight=1 → other целиком, 0 → rgb целиком."""
    w = max(0.0, min(1.0, float(weight)))
    return tuple(c * (1 - w) + o * w for c, o in zip(rgb, other))


def darken(rgb, factor: float):
    return tuple(c * factor for c in rgb)


def luminance(rgb) -> float:
    """Относительная яркость (WCAG), 0 — чёрный, 1 — белый."""
    def ch(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def build_palette(brand_hex: str, accent_hex: str = '') -> dict | None:
    """Палитра админки из двух брендовых цветов. None — если основной цвет кривой."""
    rgb = parse_hex(brand_hex)
    if rgb is None:
        return None
    white = (255, 255, 255)
    accent_rgb = parse_hex(accent_hex)
    dark = darken(rgb, 0.72)
    return {
        'brand':             to_hex(rgb),
        'brand_dark':        to_hex(dark),
        'brand_soft':        to_hex(mix(rgb, white, 0.90)),
        'brand_soft_border': to_hex(mix(rgb, white, 0.78)),
        'bcrumb':            to_hex(dark),
        'bcrumb_link':       to_hex(mix(rgb, white, 0.65)),
        'focus_rgba':        'rgba(%d,%d,%d,0.12)' % tuple(int(c) for c in rgb),
        'accent':            to_hex(accent_rgb) if accent_rgb else DEFAULT_ACCENT,
        'ink':               INK_DARK if luminance(rgb) > 0.55 else INK_LIGHT,
    }


def admin_brand_context(config) -> dict | None:
    """
    Контекст `admin_brand` для base_site.html по ClientConfig тенанта.
    None — флаг выключен, конфига нет или цвет некорректный (админка прежняя).
    """
    if config is None or not getattr(config, 'admin_brand_enabled', False):
        return None
    palette = build_palette(
        getattr(config, 'brand_color', '') or '',
        getattr(config, 'brand_color_secondary', '') or '',
    )
    if palette is None:
        return None
    logo_url = ''
    try:
        logo = getattr(config, 'logotype_image', None)
        if logo and getattr(logo, 'name', ''):
            logo_url = logo.url
    except Exception:
        logo_url = ''
    palette['logo_url'] = logo_url
    return palette
