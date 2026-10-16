"""16.10.2026 — переходы по кнопкам карт считаются; ссылка подписана, назначение не подменить."""
from types import SimpleNamespace
from unittest.mock import patch

from django.core import signing
from django.test import RequestFactory, SimpleTestCase

from apps.tenant.branch import map_links as ML


class TrackedLinkTest(SimpleTestCase):

    def test_ссылка_через_домен_сети_и_подписана(self):
        with patch.object(ML, '_домен_сети', return_value='levone.levelupapp.ru'):
            url = ML.tracked(SimpleNamespace(pk=42), 'yandex', 'https://yandex.ru/maps/org/1/reviews')
        self.assertTrue(url.startswith('https://levone.levelupapp.ru/r/map/'))
        token = url.rstrip('/').rsplit('/', 1)[1]
        data = signing.loads(token, salt=ML.SALT)
        self.assertEqual((data['c'], data['p'], data['u']), (42, 'yandex', 'https://yandex.ru/maps/org/1/reviews'))

    def test_без_домена_прямая_ссылка(self):
        with patch.object(ML, '_домен_сети', return_value=''):
            self.assertEqual(ML.tracked(SimpleNamespace(pk=1), '2gis', 'https://2gis.ru/x'), 'https://2gis.ru/x')

    def test_переход_записан_и_гость_ушёл_на_карту(self):
        token = signing.dumps({'c': 5, 'p': '2gis', 'u': 'https://2gis.ru/x'}, salt=ML.SALT, compress=True)
        conv = SimpleNamespace(pk=5, branch_id=2, inferred_branch_id=None, vk_sender_id='123')
        with patch('apps.tenant.branch.models.TestimonialConversation.objects') as convs, \
             patch('apps.tenant.branch.models.ReviewLinkClick.objects') as clicks:
            convs.filter.return_value.first.return_value = conv
            resp = ML.redirect_view(RequestFactory().get('/r/map/x/'), token)
        self.assertEqual((resp.status_code, resp['Location']), (302, 'https://2gis.ru/x'))
        kw = clicks.create.call_args.kwargs
        self.assertEqual((kw['platform'], kw['vk_id'], kw['branch_id']), ('2gis', '123', 2))

    def test_поддельная_подпись_404(self):
        from django.http import Http404
        with self.assertRaises(Http404):
            ML.redirect_view(RequestFactory().get('/r/map/x/'), 'чужой:токен')
