"""Распределитель входа /go/ (apps/shared/guest/go.py)."""
import base64
import datetime
from types import SimpleNamespace
from unittest import mock

from django.http import Http404
from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.shared.guest import go as go_mod

VK_UA = 'Mozilla/5.0 (Linux; Android 13) VKAndroidApp/8.40'
TG_UA = 'Mozilla/5.0 (Linux; Android 13) Telegram-Android/10.9.1'
BROWSER_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari/604.1'


def _tenant(web=False, active=True, paid=True):
    today = datetime.date.today()
    return SimpleNamespace(
        schema_name='levone', client_id=5, name='LevOne', is_active=active,
        paid_until=today + datetime.timedelta(days=1 if paid else -1),
        config=SimpleNamespace(web_entry_enabled=web),
    )


@override_settings(VK_MINI_APP_ID=111, GUEST_WEB_BASE_URL='https://loyalupp.ru', TELEGRAM_GUEST_APP_URL='')
class GoViewTest(SimpleTestCase):
    def call(self, query='', ua=BROWSER_UA, tenant=None, cookies=None):
        request = RequestFactory().get('/go/' + query, HTTP_USER_AGENT=ua)
        request.COOKIES.update(cookies or {})
        with mock.patch.object(go_mod, 'connection', SimpleNamespace(tenant=tenant or _tenant())):
            return go_mod.go(request)

    def test_vk_app_goes_to_mini_app_with_same_screen(self):
        r = self.call('?branch=12&src=menu&to=review', ua=VK_UA, tenant=_tenant(web=True))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'], 'https://vk.com/app111/#/review?company=5&branch=12&src=menu')

    def test_web_disabled_always_vk(self):
        for ua in (BROWSER_UA, TG_UA):
            r = self.call('?branch=12', ua=ua)
            self.assertTrue(r['Location'].startswith('https://vk.com/app111/'), ua)

    def test_browser_with_web_shows_choice(self):
        r = self.call('?branch=12&to=profile', tenant=_tenant(web=True))
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn('Открыть во ВКонтакте', body)
        self.assertIn('Продолжить в браузере', body)
        self.assertNotIn('Телеграм', body)
        self.assertIn('via=web', body)

    def test_choice_is_remembered(self):
        r = self.call('?branch=12&via=web&remember=1', tenant=_tenant(web=True))
        self.assertEqual(r['Location'], 'https://loyalupp.ru/?company=5&branch=12#/')
        self.assertEqual(r.cookies[go_mod.PREF_COOKIE].value, 'web')
        r2 = self.call('?branch=12', tenant=_tenant(web=True), cookies={go_mod.PREF_COOKIE: 'web'})
        self.assertEqual(r2.status_code, 302)
        self.assertTrue(r2['Location'].startswith('https://loyalupp.ru/'))

    def test_telegram_without_tma_goes_web(self):
        r = self.call('?branch=12', ua=TG_UA, tenant=_tenant(web=True))
        self.assertTrue(r['Location'].startswith('https://loyalupp.ru/'))

    @override_settings(TELEGRAM_GUEST_APP_URL='https://t.me/LoyalUpBot/app')
    def test_telegram_with_tma_carries_start_param(self):
        r = self.call('?branch=12&src=menu&to=coupon', ua=TG_UA, tenant=_tenant(web=True))
        loc = r['Location']
        self.assertTrue(loc.startswith('https://t.me/LoyalUpBot/app?startapp='))
        start = loc.split('startapp=', 1)[1]
        self.assertRegex(start, r'^[A-Za-z0-9_-]+$')
        decoded = base64.urlsafe_b64decode(start + '=' * (-len(start) % 4)).decode()
        self.assertEqual(decoded, 'company=5&branch=12&src=menu&to=coupon')

    def test_junk_params_dropped(self):
        r = self.call('?branch=12x&src=<script>&to=../admin&table=3&evil=1', ua=VK_UA)
        self.assertEqual(r['Location'], 'https://vk.com/app111/#/?company=5&table=3')

    def test_via_unavailable_is_ignored(self):
        r = self.call('?via=web', ua=BROWSER_UA)  # веб у сети выключен
        self.assertTrue(r['Location'].startswith('https://vk.com/'))

    def test_inactive_or_expired_tenant_404(self):
        for t in (_tenant(active=False), _tenant(paid=False), SimpleNamespace(schema_name='public')):
            with self.assertRaises(Http404):
                self.call(tenant=t)


class DecideTest(SimpleTestCase):
    def test_table(self):
        d = go_mod.decide
        self.assertEqual(d('vk', web_enabled=True, tg_available=True), 'vk')
        self.assertEqual(d('tg', web_enabled=True, tg_available=True), 'tg')
        self.assertEqual(d('tg', web_enabled=False, tg_available=True), 'vk')
        self.assertIsNone(d('browser', web_enabled=True, tg_available=False))
        self.assertEqual(d('browser', web_enabled=True, tg_available=False, remembered='tg'), None)
        self.assertEqual(d('browser', web_enabled=True, tg_available=True, remembered='tg'), 'tg')
        self.assertEqual(d('vk', web_enabled=True, tg_available=False, via='web'), 'web')
