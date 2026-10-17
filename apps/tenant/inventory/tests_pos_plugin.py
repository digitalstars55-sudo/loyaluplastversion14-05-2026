"""
Плагин iikoFront «LoyalUP» — ручки pos/plugin/* (17.10.2026).

Тенантных таблиц в тестовой БД нет: точка, гость и подарки подменяются там,
где ручки их берут. Правила:
  1) флаг сети выключен — 404 на всё, даже с верным ключом;
  2) без ключа или с чужим — 401; короткий ключ не ищется вовсе;
  3) незнакомая карта — 200 с пустым списком (плагин на любой не-2xx пишет
     «LoyalUP недоступен»);
  4) в списке только активные, не истёкшие подарки; срок строкой для кассира;
  5) redeem выдаёт подарок и помнит заказ, повтор с тем же заказом — ok,
     с другим — 409, неактивный — 409, заказ меньше условия — 409;
  6) release до оплаты подарок не трогает, после выдачи в этом заказе — 409;
  7) ключ точки выдаёт только администратор сети, ключ виден один раз.
"""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone as dt_tz
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant.inventory import pos_plugin as pp
from apps.tenant.inventory.models import ItemStatus

rf = APIRequestFactory()
KEY = 'lup_' + 'x' * 32


class _Item(SimpleNamespace):
    def save(self, update_fields=None):
        self.saved = update_fields


def _item(**kw):
    base = dict(pk=7, used_at=None, pos_order_id='', pos_order_sum=None, pos_branch=None,
                min_order_amount=0, status=ItemStatus.ACTIVE, saved=None)
    base.update(kw)
    return _Item(**base)


class _Base(SimpleTestCase):
    def setUp(self):
        self.branch = SimpleNamespace(branch_id=3, pk=3)
        for p in (patch.object(pp, 'plugin_enabled', return_value=True),
                  patch.object(pp, 'branch_by_key', side_effect=lambda k: self.branch if k == KEY else None),
                  patch.object(pp.transaction, 'atomic', lambda: nullcontext()),
                  patch.object(pp._PluginThrottle, 'allow_request', return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def get(self, card='123', key=KEY):
        req = rf.get('/api/v1/pos/plugin/gifts', {'card': card}, HTTP_X_PLUGIN_KEY=key)
        return pp.PluginGiftsView.as_view()(req)

    def post(self, view, pk, body, key=KEY):
        req = rf.post(f'/api/v1/pos/plugin/gifts/{pk}/x', body, format='json', HTTP_X_PLUGIN_KEY=key)
        return view.as_view()(req, pk=pk)

    def with_item(self, item):
        qs = MagicMock()
        qs.select_for_update.return_value.filter.return_value.first.return_value = item
        qs.filter.return_value.first.return_value = item
        from apps.tenant.inventory import models
        p = patch.object(models.InventoryItem, 'objects', qs)
        p.start()
        self.addCleanup(p.stop)


class Доступ(_Base):
    def test_флаг_выключен_404(self):
        with patch.object(pp, 'plugin_enabled', return_value=False):
            self.assertEqual(self.get().status_code, 404)

    def test_чужой_ключ_401(self):
        self.assertEqual(self.get(key='lup_чужой_ключ_1234567').status_code, 401)


class КороткийКлюч(SimpleTestCase):
    def test_короткий_и_пустой_ключ(self):
        with patch('apps.tenant.branch.models.Branch.objects') as objs:
            self.assertIsNone(pp.branch_by_key(''))
            self.assertIsNone(pp.branch_by_key('short'))
            objs.filter.assert_not_called()

    def test_хеш_стабилен_и_не_равен_ключу(self):
        self.assertEqual(pp.key_hash(KEY), pp.key_hash(KEY))
        self.assertNotIn(KEY, pp.key_hash(KEY))
        self.assertTrue(pp.new_key().startswith('lup_'))


class Подарки(_Base):
    def test_незнакомая_карта_200_пусто(self):
        with patch.object(pp, '_guest_by_card', return_value=None):
            r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['gifts'], [])
        self.assertEqual(r.data['code'], 'card_not_found')

    def test_только_не_истёкшие_и_срок_строкой(self):
        now = datetime.now(dt_tz.utc)
        product = SimpleNamespace(name='Ролл Филадельфия', iiko_product_id='uuid-1')
        живой = SimpleNamespace(pk=1, product=product, expires_at=now + timedelta(minutes=30), min_order_amount=1500)
        истёк = SimpleNamespace(pk=2, product=product, expires_at=now - timedelta(minutes=1), min_order_amount=0)
        бессрочный = SimpleNamespace(pk=3, product=None, expires_at=None, min_order_amount=0)
        qs = MagicMock()
        qs.select_related.return_value.filter.return_value.order_by.return_value = [живой, истёк, бессрочный]
        from apps.tenant.inventory import models
        with patch.object(pp, '_guest_by_card', return_value=SimpleNamespace(first_name='Иван')), \
             patch.object(models.InventoryItem, 'objects', qs):
            r = self.get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['guest'], {'name': 'Иван'})
        self.assertEqual([g['id'] for g in r.data['gifts']], [1, 3])
        g = r.data['gifts'][0]
        self.assertEqual(g['iiko_product_id'], 'uuid-1')
        self.assertEqual(g['min_order'], 1500)
        self.assertRegex(g['expires_at'], r'^\d\d\.\d\d \d\d:\d\d$')
        self.assertIsNone(r.data['gifts'][1]['iiko_product_id'])


class Выдача(_Base):
    def test_выдаёт_и_помнит_заказ(self):
        it = _item()
        self.with_item(it)
        r = self.post(pp.PluginRedeemView, 7, {'order_id': 'o-1', 'order_sum': 2350})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['ok'])
        self.assertEqual(it.pos_order_id, 'o-1')
        self.assertIs(it.pos_branch, self.branch)
        self.assertIsNotNone(it.used_at)
        self.assertIn('used_at', it.saved)

    def test_повтор_тем_же_заказом_ok(self):
        self.with_item(_item(used_at=datetime.now(dt_tz.utc), pos_order_id='o-1'))
        r = self.post(pp.PluginRedeemView, 7, {'order_id': 'o-1', 'order_sum': 2350})
        self.assertEqual((r.status_code, r.data.get('repeat')), (200, True))

    def test_другим_заказом_409(self):
        self.with_item(_item(used_at=datetime.now(dt_tz.utc), pos_order_id='o-1'))
        r = self.post(pp.PluginRedeemView, 7, {'order_id': 'o-2'})
        self.assertEqual((r.status_code, r.data['code']), (409, 'already_used'))

    def test_не_активный_409(self):
        self.with_item(_item(status=ItemStatus.PENDING))
        r = self.post(pp.PluginRedeemView, 7, {'order_id': 'o-1'})
        self.assertEqual((r.status_code, r.data['code']), (409, 'not_active'))

    def test_заказ_меньше_условия_409(self):
        it = _item(min_order_amount=1500)
        self.with_item(it)
        r = self.post(pp.PluginRedeemView, 7, {'order_id': 'o-1', 'order_sum': 900})
        self.assertEqual((r.status_code, r.data['code']), (409, 'min_order'))
        self.assertIsNone(it.used_at)

    def test_без_заказа_400(self):
        self.with_item(_item())
        self.assertEqual(self.post(pp.PluginRedeemView, 7, {}).status_code, 400)

    def test_нет_подарка_404(self):
        self.with_item(None)
        self.assertEqual(self.post(pp.PluginRedeemView, 7, {'order_id': 'o'}).status_code, 404)


class Отмена(_Base):
    def test_до_оплаты_ok_подарок_не_тронут(self):
        it = _item()
        self.with_item(it)
        r = self.post(pp.PluginReleaseView, 7, {'order_id': 'o-1'})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(it.used_at)
        self.assertIsNone(it.saved)

    def test_после_выдачи_в_этом_заказе_409(self):
        self.with_item(_item(used_at=datetime.now(dt_tz.utc), pos_order_id='o-1'))
        r = self.post(pp.PluginReleaseView, 7, {'order_id': 'o-1'})
        self.assertEqual((r.status_code, r.data['code']), (409, 'already_redeemed'))


class КлючТочки(SimpleTestCase):
    def setUp(self):
        for p in (patch('apps.shared.users.access.has_tenant_access', return_value=True),
                  patch('apps.shared.users.access.current_schema_name', return_value='levone'),
                  patch.object(pp, 'plugin_enabled', return_value=False)):
            p.start()
            self.addCleanup(p.stop)
        self.branch = SimpleNamespace(pk=3, branch_id=3, pos_plugin_key_hash='', pos_plugin_key_hint='')
        self.branch.save = MagicMock()
        p = patch('apps.tenant.branch.models.Branch.objects')
        objs = p.start()
        self.addCleanup(p.stop)
        objs.filter.return_value.first.return_value = self.branch

    def call(self, method, user):
        req = getattr(rf, method)('/api/v1/branches/3/pos-plugin-key/')
        force_authenticate(req, user=user)
        return pp.BranchPluginKeyView.as_view()(req, pk=3)

    def test_не_админ_403(self):
        u = SimpleNamespace(is_authenticated=True, is_superuser=False, role='manager', is_network_admin=False)
        self.assertEqual(self.call('post', u).status_code, 403)
        self.branch.save.assert_not_called()

    def test_админ_получает_ключ_один_раз(self):
        u = SimpleNamespace(is_authenticated=True, is_superuser=False, role='network_admin')
        r = self.call('post', u)
        self.assertEqual(r.status_code, 200)
        key = r.data['key']
        self.assertEqual(self.branch.pos_plugin_key_hash, pp.key_hash(key))
        self.assertEqual(r.data['hint'], key[-4:])
        r = self.call('get', u)
        self.assertEqual(r.data, {'has_key': True, 'hint': key[-4:], 'enabled': False})
        self.assertNotIn('key', r.data)
