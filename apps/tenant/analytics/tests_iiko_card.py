"""
iikoCard «только связь» (волна 2 ТЗ маркетолога, 14.10.2026).

Сеть iiko подменена фальшивым HTTP-клиентом; тенантных таблиц в тестовой БД
нет — модели связи подменяются там, где функция их импортирует.

Правила:
  1) «нет гостя» iiko (400 «There is no user…») — статус «не найден», не ошибка;
  2) иная ошибка iiko — статус «ошибка», гость остаётся на перепроверку;
  3) истёк токен (401) — один повторный вход, запрос проходит;
  4) заказ — только операция CloseOrder с posOrderId; orderSum до скидок, sum — оплачено;
  5) страницы идут от курсора, пока страница полная; курсор сохраняется;
  6) сеть с выключенным режимом задача не трогает вовсе.
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.tenant.analytics import iiko_card as ic


class _Resp:
    def __init__(self, status, payload=None, text=''):
        self.status_code = status
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else '')

    def json(self):
        return self._payload


class _Http:
    """Ответы по пути: список — по очереди."""

    def __init__(self, routes):
        self.routes = {k: list(v) if isinstance(v, list) else [v] for k, v in routes.items()}
        self.calls = []

    def post(self, url, json=None, headers=None):
        path = url.split('/api/1/', 1)[1]
        self.calls.append((path, json, headers))
        queue = self.routes[path]
        return queue.pop(0) if len(queue) > 1 else queue[0]


class _Cache(dict):
    def get(self, key, default=None):
        return super().get(key, default)

    def set(self, key, value, ttl=None):
        self[key] = value


def _api(routes):
    http = _Http({'access_token': _Resp(200, {'token': 'T1'}), **routes})
    api = ic.IikoCloudClient('key', http=http)
    api.sleep = lambda s: None          # без настоящих пауз в тестах
    return api, http


class ClientTest(SimpleTestCase):

    def setUp(self):
        p = patch.object(ic, 'cache', _Cache())
        p.start()
        self.addCleanup(p.stop)

    def test_нет_гостя_это_none(self):
        api, _ = _api({'loyalty/iiko/customer/info': _Resp(
            400, text='{"message": "There is no user with phone +70000000001"}')})
        self.assertIsNone(api.customer_by_phone('org', '+70000000001'))

    def test_другая_ошибка_это_исключение(self):
        api, _ = _api({'loyalty/iiko/customer/info': _Resp(500, text='boom')})
        with self.assertRaises(ic.IikoCloudError):
            api.customer_by_phone('org', '+7900')

    def test_401_один_повторный_вход(self):
        api, http = _api({'loyalty/iiko/customer/info': [_Resp(401, text='expired'), _Resp(200, {'id': 'c1'})]})
        self.assertEqual(api.customer_by_phone('org', '+7900'), {'id': 'c1'})
        self.assertEqual([c[0] for c in http.calls].count('access_token'), 2)

    def test_токен_берётся_из_кэша(self):
        api, http = _api({'loyalty/iiko/customer/info': _Resp(200, {'id': 'c1'})})
        api.customer_by_phone('org', '+7900')
        api.customer_by_phone('org', '+7900')
        self.assertEqual([c[0] for c in http.calls].count('access_token'), 1)

    def test_429_ждём_и_повторяем(self):
        api, _ = _api({'loyalty/iiko/customer/info': [_Resp(429, text='slow'), _Resp(200, {'id': 'c1'})]})
        waits = []
        api.sleep = waits.append
        self.assertEqual(api.customer_by_phone('org', '+7900'), {'id': 'c1'})
        self.assertIn(ic.RATE_LIMIT_WAITS[0], waits)

    def test_429_не_проходит_проход_останавливается(self):
        api, _ = _api({'loyalty/iiko/customer/info': _Resp(429, text='slow')})
        with self.assertRaises(ic.IikoCloudRateLimited):
            api.customer_by_phone('org', '+7900')

    def test_пауза_между_запросами(self):
        api, _ = _api({'loyalty/iiko/customer/info': _Resp(200, {'id': 'c1'})})
        waits = []
        api.sleep = waits.append
        api.customer_by_phone('org', '+7900')
        api.customer_by_phone('org', '+7900')
        self.assertTrue(any(0 < w <= ic.MIN_INTERVAL for w in waits))

    def test_разбор_времени_и_денег(self):
        self.assertEqual(ic._dt('2026-06-30T10:50:39.097557Z').utcoffset().total_seconds(), 0)
        self.assertIsNotNone(ic._dt('2022-09-02T22:59:40.323').tzinfo)
        self.assertEqual(ic._money(310.4), Decimal('310.40'))
        self.assertIsNone(ic._money('x'))


class SyncOrdersTest(SimpleTestCase):
    """sync_orders: модели подменены, проверяем, что и как пишется."""

    def setUp(self):
        p = patch.object(ic, 'cache', _Cache())
        p.start()
        self.addCleanup(p.stop)
        self.created = []
        orders = MagicMock()

        def get_or_create(link, pos_order_id, defaults):
            known = {c['pos_order_id'] for c in self.created}
            if pos_order_id in known:
                return None, False
            self.created.append({'pos_order_id': pos_order_id, **defaults})
            return None, True
        orders.objects.get_or_create.side_effect = get_or_create
        mp = patch('apps.tenant.analytics.models.PosGuestOrder', orders)
        mp.start()
        self.addCleanup(mp.stop)
        ta = patch.object(ic.transaction, 'atomic', MagicMock())
        ta.start()
        self.addCleanup(ta.stop)
        self.link = SimpleNamespace(iiko_customer_id='c1', last_revision=None, last_transaction_id='',
                                    orders_synced_at=None, save=MagicMock())
        self.config = SimpleNamespace(iiko_cloud_organization_id='org')

    def _tx(self, kind, pos='p1', order_sum=320, s=310.4, delivery=False):
        return {'typeName': kind, 'posOrderId': pos, 'orderSum': order_sum, 'sum': s,
                'isDelivery': delivery, 'orderNumber': 12, 'organizationId': 'org',
                'whenCreated': '2025-11-21T12:56:54.089958Z'}

    def test_только_закрытые_заказы_и_две_суммы(self):
        api, _ = _api({'loyalty/iiko/customer/transactions/by_revision': _Resp(200, {
            'transactions': [self._tx('DiscountSum', s=9.6), self._tx('CloseOrder')],
            'lastRevision': 5870652087, 'lastTransactionId': 'tx2'})})
        self.assertEqual(ic.sync_orders(self.link, self.config, api), 1)
        self.assertEqual(self.created[0]['order_sum'], Decimal('320.00'))
        self.assertEqual(self.created[0]['paid_sum'], Decimal('310.40'))
        self.assertEqual((self.link.last_revision, self.link.last_transaction_id), (5870652087, 'tx2'))

    def test_страницы_по_курсору_пока_полные(self):
        full = {'transactions': [self._tx('CloseOrder', pos=f'p{i}') for i in range(ic.PAGE_SIZE)],
                'lastRevision': 10, 'lastTransactionId': 'a'}
        tail = {'transactions': [self._tx('CloseOrder', pos='last')], 'lastRevision': 11, 'lastTransactionId': 'b'}
        api, http = _api({'loyalty/iiko/customer/transactions/by_revision': [_Resp(200, full), _Resp(200, tail)]})
        self.assertEqual(ic.sync_orders(self.link, self.config, api), ic.PAGE_SIZE + 1)
        bodies = [c[1] for c in http.calls if c[0].endswith('by_revision')]
        self.assertEqual(bodies[1]['revision'], 10)
        self.assertEqual(bodies[1]['lastTransactionId'], 'a')
        self.assertEqual(self.link.last_revision, 11)

    def test_повтор_не_задваивает(self):
        page = {'transactions': [self._tx('CloseOrder')], 'lastRevision': 1, 'lastTransactionId': 'x'}
        api, _ = _api({'loyalty/iiko/customer/transactions/by_revision': _Resp(200, page)})
        ic.sync_orders(self.link, self.config, api)
        self.assertEqual(ic.sync_orders(self.link, self.config, api), 0)
        self.assertEqual(len(self.created), 1)


class LinkGuestTest(SimpleTestCase):

    def setUp(self):
        p = patch.object(ic, 'cache', _Cache())
        p.start()
        self.addCleanup(p.stop)
        self.link = MagicMock(iiko_customer_id='', linked_at=None)
        model = MagicMock()
        model.objects.filter.return_value.first.return_value = self.link
        mp = patch('apps.tenant.analytics.models.PosGuestLink', model)
        mp.start()
        self.addCleanup(mp.stop)
        self.client_obj = SimpleNamespace(phone='+79001112233')
        self.config = SimpleNamespace(iiko_cloud_organization_id='org')

    def test_найден(self):
        api, _ = _api({'loyalty/iiko/customer/info': _Resp(200, {
            'id': 'c1', 'walletBalances': [{'name': 'Бонусы LevOne', 'type': 1, 'balance': 45}],
            'firstOrderDate': '2022-09-02T22:59:40.323', 'lastProcessedOrderDate': '2026-06-30T13:50:39'})})
        link = ic.link_guest(self.client_obj, self.config, api)
        self.assertEqual(link.status, 'linked')
        self.assertEqual(link.iiko_customer_id, 'c1')
        self.assertEqual(link.wallets, [{'name': 'Бонусы LevOne', 'type': 1, 'balance': 45}])
        self.assertEqual(link.phone_checked, '+79001112233')

    def test_не_найден(self):
        api, _ = _api({'loyalty/iiko/customer/info': _Resp(400, text='There is no user with phone')})
        self.assertEqual(ic.link_guest(self.client_obj, self.config, api).status, 'not_found')

    def test_ошибка_iiko(self):
        api, _ = _api({'loyalty/iiko/customer/info': _Resp(503, text='down')})
        link = ic.link_guest(self.client_obj, self.config, api)
        self.assertEqual(link.status, 'error')
        self.assertIn('503', link.error)


class TaskTest(SimpleTestCase):

    def test_выключенную_сеть_не_трогаем(self):
        from apps.tenant.analytics import tasks
        off = SimpleNamespace(schema_name='levone', config=SimpleNamespace(
            iiko_card_mode='off', iiko_cloud_api_login='k', iiko_cloud_organization_id='o'))
        unset = SimpleNamespace(schema_name='asap', config=SimpleNamespace(
            iiko_card_mode='link', iiko_cloud_api_login='', iiko_cloud_organization_id=''))
        qs = MagicMock()
        qs.select_related.return_value = [off, unset]
        with patch.object(tasks, 'beat_tenants', return_value=qs), \
                patch('apps.tenant.analytics.iiko_card.run_tenant') as run:
            out = tasks.sync_iiko_card_all_tenants_task()
        run.assert_not_called()
        self.assertEqual(out, {'asap': 'не настроено'})
