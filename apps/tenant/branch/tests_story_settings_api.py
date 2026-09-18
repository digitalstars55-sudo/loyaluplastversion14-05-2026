"""
Тесты настроек механики «Игра через сториз» (контракт платформы 3б.3, №23).

Стратегия патчей — как в senler/tests_broadcasts_api.py: модели тенантные, в
тестовую БД не ходим, подменяем зависимости прямо в модуле вьюх.

Главный тест здесь — `SourceAgreesWithResolverTest`. Кабинет CheckUp показывает
сотруднику `effective` (общим резолвом) и рядом `source` (посчитанный у нас).
Если правила разъедутся, сотрудник увидит «значение сети» там, где на самом
деле работает переопределение точки, — и будет править не то поле. Тест гоняет
матрицу «значение точки × значение сети» через НАСТОЯЩИЙ резолв и требует,
чтобы названный источник указывал ровно на выбранное резолвом значение.
"""
from datetime import date
from types import SimpleNamespace
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant.branch.api import story_settings as ST
from apps.tenant.inventory.api import story_services as SS

STP = 'apps.tenant.branch.api.story_settings.'
SSP = 'apps.tenant.inventory.api.story_services.'


def _net(**over):
    """ClientConfig сети: значения по умолчанию модели (они НЕ nullable)."""
    values = {
        'story_game_enabled': False,
        'story_min_order_amount': 600,
        'story_activation_minutes': 40,
        'story_require_cafe_visit': True,
        'story_cafe_address': '',
        'story_activation_text': '',
        'story_saved_text': '',
        'story_gift_lifetime_days': 14,
        'story_gift_reminder_days': 10,
        'story_campaign_start': None,
        'story_campaign_end': None,
    }
    values.update(over)
    return SimpleNamespace(**values)


def _branch_cfg(**over):
    """BranchConfig: переопределения пустые (null у числа и булева, '' у текстов)."""
    values = {
        'address': '',
        'story_game_enabled': None,
        'story_min_order_amount': None,
        'story_cafe_address': '',
        'story_activation_text': '',
        'story_saved_text': '',
    }
    values.update(over)
    return SimpleNamespace(**values)


def _branch(cfg=None, pk=3, branch_id=202, name='Институтская', story_image=None):
    # story_image — поле САМОЙ Branch (models.py:63), не BranchConfig.
    return SimpleNamespace(pk=pk, id=pk, branch_id=branch_id, name=name,
                           story_image=story_image,
                           config=cfg if cfg is not None else _branch_cfg())


def _user(role='network_admin', is_superuser=False):
    return SimpleNamespace(
        is_authenticated=True, is_active=True, is_superuser=is_superuser,
        is_superadmin=(role == 'superadmin'), is_network_admin=(role == 'network_admin'),
        is_client=(role == 'client'), role=role, username='t', pk=1, is_staff=True,
    )


def _call(view_cls, method, path, *, data=None, user=None, **kwargs):
    factory = APIRequestFactory()
    if method == 'get':
        request = factory.get(path)
    else:
        request = getattr(factory, method)(path, data or {}, format='json')
    force_authenticate(request, user=user or _user())
    return view_cls.as_view()(request, **kwargs)


# ── источник значения против настоящего резолва ──────────────────────────────

class SourceAgreesWithResolverTest(SimpleTestCase):
    """`source` обязан указывать на то значение, которое выбрал общий резолв."""

    SCENARIOS = {
        'всё по умолчанию': (_branch_cfg(), _net()),
        'сеть включила игру': (_branch_cfg(), _net(story_game_enabled=True)),
        'точка выключила игру, сеть включила': (
            _branch_cfg(story_game_enabled=False), _net(story_game_enabled=True)),
        'точка включила игру, сеть выключила': (
            _branch_cfg(story_game_enabled=True), _net()),
        'точка переопределила всё': (
            _branch_cfg(story_min_order_amount=900, story_cafe_address='Ленина 1',
                        story_activation_text='текст точки', story_saved_text='сохранено точкой'),
            _net(story_min_order_amount=600, story_cafe_address='Сетевой адрес',
                 story_activation_text='текст сети', story_saved_text='сохранено сетью')),
        'ноль у точки = как в сети': (
            _branch_cfg(story_min_order_amount=0), _net(story_min_order_amount=700)),
        'адрес пуст везде, но у точки есть адрес карточки': (
            _branch_cfg(address='Институтская 5'), _net()),
        'адреса нет нигде': (_branch_cfg(), _net()),
        'кампания задана сетью': (
            _branch_cfg(), _net(story_campaign_start=date(2026, 9, 1),
                                story_campaign_end=date(2026, 9, 30))),
    }

    DEFAULTS = {
        'story_game_enabled': False,
        'story_min_order_amount': SS._DEFAULT_MIN_ORDER,
        'story_activation_minutes': SS._DEFAULT_ACTIVATION_MINUTES,
        'story_require_cafe_visit': SS._DEFAULT_REQUIRE_CAFE,
        'story_activation_text': SS._DEFAULT_ACTIVATION_TEXT,
        'story_saved_text': SS._DEFAULT_SAVED_TEXT,
        'story_gift_lifetime_days': SS._DEFAULT_GIFT_LIFETIME_DAYS,
        'story_gift_reminder_days': SS._DEFAULT_GIFT_REMINDER_DAYS,
        'story_campaign_start': None,
        'story_campaign_end': None,
    }

    def test_matrix(self):
        for label, (branch_cfg, net_cfg) in self.SCENARIOS.items():
            branch = _branch(branch_cfg)
            with patch(SSP + '_network_config', return_value=net_cfg):
                resolved = SS.resolve_story_settings_for_branch(branch)
            address = branch_cfg.address or ''
            for field in ST.NETWORK_FIELDS:
                source = ST._source_for(field, branch_cfg, net_cfg, address)
                value = resolved[ST.RESOLVED_KEY[field]]
                with self.subTest(scenario=label, field=field, source=source):
                    if source == 'branch':
                        self.assertEqual(value, getattr(branch_cfg, field))
                    elif source == 'network':
                        self.assertEqual(value, getattr(net_cfg, field))
                    elif source == 'branch_address':
                        self.assertEqual(value, address)
                    else:
                        expected = self.DEFAULTS.get(field, '')
                        self.assertEqual(value, expected)

    def test_branch_false_wins_over_network_true(self):
        """Ключевая семантика: выключить игру на одной точке можно."""
        branch_cfg = _branch_cfg(story_game_enabled=False)
        net_cfg = _net(story_game_enabled=True)
        with patch(SSP + '_network_config', return_value=net_cfg):
            resolved = SS.resolve_story_settings_for_branch(_branch(branch_cfg))
        self.assertFalse(resolved['enabled'])
        self.assertEqual(ST._source_for('story_game_enabled', branch_cfg, net_cfg, ''), 'branch')

    def test_zero_means_inherit_not_zero(self):
        """`0` у точки = «как в сети» (порог 0 ₽ в v1.5 задать нельзя)."""
        branch_cfg = _branch_cfg(story_min_order_amount=0)
        net_cfg = _net(story_min_order_amount=700)
        with patch(SSP + '_network_config', return_value=net_cfg):
            resolved = SS.resolve_story_settings_for_branch(_branch(branch_cfg))
        self.assertEqual(resolved['min_order_amount'], 700)
        self.assertEqual(ST._source_for('story_min_order_amount', branch_cfg, net_cfg, ''), 'network')

    def test_sources_are_from_the_contract_set(self):
        allowed = {'branch', 'network', 'branch_address', 'default'}
        for field in ST.NETWORK_FIELDS:
            self.assertIn(ST._source_for(field, _branch_cfg(), _net(), ''), allowed)


# ── поля и подстановки: сверка с моделями и с рендером ───────────────────────

class ContractShapeTest(SimpleTestCase):

    def test_eleven_network_fields_exist_on_model(self):
        from apps.shared.config.models import ClientConfig
        names = {f.name for f in ClientConfig._meta.get_fields()}
        self.assertEqual(len(ST.NETWORK_FIELDS), 11)
        for field in ST.NETWORK_FIELDS:
            self.assertIn(field, names)

    def test_five_override_fields_exist_on_branch_config(self):
        from apps.tenant.branch.models import BranchConfig
        names = {f.name for f in BranchConfig._meta.get_fields()}
        self.assertEqual(len(ST.OVERRIDE_FIELDS), 5)
        for field in ST.OVERRIDE_FIELDS:
            self.assertIn(field, names)

    def test_placeholders_match_the_renderer(self):
        """Список подстановок в ответе = то, что реально подставляет рендер."""
        text = ' '.join(ST.PLACEHOLDERS)
        rendered = SS.render_story_text(
            text, cafe_name='Кафе',
            settings={'cafe_address': 'Адрес', 'min_order_amount': 600, 'activation_minutes': 40},
            gift_name='Подарок')
        for placeholder in ST.PLACEHOLDERS:
            self.assertNotIn(placeholder, rendered)


# ── разбор значений ──────────────────────────────────────────────────────────

class ParseValueTest(SimpleTestCase):

    def test_inheritable_nulls(self):
        self.assertIsNone(ST._parse_value('story_min_order_amount', None, inheritable=True))
        self.assertIsNone(ST._parse_value('story_min_order_amount', 0, inheritable=True))
        self.assertIsNone(ST._parse_value('story_game_enabled', None, inheritable=True))
        self.assertEqual(ST._parse_value('story_activation_text', '', inheritable=True), '')
        self.assertEqual(ST._parse_value('story_activation_text', None, inheritable=True), '')

    def test_values_are_parsed(self):
        self.assertTrue(ST._parse_value('story_game_enabled', 'true', inheritable=True))
        self.assertFalse(ST._parse_value('story_game_enabled', False, inheritable=True))
        self.assertEqual(ST._parse_value('story_min_order_amount', '900', inheritable=False), 900)
        self.assertEqual(ST._parse_value('story_campaign_start', '2026-09-01', inheritable=False),
                         date(2026, 9, 1))

    def test_bad_values(self):
        for field, raw in (('story_game_enabled', 'ага'),
                           ('story_min_order_amount', 'много'),
                           ('story_min_order_amount', -5),
                           ('story_campaign_start', '01.09.2026'),
                           ('story_cafe_address', 'x' * 256)):
            with self.subTest(field=field, raw=raw):
                with self.assertRaises(ST.PayloadError):
                    ST._parse_value(field, raw, inheritable=False)

    def test_unknown_keys_are_rejected(self):
        with self.assertRaises(ST.PayloadError) as ctx:
            ST._collect({'brand_color': '#fff'}, ST.NETWORK_FIELDS, inheritable=False)
        self.assertIn('brand_color', ctx.exception.detail)

    def test_empty_body_is_rejected(self):
        with self.assertRaises(ST.PayloadError):
            ST._collect({}, ST.NETWORK_FIELDS, inheritable=False)


# ── ручка сети ───────────────────────────────────────────────────────────────

class NetworkStoryViewTest(SimpleTestCase):

    def test_requires_auth(self):
        self.assertIn(IsAuthenticated, ST.NetworkStorySettingsAPIView.permission_classes)
        factory = APIRequestFactory()
        resp = ST.NetworkStorySettingsAPIView.as_view()(factory.get('/api/v1/settings/story/'))
        self.assertIn(resp.status_code, (401, 403))

    def test_client_cannot_write(self):
        with patch(STP + '_network_config', return_value=_net()):
            resp = _call(ST.NetworkStorySettingsAPIView, 'patch', '/api/v1/settings/story/',
                         data={'story_game_enabled': True}, user=_user(role='client'))
        self.assertEqual((resp.status_code, resp.data['code']), (403, 'role_not_allowed'))

    def test_foreign_field_is_400_with_editable(self):
        with patch(STP + '_network_config', return_value=_net()):
            resp = _call(ST.NetworkStorySettingsAPIView, 'patch', '/api/v1/settings/story/',
                         data={'brand_color': '#fff'})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))
        self.assertEqual(resp.data['editable'], ST.NETWORK_FIELDS)

    def test_campaign_window_is_checked(self):
        cfg = _net()
        cfg.save = MagicMock()
        with patch(STP + '_network_config', return_value=cfg):
            resp = _call(ST.NetworkStorySettingsAPIView, 'patch', '/api/v1/settings/story/',
                         data={'story_campaign_start': '2026-09-30',
                               'story_campaign_end': '2026-09-01'})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))
        cfg.save.assert_not_called()

    def test_patch_writes_only_given_fields(self):
        cfg = _net()
        cfg.save = MagicMock()
        with patch(STP + '_network_config', return_value=cfg), \
             patch('apps.tenant.catalog.models.Product') as product:
            product.objects.filter.return_value.count.return_value = 3
            resp = _call(ST.NetworkStorySettingsAPIView, 'patch', '/api/v1/settings/story/',
                         data={'story_game_enabled': True, 'story_min_order_amount': 900})
        self.assertEqual(resp.status_code, 200)
        cfg.save.assert_called_once_with(
            update_fields=['story_game_enabled', 'story_min_order_amount'])
        self.assertTrue(cfg.story_game_enabled)
        self.assertEqual(cfg.story_min_order_amount, 900)
        self.assertEqual(resp.data['prizes'], {'network_count': 3})
        self.assertEqual(resp.data['branch_override_fields'], ST.OVERRIDE_FIELDS)
        self.assertEqual(resp.data['placeholders'], ST.PLACEHOLDERS)


# ── ручка точки ──────────────────────────────────────────────────────────────

class BranchStoryViewTest(SimpleTestCase):

    def test_requires_auth(self):
        self.assertIn(IsAuthenticated, ST.BranchStorySettingsAPIView.permission_classes)

    def test_foreign_branch_is_404(self):
        with patch(STP + '_branch_or_none', return_value=None):
            resp = _call(ST.BranchStorySettingsAPIView, 'get',
                         '/api/v1/mobile/branches/3/story/', pk=3)
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))

    def test_client_cannot_write(self):
        with patch(STP + '_branch_or_none', return_value=_branch()):
            resp = _call(ST.BranchStorySettingsAPIView, 'patch',
                         '/api/v1/mobile/branches/3/story/',
                         data={'story_game_enabled': False}, user=_user(role='client'), pk=3)
        self.assertEqual((resp.status_code, resp.data['code']), (403, 'role_not_allowed'))

    def test_network_only_field_is_rejected(self):
        """Минуты активации и «нужен визит» остаются сетевыми (в v1.5 без миграции)."""
        with patch(STP + '_branch_or_none', return_value=_branch()):
            resp = _call(ST.BranchStorySettingsAPIView, 'patch',
                         '/api/v1/mobile/branches/3/story/',
                         data={'story_activation_minutes': 10}, pk=3)
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))
        self.assertEqual(resp.data['editable'], ST.OVERRIDE_FIELDS)

    def test_patch_stores_inheritance_as_none(self):
        branch = _branch()
        cfg = _branch_cfg(story_min_order_amount=900, story_activation_text='текст точки')
        cfg.save = MagicMock()
        with patch(STP + '_branch_or_none', return_value=branch), \
             patch(STP + '_atomic', side_effect=nullcontext), \
             patch(STP + 'BranchConfig') as branch_config, \
             patch(STP + '_network_config', return_value=_net()), \
             patch(STP + 'story_gifts_for_branch') as gifts, \
             patch(SSP + '_network_config', return_value=_net()):
            branch_config.objects.get_or_create.return_value = (cfg, False)
            gifts.return_value = MagicMock(count=MagicMock(return_value=0))
            gifts.return_value.__getitem__ = lambda self_, item: []
            resp = _call(ST.BranchStorySettingsAPIView, 'patch',
                         '/api/v1/mobile/branches/3/story/',
                         data={'story_min_order_amount': 0, 'story_activation_text': ''}, pk=3)
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(cfg.story_min_order_amount)
        self.assertEqual(cfg.story_activation_text, '')
        cfg.save.assert_called_once()

    def test_payload_shape(self):
        branch = _branch(_branch_cfg(address='Институтская 5'))
        request = APIRequestFactory().get('/')
        with patch(STP + '_network_config', return_value=_net(story_game_enabled=True)), \
             patch(SSP + '_network_config', return_value=_net(story_game_enabled=True)), \
             patch(STP + 'story_gifts_for_branch') as gifts:
            qs = MagicMock()
            qs.count.return_value = 0
            qs.__getitem__ = lambda self_, item: []
            gifts.return_value = qs
            payload = ST.BranchStorySettingsAPIView._payload(branch)

        self.assertEqual(set(payload), {'branch', 'overrides', 'effective', 'source',
                                        'prizes', 'rendered'})
        self.assertEqual(payload['branch'], {'id': 3, 'branch_id': 202, 'name': 'Институтская'})
        self.assertEqual(len(payload['effective']), 11)
        self.assertEqual(set(payload['overrides']), set(ST.OVERRIDE_FIELDS))
        self.assertIsNone(payload['overrides']['story_min_order_amount'], 'ненастроенное = null')
        self.assertEqual(payload['source']['story_cafe_address'], 'branch_address')
        self.assertEqual(payload['prizes'],
                         {'count': 0, 'pool': [], 'first': None, 'story_image_url': None})
        # Пул пуст → подстановка остаётся видимой, чтобы сотрудник это заметил.
        self.assertIn('[название подарка]', payload['rendered']['activation_text'])


class RenderedTest(SimpleTestCase):

    def test_first_prize_name_is_substituted(self):
        branch = _branch()
        effective = {
            'story_cafe_address': 'Ленина 1',
            'story_min_order_amount': 600,
            'story_activation_minutes': 40,
            'story_activation_text': 'Закажи от [сумма] ₽ в [название кафе] и получи [название подарка]',
            'story_saved_text': 'Подарок [название подарка] ждёт [время] минут',
        }
        gift = SimpleNamespace(pk=5, name='Кофе')
        out = ST._rendered(branch, effective, gift)
        self.assertEqual(out['activation_text'],
                         'Закажи от 600 ₽ в Институтская и получи Кофе')
        self.assertEqual(out['saved_text'], 'Подарок Кофе ждёт 40 минут')


# ── доделка по ревью CheckUp (3б.8) ──────────────────────────────────────────

class StoryImageUrlTest(SimpleTestCase):
    """
    ★5: ссылка на картинку сториса — от primary-домена СЕТИ, не от запроса.

    CheckUp ходит через loopback по http с подменённым Host, поэтому
    `build_absolute_uri` вернул бы `http://127.0.0.1:7000/media/...`.
    """

    def test_url_is_built_from_network_domain(self):
        image = SimpleNamespace(url='/media/branch/stories/x.png', name='branch/stories/x.png')
        branch = _branch(story_image=image)
        with patch(STP + '_network_domain', return_value='levone.levelupapp.ru'), \
             patch(STP + 'story_gifts_for_branch') as gifts:
            qs = MagicMock()
            qs.count.return_value = 0
            qs.__getitem__ = lambda self_, item: []
            gifts.return_value = qs
            prizes, _ = ST._prizes(branch)
        self.assertEqual(prizes['story_image_url'],
                         'https://levone.levelupapp.ru/media/branch/stories/x.png')

    def test_prizes_takes_no_request(self):
        import inspect
        self.assertNotIn('request', inspect.signature(ST._prizes).parameters)

    def test_payload_has_no_request_host(self):
        import json
        image = SimpleNamespace(url='/media/branch/stories/x.png', name='branch/stories/x.png')
        branch = _branch(_branch_cfg(address='Институтская 5'), story_image=image)
        with patch(STP + '_network_config', return_value=_net()), \
             patch(SSP + '_network_config', return_value=_net()), \
             patch(STP + '_network_domain', return_value='levone.levelupapp.ru'), \
             patch(STP + 'story_gifts_for_branch') as gifts:
            qs = MagicMock()
            qs.count.return_value = 0
            qs.__getitem__ = lambda self_, item: []
            gifts.return_value = qs
            payload = ST.BranchStorySettingsAPIView._payload(branch)
        body = json.dumps(payload, ensure_ascii=False, default=str)
        self.assertNotIn('127.0.0.1', body)
        self.assertNotIn('testserver', body)
        self.assertIn('https://levone.levelupapp.ru/media/', body)


class ZeroMeansInheritTest(SimpleTestCase):
    """★15 и м16: ноль — это «как в сети», а не «без порога», и это не ошибка."""

    def test_patch_zero_returns_200_with_null_override_and_network_source(self):
        branch = _branch()
        cfg = _branch_cfg(story_min_order_amount=900)
        cfg.save = MagicMock()
        net_cfg = _net(story_min_order_amount=700)
        with patch(STP + '_branch_or_none', return_value=branch), \
             patch(STP + '_atomic', side_effect=nullcontext), \
             patch(STP + 'BranchConfig') as branch_config, \
             patch(STP + '_network_config', return_value=net_cfg), \
             patch(SSP + '_network_config', return_value=net_cfg), \
             patch(STP + '_network_domain', return_value='levone.levelupapp.ru'), \
             patch(STP + 'story_gifts_for_branch') as gifts:
            branch_config.objects.get_or_create.return_value = (cfg, False)
            qs = MagicMock()
            qs.count.return_value = 0
            qs.__getitem__ = lambda self_, item: []
            gifts.return_value = qs
            branch.config = cfg
            resp = _call(ST.BranchStorySettingsAPIView, 'patch',
                         '/api/v1/mobile/branches/3/story/',
                         data={'story_min_order_amount': 0}, pk=3)

        self.assertEqual(resp.status_code, 200, 'ноль — принятое значение, не 400')
        self.assertIsNone(cfg.story_min_order_amount, 'в БД легло «не задано»')
        self.assertIsNone(resp.data['overrides']['story_min_order_amount'])
        self.assertEqual(resp.data['source']['story_min_order_amount'], 'network')
        self.assertEqual(resp.data['effective']['story_min_order_amount'], 700)

    def test_network_zero_falls_back_to_hardcoded_default(self):
        """м16: у сети ноль тоже «не задано» → 600 ₽ из кода и source=default."""
        branch_cfg = _branch_cfg()
        net_cfg = _net(story_min_order_amount=0)
        with patch(SSP + '_network_config', return_value=net_cfg):
            resolved = SS.resolve_story_settings_for_branch(_branch(branch_cfg))
        self.assertEqual(resolved['min_order_amount'], SS._DEFAULT_MIN_ORDER)
        self.assertEqual(ST._source_for('story_min_order_amount', branch_cfg, net_cfg, ''),
                         'default')

    def test_limitation_is_documented(self):
        """Ограничение должно быть видно в докстринге ручки, а не только в контракте."""
        doc = ST.BranchStorySettingsAPIView.__doc__ or ''
        self.assertIn('0 ₽', doc)


class PrizesPoolTest(SimpleTestCase):
    """`prizes.pool` — первые 50 подарков в порядке гостя, `count` — весь пул."""

    @staticmethod
    def _product(pk, name, emoji='🎁', price=0):
        return SimpleNamespace(pk=pk, name=name, emoji=emoji, price=price)

    def _prizes_with(self, products, total):
        qs = MagicMock()
        qs.count.return_value = total
        qs.__getitem__ = lambda self_, item: products[item]
        with patch(STP + 'story_gifts_for_branch', return_value=qs):
            return ST._prizes(_branch())

    def test_pool_keeps_guest_order_and_first_matches(self):
        products = [self._product(1, 'Кофе'), self._product(2, 'Десерт', price=300)]
        prizes, first = self._prizes_with(products, 2)
        self.assertEqual([p['name'] for p in prizes['pool']], ['Кофе', 'Десерт'])
        self.assertEqual(prizes['pool'][1], {'id': 2, 'name': 'Десерт', 'emoji': '🎁', 'price': 300})
        self.assertEqual(prizes['first'], {'id': 1, 'name': 'Кофе'})
        self.assertEqual(first.name, 'Кофе', 'им же подставляется rendered')

    def test_pool_is_capped_but_count_is_full(self):
        products = [self._product(i, f'Подарок {i}') for i in range(1, 61)]
        prizes, _ = self._prizes_with(products, 60)
        self.assertEqual(len(prizes['pool']), ST.POOL_LIMIT)
        self.assertEqual(prizes['count'], 60, 'count — полный размер пула, а не длина pool')

    def test_empty_pool(self):
        prizes, first = self._prizes_with([], 0)
        self.assertEqual(prizes['pool'], [])
        self.assertIsNone(prizes['first'])
        self.assertIsNone(first)


class StoryImageLivesOnBranchTest(SimpleTestCase):
    """
    Регресс: `story_image` — поле Branch (models.py:63), а НЕ BranchConfig.

    Первая версия ручки читала его с `branch.config` и всегда отдавала `null`;
    у гостя та же картинка берётся как `_image_url(branch.story_image)`
    (branch/api/services.py:632).
    """

    def _url_for(self, branch):
        qs = MagicMock()
        qs.count.return_value = 0
        qs.__getitem__ = lambda self_, item: []
        with patch(STP + 'story_gifts_for_branch', return_value=qs), \
             patch(STP + '_network_domain', return_value='levone.levelupapp.ru'):
            prizes, _ = ST._prizes(branch)
        return prizes['story_image_url']

    def test_image_on_branch_is_used(self):
        image = SimpleNamespace(url='/media/branch/stories/x.png', name='branch/stories/x.png')
        self.assertEqual(self._url_for(_branch(story_image=image)),
                         'https://levone.levelupapp.ru/media/branch/stories/x.png')

    def test_image_on_config_is_ignored(self):
        cfg = _branch_cfg()
        cfg.story_image = SimpleNamespace(url='/media/wrong.png', name='wrong.png')
        self.assertIsNone(self._url_for(_branch(cfg)), 'картинка с config не должна подхватываться')

    def test_empty_file_field_gives_none(self):
        empty = SimpleNamespace(url='', name='')
        self.assertIsNone(self._url_for(_branch(story_image=empty)))
