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


def _health(**down):
    """Состояние проверок: перечисленные площадки лежат."""
    return {'checked_at': 0, 'channels': {
        name: {'up': not down.get(name, False)} for name in ('vk', 'web', 'vkid', 'tg')}}


class OrchestratorDecideTest(SimpleTestCase):
    """15.10.2026: /go ведёт только в живые площадки, основная — из настроек сети."""

    def d(self, channel, **kw):
        kw.setdefault('web_enabled', True)
        kw.setdefault('tg_available', True)
        return go_mod.decide(channel, **kw)

    def test_vk_down_vk_app_goes_web(self):
        self.assertEqual(self.d(go_mod.CHANNEL_VK, health=_health(vk=True)), go_mod.TARGET_WEB)

    def test_browser_only_web_alive_goes_web_without_choice(self):
        self.assertEqual(self.d(go_mod.CHANNEL_BROWSER, health=_health(vk=True)), go_mod.TARGET_WEB)

    def test_browser_all_alive_primary_wins(self):
        self.assertEqual(self.d(go_mod.CHANNEL_BROWSER, primary='web', health=_health()), go_mod.TARGET_WEB)
        self.assertIsNone(self.d(go_mod.CHANNEL_BROWSER, primary='ask', health=_health()))

    def test_primary_down_falls_back(self):
        # основная — ВК, но ВК лежит: из быстрых живых остался веб
        self.assertEqual(self.d(go_mod.CHANNEL_BROWSER, primary='vk', health=_health(vk=True)), go_mod.TARGET_WEB)
        self.assertEqual(self.d(go_mod.CHANNEL_BROWSER, primary='vk', tg_available=False,
                                health=_health(vk=True)), go_mod.TARGET_WEB)

    def test_vkid_down_means_no_web(self):
        self.assertEqual(self.d(go_mod.CHANNEL_TELEGRAM, health=_health(vkid=True)), go_mod.TARGET_VK)

    def test_remembered_dead_choice_ignored(self):
        self.assertEqual(self.d(go_mod.CHANNEL_BROWSER, remembered='vk', primary='web',
                                health=_health(vk=True)), go_mod.TARGET_WEB)

    def test_everything_down_old_behaviour(self):
        self.assertEqual(self.d(go_mod.CHANNEL_BROWSER, health=_health(vk=True, web=True)), go_mod.TARGET_VK)

    def test_no_health_data_all_alive(self):
        self.assertEqual(self.d(go_mod.CHANNEL_VK, health=None), go_mod.TARGET_VK)


class ChannelHealthTest(SimpleTestCase):
    def test_down_only_after_two_failures_and_alert_once(self):
        from django.core.cache import caches
        from apps.shared.guest import channel_health as ch
        store = {}
        fake = SimpleNamespace(get=lambda k: store.get(k), set=lambda k, v, timeout=None: store.__setitem__(k, v))
        probes = {'vk': (False, 'timeout', 5), 'web': (True, '', 5), 'vkid': (True, '', 5), 'tg': (True, '', 0)}
        with mock.patch.object(ch, '_cache', return_value=fake), \
                mock.patch.object(ch, '_probe_all', return_value=probes), \
                mock.patch.object(ch, '_alert') as alert:
            s1 = ch.run_checks(now=1000)
            self.assertTrue(s1['channels']['vk']['up'])
            s2 = ch.run_checks(now=1060)
            self.assertFalse(s2['channels']['vk']['up'])
            ch.run_checks(now=1120)
            self.assertEqual(alert.call_count, 1)
            probes['vk'] = (True, '', 5)
            s4 = ch.run_checks(now=1180)
            self.assertTrue(s4['channels']['vk']['up'])
            self.assertEqual(alert.call_count, 2)
            self.assertFalse(ch.is_up('vk', s2))
            self.assertIsNone(ch.get_state(now=1180 + 10_000))


@override_settings(VK_MINI_APP_ID=111, GUEST_WEB_BASE_URL='https://vkapp.levelupapp.ru', TELEGRAM_GUEST_APP_URL='')
class GoRouteProbeTest(SimpleTestCase):
    def test_browser_primary_vk_checks_vk_on_guest_side(self):
        tenant = _tenant(web=True)
        tenant.config.guest_primary_channel = 'vk'
        request = RequestFactory().get('/go/?branch=1', HTTP_USER_AGENT=BROWSER_UA)
        with mock.patch.object(go_mod, 'connection', SimpleNamespace(tenant=tenant)), \
                mock.patch('apps.shared.guest.channel_health.get_state', return_value=None):
            r = go_mod.go(request)
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn('vk.com/favicon.ico', body)
        self.assertIn('var vk = "https://vk.com/app111/', body)
        self.assertIn('vkapp.levelupapp.ru', body)
