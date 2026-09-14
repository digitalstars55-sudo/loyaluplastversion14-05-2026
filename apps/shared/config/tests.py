from django.test import TestCase

# Create your tests here.


# ── Брендирование админки цветами сети (admin_brand.py, 14.09.2026) ──────────
from types import SimpleNamespace  # noqa: E402

from django.test import SimpleTestCase  # noqa: E402

from apps.shared.config.admin_brand import (  # noqa: E402
    DEFAULT_ACCENT, INK_DARK, INK_LIGHT, admin_brand_context, build_palette, parse_hex,
)


class AdminBrandPaletteTest(SimpleTestCase):
    def test_parse_hex(self):
        self.assertEqual(parse_hex('#FF6A1A'), (255, 106, 26))
        self.assertEqual(parse_hex('#fff'), (255, 255, 255))
        self.assertIsNone(parse_hex('FF6A1A'))
        self.assertIsNone(parse_hex('#GGGGGG'))
        self.assertIsNone(parse_hex(''))
        self.assertIsNone(parse_hex(None))

    def test_palette_autosushi(self):
        p = build_palette('#FF6A1A', '#66b14c')
        self.assertEqual(p['brand'], '#ff6a1a')
        self.assertEqual(p['accent'], '#66b14c')
        self.assertEqual(p['ink'], INK_LIGHT)            # оранжевый — белый текст
        self.assertNotEqual(p['brand_dark'], p['brand'])
        self.assertTrue(p['brand_soft'].startswith('#'))
        self.assertEqual(p['focus_rgba'], 'rgba(255,106,26,0.12)')

    def test_palette_light_brand_uses_dark_ink(self):
        p = build_palette('#FFE600', '#0A0A0A')         # жёлтый (Шавуха)
        self.assertEqual(p['ink'], INK_DARK)

    def test_palette_bad_accent_falls_back(self):
        p = build_palette('#9b000a', 'oops')
        self.assertEqual(p['accent'], DEFAULT_ACCENT)

    def test_palette_bad_brand_is_none(self):
        self.assertIsNone(build_palette('purple', '#fff'))


class AdminBrandContextTest(SimpleTestCase):
    def _cfg(self, **kw):
        base = dict(admin_brand_enabled=True, brand_color='#FF6A1A', brand_color_secondary='#66b14c',
                    logotype_image=SimpleNamespace(name='config/logos/x.png', url='/media/config/logos/x.png'))
        base.update(kw)
        return SimpleNamespace(**base)

    def test_flag_off_is_none(self):
        self.assertIsNone(admin_brand_context(self._cfg(admin_brand_enabled=False)))
        self.assertIsNone(admin_brand_context(None))

    def test_flag_on_gives_palette_and_logo(self):
        ctx = admin_brand_context(self._cfg())
        self.assertEqual(ctx['brand'], '#ff6a1a')
        self.assertEqual(ctx['logo_url'], '/media/config/logos/x.png')

    def test_no_logo(self):
        ctx = admin_brand_context(self._cfg(logotype_image=SimpleNamespace(name='', url='/x')))
        self.assertEqual(ctx['logo_url'], '')

    def test_bad_color_is_none(self):
        self.assertIsNone(admin_brand_context(self._cfg(brand_color='')))
