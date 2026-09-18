"""
Тесты JSON-API «Каталога наград» для кабинета CheckUp (контракт №49).

Стратегия патчей — как в senler/tests_broadcasts_api.py и
branch/tests_contact_points_api.py: модели тенантные, их таблиц в тестовой БД
нет, поэтому в ORM не ходим совсем. Вьюхи импортируют модели и помощники на
уровне модуля, значит подменяем их прямо в
apps.tenant.inventory.api.reward_catalog.

Главное здесь — `LegacyShapeTest`. Новый модуль ПЕРЕКРЫВАЕТ путь старой ручки
`analytics/rf/reward-catalog/` (include стоит выше в main/urls.py), и если из
ответа пропадёт хоть один старый ключ, молча сломается веб RFM, который уже
живёт на этой ручке. Форма прибита гвоздями намеренно.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.db.models import Q
from django.test import SimpleTestCase
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from apps.tenant.inventory.api import reward_catalog as RC

RCP = 'apps.tenant.inventory.api.reward_catalog.'
URL = '/api/v1/analytics/rf/reward-catalog/'
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=dt_timezone.utc)

# Ключи и типы старой ручки analytics/api/views.py:1746 — ответ обязан быть
# её надмножеством.
LEGACY_KEYS = {
    'id': int, 'name': str, 'tier': str, 'cost_price': float,
    'min_order_amount': float, 'default_lifetime_days': int,
    'remaining_issues': (int, type(None)), 'branch': (str, type(None)),
}


# ── стабы ────────────────────────────────────────────────────────────────────

def _branch(pk=3, name='Институтская'):
    return SimpleNamespace(pk=pk, id=pk, name=name, branch_id=202)


def _product(pk=7, name='Капучино'):
    return SimpleNamespace(pk=pk, id=pk, name=name, is_archived=False)


_DEFAULT = object()


def _item(pk=1, branch=None, product=_DEFAULT, **over):
    """Позиция каталога: те же атрибуты, что читает _card (product=None — без подарка)."""
    product = _product() if product is _DEFAULT else product
    values = dict(
        pk=pk, id=pk, tier='G1', name='', internal_code='RC-1',
        description='', cost_price=Decimal('0'), min_order_amount=Decimal('500'),
        weight=1, default_lifetime_days=10, activation_limit=None, issued_count=0,
        available_from=None, available_to=None,
        is_active=True, is_archived=False, available_for_rfm=True,
        created_at=NOW, updated_at=NOW,
        display_name=(product.name if product else 'Награда'),
        display_description='', display_image=None,
        effective_cost_price=Decimal('120.50'), remaining_issues=None,
    )
    values.update(over)
    item = SimpleNamespace(
        product=product, product_id=(product.pk if product else None),
        branch=branch, branch_id=(branch.pk if branch else None),
        save=MagicMock(), delete=MagicMock(), **values)
    item.is_available_now = lambda at=None: bool(item.is_active and not item.is_archived)
    return item


class _QS:
    """Мини-queryset: count() + срез, как ждёт вьюха списка."""

    def __init__(self, rows):
        self.rows = list(rows)

    def count(self):
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def __getitem__(self, item):
        return self.rows[item]


def _user(role='network_admin', is_superuser=False):
    return SimpleNamespace(
        is_authenticated=True, is_active=True, is_superuser=is_superuser,
        is_superadmin=(role == 'superadmin'), is_network_admin=(role == 'network_admin'),
        is_client=(role == 'client'), username='t', pk=1, is_staff=True, role=role)


def _call(view_cls, method, path=URL, *, data=None, params=None, user=None, **kwargs):
    factory = APIRequestFactory()
    if method == 'get':
        request = factory.get(path, params or {})
    elif method == 'delete':
        request = factory.delete(path)
    else:
        request = getattr(factory, method)(path, data or {}, format='json')
    force_authenticate(request, user=user or _user())
    return view_cls.as_view()(request, **kwargs)


def _zero_use(ids, now=None):
    return {int(i): {'campaigns': 0, 'live_gifts': 0} for i in (ids or [])}


# ── 1. Доступ ────────────────────────────────────────────────────────────────

class AuthTest(SimpleTestCase):
    """Правило CLAUDE.md: у DRF дефолт AllowAny, поэтому проверяем явно."""

    def test_every_view_requires_auth(self):
        views = [v for v in vars(RC).values()
                 if isinstance(v, type) and issubclass(v, APIView) and v is not APIView]
        self.assertEqual(len(views), 2)
        for view in views:
            self.assertIn(IsAuthenticated, view.permission_classes, view.__name__)

    def test_anonymous_is_rejected(self):
        factory = APIRequestFactory()
        list_view = RC.RewardCatalogListCreateAPIView.as_view()
        detail_view = RC.RewardCatalogDetailAPIView.as_view()
        cases = [
            list_view(factory.get(URL)),
            list_view(factory.post(URL, {}, format='json')),
            detail_view(factory.get(URL + '1/'), pk=1),
            detail_view(factory.patch(URL + '1/', {}, format='json'), pk=1),
            detail_view(factory.delete(URL + '1/'), pk=1),
        ]
        for resp in cases:
            self.assertIn(resp.status_code, (401, 403))


# ── 2. Карточка и форма старой ручки ─────────────────────────────────────────

class CardShapeTest(SimpleTestCase):

    def test_card_keys(self):
        card = RC._card(_item(branch=_branch()), {'campaigns': 1, 'live_gifts': 2})
        self.assertEqual(sorted(card), sorted([
            'id', 'name', 'tier', 'tier_label', 'product', 'product_id', 'internal_code',
            'description', 'image_url', 'cost_price', 'min_order_amount', 'weight',
            'default_lifetime_days', 'activation_limit', 'issued_count', 'remaining_issues',
            'available_from', 'available_to', 'branch_id', 'branch', 'is_active',
            'is_archived', 'available_for_rfm', 'is_available_now', 'in_use',
            'created_at', 'updated_at',
        ]))
        self.assertEqual(card['branch_id'], 3)          # внутренний id
        self.assertEqual(card['branch'], 'Институтская')  # старый ключ — имя
        self.assertEqual(card['product'], {'id': 7, 'name': 'Капучино'})
        self.assertEqual(card['in_use'], {'campaigns': 1, 'live_gifts': 2})
        self.assertEqual(card['tier_label'], RC.TIER_LABELS['G1'])

    def test_network_item_has_no_branch(self):
        card = RC._card(_item())
        self.assertIsNone(card['branch_id'])
        self.assertIsNone(card['branch'])
        self.assertEqual(card['in_use'], {'campaigns': 0, 'live_gifts': 0})

    def test_cost_price_is_effective_float(self):
        card = RC._card(_item(effective_cost_price=Decimal('120.50')))
        self.assertEqual(card['cost_price'], 120.5)
        self.assertIsInstance(card['cost_price'], float)

    def test_item_without_product(self):
        card = RC._card(_item(product=None, display_name='Награда #1'))
        self.assertIsNone(card['product'])
        self.assertIsNone(card['product_id'])


class LegacyShapeTest(SimpleTestCase):
    """Старые ключи ручки analytics не должны исчезнуть или сменить тип."""

    def test_card_is_superset_of_legacy(self):
        card = RC._card(_item(branch=_branch(), remaining_issues=5))
        for key, kind in LEGACY_KEYS.items():
            self.assertIn(key, card)
            self.assertIsInstance(card[key], kind, key)

    def test_response_keeps_items_key(self):
        with patch(RCP + '_allowed_branches', return_value=None), \
             patch(RCP + '_catalog_queryset', return_value=_QS([_item()])), \
             patch(RCP + '_in_use_map', side_effect=_zero_use):
            resp = _call(RC.RewardCatalogListCreateAPIView, 'get')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sorted(resp.data), ['items', 'limit', 'offset', 'tiers', 'total'])
        for key in LEGACY_KEYS:
            self.assertIn(key, resp.data['items'][0])
        self.assertEqual(resp.data['tiers'], RC.TIERS)


# ── 3. Список: фильтры, страница, RBAC ───────────────────────────────────────

class ListTest(SimpleTestCase):

    def _get(self, params=None, rows=None, user=None):
        rows = rows if rows is not None else [_item(pk=i) for i in (1, 2, 3)]
        self.captured = {}

        def fake_qs(**kwargs):
            self.captured = kwargs
            return _QS(rows)

        with patch(RCP + '_allowed_branches', return_value=None), \
             patch(RCP + '_catalog_queryset', side_effect=fake_qs), \
             patch(RCP + '_in_use_map', side_effect=_zero_use):
            return _call(RC.RewardCatalogListCreateAPIView, 'get', params=params or {},
                         user=user)

    def test_default_mode_is_the_old_handler(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.captured['include_inactive'])
        self.assertFalse(self.captured['include_archived'])
        self.assertEqual(self.captured['tiers'], [])
        self.assertIsNone(self.captured['branch_scope'])

    def test_include_flags(self):
        self._get({'include_inactive': '1', 'include_archived': 'true'})
        self.assertTrue(self.captured['include_inactive'])
        self.assertTrue(self.captured['include_archived'])

    def test_tier_filter(self):
        self._get({'tier': 'G1,G3'})
        self.assertEqual(self.captured['tiers'], ['G1', 'G3'])

    def test_unknown_tier_is_400(self):
        resp = self._get({'tier': 'G9'})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'tier_invalid'))

    def test_branch_ids_go_through_rbac(self):
        with patch(RCP + 'effective_branch_ids', return_value=[3]) as eff:
            self._get({'branch_ids': '3,4'})
        self.assertEqual(self.captured['branch_scope'], [3])
        self.assertEqual(eff.call_args[0][2], [3, 4])

    def test_branch_ids_garbage_is_400(self):
        resp = self._get({'branch_ids': 'abc'})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))

    def test_without_limit_returns_everything(self):
        resp = self._get()
        self.assertEqual(len(resp.data['items']), 3)
        self.assertEqual((resp.data['total'], resp.data['limit'], resp.data['offset']), (3, 3, 0))

    def test_offset_without_limit(self):
        resp = self._get({'offset': '2'})
        self.assertEqual([i['id'] for i in resp.data['items']], [3])
        self.assertEqual((resp.data['total'], resp.data['limit'], resp.data['offset']), (3, 1, 2))

    def test_limit_and_offset(self):
        resp = self._get({'limit': '1', 'offset': '1'})
        self.assertEqual([i['id'] for i in resp.data['items']], [2])
        self.assertEqual((resp.data['total'], resp.data['limit'], resp.data['offset']), (3, 1, 1))


class VisibilityTest(SimpleTestCase):
    """Сетевые позиции видны всем; точечные — только своим точкам."""

    def test_unrestricted_user_has_no_filter(self):
        self.assertIsNone(RC._visibility_q(None))

    def test_restricted_user_sees_network_and_own(self):
        self.assertEqual(RC._visibility_q([3, 4]),
                         Q(branch__isnull=True) | Q(branch_id__in=[3, 4]))

    def test_queryset_applies_visibility(self):
        model = MagicMock()
        with patch(RCP + 'RewardCatalogItem', model):
            RC._catalog_queryset(allowed=[3], now=NOW)
        first = model.objects.select_related.return_value.filter
        self.assertEqual(first.call_args[0][0],
                         Q(branch__isnull=True) | Q(branch_id__in=[3]))

    def test_foreign_item_is_404(self):
        with patch(RCP + '_allowed_branches', return_value=[3]), \
             patch(RCP + '_get_item', return_value=None):
            resp = _call(RC.RewardCatalogDetailAPIView, 'get', URL + '9/', pk=9)
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))


# ── 4. Создание ──────────────────────────────────────────────────────────────

class CreateTest(SimpleTestCase):

    BODY = {'product_id': 7, 'tier': 'G1', 'branch_id': 3, 'name': 'Кофе в подарок'}

    def _post(self, body=None, *, user=None, allowed=None, branch=None, product=None):
        model = MagicMock()
        created = _item(branch=branch, product=product or _product())
        model.objects.create.return_value = created
        self.model = model
        with patch(RCP + '_allowed_branches', return_value=allowed), \
             patch(RCP + 'RewardCatalogItem', model), \
             patch(RCP + '_branch_or_none', return_value=branch), \
             patch(RCP + '_product_or_none', return_value=product or _product()), \
             patch(RCP + '_in_use_map', side_effect=_zero_use):
            return _call(RC.RewardCatalogListCreateAPIView, 'post',
                         data=body if body is not None else dict(self.BODY), user=user)

    def test_created(self):
        resp = self._post(branch=_branch())
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['branch_id'], 3)
        self.assertTrue(self.model.objects.create.called)

    def test_client_cannot_create(self):
        resp = self._post(user=_user(role='client'))
        self.assertEqual((resp.status_code, resp.data['code']), (403, 'role_not_allowed'))

    def test_network_item_needs_unrestricted_admin(self):
        body = {'product_id': 7, 'tier': 'G1'}
        resp = self._post(body, allowed=[3])
        self.assertEqual((resp.status_code, resp.data['code']), (403, 'role_not_allowed'))

    def test_network_item_created_by_network_admin(self):
        resp = self._post({'product_id': 7, 'tier': 'G1'}, allowed=None)
        self.assertEqual(resp.status_code, 201)
        self.assertIsNone(resp.data['branch_id'])

    def test_product_required(self):
        resp = self._post({'tier': 'G1'})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'product_required'))

    def test_product_null_is_product_required(self):
        resp = self._post({'product_id': None, 'tier': 'G1'})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'product_required'))

    def test_tier_required_and_validated(self):
        self.assertEqual(self._post({'product_id': 7}).data['code'], 'tier_invalid')
        self.assertEqual(self._post({'product_id': 7, 'tier': 'G9'}).data['code'], 'tier_invalid')

    def test_unknown_key_is_400_with_editable(self):
        resp = self._post({'product_id': 7, 'tier': 'G1', 'is_super': True})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))
        self.assertEqual(resp.data['editable'], list(RC.CREATE_FIELDS))
        self.assertIn('is_super', resp.data['detail'])

    def test_issued_count_is_read_only(self):
        resp = self._post({'product_id': 7, 'tier': 'G1', 'issued_count': 100})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))
        self.assertEqual(resp.data['read_only'], ['issued_count'])

    def test_image_is_not_accepted_in_v1(self):
        resp = self._post({'product_id': 7, 'tier': 'G1', 'image': 'x.png'})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))
        self.assertIn('image', resp.data['detail'])

    def test_negative_numbers_rejected(self):
        for body in ({'product_id': 7, 'tier': 'G1', 'cost_price': -1},
                     {'product_id': 7, 'tier': 'G1', 'weight': -2},
                     {'product_id': 7, 'tier': 'G1', 'min_order_amount': '-0.5'}):
            resp = self._post(body)
            self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'), body)

    def test_period_invalid(self):
        resp = self._post({'product_id': 7, 'tier': 'G1',
                           'available_from': '2026-10-01T00:00:00+03:00',
                           'available_to': '2026-09-01T00:00:00+03:00'})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'period_invalid'))

    def test_foreign_branch_is_404(self):
        resp = self._post(dict(self.BODY), allowed=[9], branch=None)
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))

    def test_archived_product_is_404(self):
        with patch(RCP + '_allowed_branches', return_value=None), \
             patch(RCP + 'RewardCatalogItem', MagicMock()), \
             patch(RCP + '_branch_or_none', return_value=_branch()), \
             patch(RCP + '_product_or_none', return_value=None):
            resp = _call(RC.RewardCatalogListCreateAPIView, 'post', data=dict(self.BODY))
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))


# ── 5. Правка ────────────────────────────────────────────────────────────────

class PatchTest(SimpleTestCase):

    def _patch(self, body, *, item=None, use=None, user=None, warning=None):
        item = item or _item()
        self.item = item
        usage = use or {'campaigns': 0, 'live_gifts': 0}
        with patch(RCP + '_allowed_branches', return_value=None), \
             patch(RCP + '_get_item', return_value=item), \
             patch(RCP + '_in_use_map', return_value={item.pk: usage}), \
             patch(RCP + '_processing_campaigns',
                   return_value=[{'id': 5, 'name': 'RFM / Засыпающие', 'status': 'processing'}]), \
             patch(RCP + '_product_or_none', return_value=_product(pk=8, name='Латте')), \
             patch(RCP + '_branch_or_none', return_value=_branch(pk=4, name='Ленина')), \
             patch(RCP + '_tier_warning', return_value=warning):
            return _call(RC.RewardCatalogDetailAPIView, 'patch', URL + '1/',
                         data=body, user=user, pk=1)

    def test_client_cannot_patch(self):
        resp = self._patch({'name': 'x'}, user=_user(role='client'))
        self.assertEqual((resp.status_code, resp.data['code']), (403, 'role_not_allowed'))

    def test_changed_list_and_save_fields(self):
        resp = self._patch({'name': 'Новое', 'weight': 5})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sorted(resp.data['changed']), ['name', 'weight'])
        fields = self.item.save.call_args[1]['update_fields']
        self.assertEqual(sorted(fields), ['name', 'updated_at', 'weight'])

    def test_same_value_is_not_changed(self):
        resp = self._patch({'weight': 1})
        self.assertEqual(resp.data['changed'], [])
        self.assertFalse(self.item.save.called)

    def test_empty_body_is_400(self):
        resp = self._patch({})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))

    def test_issued_count_is_read_only(self):
        resp = self._patch({'issued_count': 0})
        self.assertEqual(resp.data['read_only'], ['issued_count'])

    def test_limit_below_issued_is_409(self):
        resp = self._patch({'activation_limit': 3}, item=_item(issued_count=5))
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'limit_below_issued'))
        self.assertEqual(resp.data['issued_count'], 5)

    def test_limit_equal_to_issued_is_allowed(self):
        resp = self._patch({'activation_limit': 5}, item=_item(issued_count=5))
        self.assertEqual(resp.status_code, 200)

    def test_locked_fields_while_in_use(self):
        resp = self._patch({'tier': 'G3'}, use={'campaigns': 1, 'live_gifts': 12})
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'in_use'))
        self.assertEqual(resp.data['blocked_fields'], ['tier'])
        self.assertEqual(resp.data['live_gifts'], 12)
        self.assertEqual(resp.data['campaigns'],
                         [{'id': 5, 'name': 'RFM / Засыпающие', 'status': 'processing'}])

    def test_live_gifts_alone_block_product_change(self):
        resp = self._patch({'product_id': 8, 'branch_id': 4},
                           use={'campaigns': 0, 'live_gifts': 1})
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'in_use'))
        self.assertEqual(resp.data['blocked_fields'], ['product_id', 'branch_id'])

    def test_free_fields_are_editable_while_in_use(self):
        resp = self._patch({'name': 'Новое имя', 'is_active': False},
                           use={'campaigns': 1, 'live_gifts': 3})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(sorted(resp.data['changed']), ['is_active', 'name'])

    def test_locked_field_with_same_value_passes(self):
        resp = self._patch({'tier': 'G1', 'name': 'Имя'},
                           use={'campaigns': 1, 'live_gifts': 0})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['changed'], ['name'])

    def test_product_change_resolves_new_product(self):
        resp = self._patch({'product_id': 8})
        self.assertEqual(resp.data['changed'], ['product_id'])
        self.assertEqual(self.item.product.name, 'Латте')
        self.assertIn('product', self.item.save.call_args[1]['update_fields'])

    def test_deactivation_reports_last_item_in_tier(self):
        resp = self._patch({'is_active': False},
                           warning={'warning': 'last_item_in_tier',
                                    'rules': [{'id': 2, 'name': 'Реактивация 30 дней'}]})
        self.assertEqual(resp.data['warning'], 'last_item_in_tier')
        self.assertEqual(resp.data['rules'], [{'id': 2, 'name': 'Реактивация 30 дней'}])

    def test_no_warning_on_plain_rename(self):
        resp = self._patch({'name': 'Просто имя'},
                           warning={'warning': 'last_item_in_tier', 'rules': [{'id': 2, 'name': 'r'}]})
        self.assertNotIn('warning', resp.data)


# ── 6. Архив вместо удаления ─────────────────────────────────────────────────

class DeleteTest(SimpleTestCase):

    def _delete(self, *, use=None, user=None, warning=None, item=None):
        item = item or _item()
        self.item = item
        usage = use or {'campaigns': 0, 'live_gifts': 0}
        with patch(RCP + '_allowed_branches', return_value=None), \
             patch(RCP + '_get_item', return_value=item), \
             patch(RCP + '_in_use_map', return_value={item.pk: usage}), \
             patch(RCP + '_processing_campaigns',
                   return_value=[{'id': 5, 'name': 'RFM', 'status': 'processing'}]), \
             patch(RCP + '_tier_warning', return_value=warning):
            return _call(RC.RewardCatalogDetailAPIView, 'delete', URL + '1/', user=user, pk=1)

    def test_archives_and_never_deletes(self):
        resp = self._delete()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {'id': 1, 'is_archived': True, 'is_active': False})
        self.assertTrue(self.item.is_archived)
        self.assertFalse(self.item.is_active)
        self.assertFalse(self.item.delete.called)
        self.assertEqual(sorted(self.item.save.call_args[1]['update_fields']),
                         ['is_active', 'is_archived', 'updated_at'])

    def test_client_cannot_archive(self):
        resp = self._delete(user=_user(role='client'))
        self.assertEqual((resp.status_code, resp.data['code']), (403, 'role_not_allowed'))

    def test_processing_campaign_blocks(self):
        resp = self._delete(use={'campaigns': 1, 'live_gifts': 0})
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'in_use'))
        self.assertEqual(resp.data['campaigns'][0]['id'], 5)
        self.assertFalse(self.item.save.called)

    def test_live_gifts_do_not_block_archive(self):
        resp = self._delete(use={'campaigns': 0, 'live_gifts': 40})
        self.assertEqual(resp.status_code, 200)

    def test_warning_about_last_item_in_tier(self):
        resp = self._delete(warning={'warning': 'last_item_in_tier',
                                     'rules': [{'id': 2, 'name': 'Реактивация'}]})
        self.assertEqual(resp.data['warning'], 'last_item_in_tier')
        self.assertEqual(resp.data['rules'], [{'id': 2, 'name': 'Реактивация'}])

    def test_missing_item_is_404(self):
        with patch(RCP + '_allowed_branches', return_value=None), \
             patch(RCP + '_get_item', return_value=None):
            resp = _call(RC.RewardCatalogDetailAPIView, 'delete', URL + '9/', pk=9)
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))


# ── 7. Предупреждение по тиру (настоящая функция) ────────────────────────────

class TierWarningTest(SimpleTestCase):

    def _run(self, *, left, rules):
        model = MagicMock()
        model.objects.filter.return_value.exclude.return_value.count.return_value = left
        rule_model = MagicMock()
        rule_model.objects.filter.return_value = rules
        with patch(RCP + 'RewardCatalogItem', model), \
             patch(RCP + 'AutoBroadcastRule', rule_model):
            return RC._tier_warning(_item(tier='G1'))

    def test_silent_while_tier_has_items(self):
        rule = SimpleNamespace(pk=2, name='Реактивация', gift_tier='G1')
        self.assertIsNone(self._run(left=1, rules=[rule]))

    def test_warns_when_tier_is_empty(self):
        rules = [SimpleNamespace(pk=2, name='Реактивация', gift_tier='G1,G2'),
                 SimpleNamespace(pk=3, name='Welcome', gift_tier=' g1 ')]
        out = self._run(left=0, rules=rules)
        self.assertEqual(out, {'warning': 'last_item_in_tier',
                               'rules': [{'id': 2, 'name': 'Реактивация'},
                                         {'id': 3, 'name': 'Welcome'}]})

    def test_other_tier_rule_is_not_reported(self):
        # icontains нашёл бы 'G10'/'G12' — разбор строки отсекает не тот тир.
        rule = SimpleNamespace(pk=4, name='Чужое', gift_tier='G2,G3')
        self.assertIsNone(self._run(left=0, rules=[rule]))

    def test_no_active_rules_means_no_warning(self):
        self.assertIsNone(self._run(left=0, rules=[]))


# ── 8. Разбор тела и занятость ───────────────────────────────────────────────

class ParseTest(SimpleTestCase):

    def test_money(self):
        self.assertEqual(RC._parse_money('cost_price', '120.5'), Decimal('120.50'))
        self.assertEqual(RC._parse_money('cost_price', None), Decimal('0'))
        with self.assertRaises(RC.PayloadError):
            RC._parse_money('cost_price', 'дорого')

    def test_int(self):
        self.assertEqual(RC._parse_int('weight', '3'), 3)
        self.assertIsNone(RC._parse_int('activation_limit', None, allow_null=True))
        with self.assertRaises(RC.PayloadError):
            RC._parse_int('weight', True)

    def test_bool(self):
        self.assertTrue(RC._parse_bool('is_active', 'true'))
        self.assertFalse(RC._parse_bool('is_active', 0))
        with self.assertRaises(RC.PayloadError):
            RC._parse_bool('is_active', 'может быть')

    def test_datetime_is_aware(self):
        value = RC._parse_dt('available_from', '2026-09-20T10:00:00')
        self.assertIsNotNone(value.tzinfo)
        self.assertIsNone(RC._parse_dt('available_from', None))
        with self.assertRaises(RC.PayloadError):
            RC._parse_dt('available_from', 'завтра')

    def test_tier_is_case_insensitive(self):
        self.assertEqual(RC._parse_tier('g2'), 'G2')

    def test_tiers_match_the_model(self):
        from apps.tenant.inventory.models import RewardTier
        self.assertEqual([t['code'] for t in RC.TIERS], list(RewardTier.values))


class LiveGiftQTest(SimpleTestCase):
    """Условие «живого подарка» копирует гейт движка (senler/engine.py:694)."""

    def test_matches_engine(self):
        expected = (
            Q(activated_at__isnull=True)
            & (Q(claim_expires_at__isnull=True) | Q(claim_expires_at__gt=NOW))
            | Q(activated_at__isnull=False)
            & (Q(expires_at__isnull=True) | Q(expires_at__gt=NOW))
        )
        self.assertEqual(RC._live_gift_q(NOW), expected)

    def test_in_use_map_counts_both_sources(self):
        campaigns = MagicMock()
        campaigns.objects.filter.return_value.values.return_value.annotate.return_value = [
            {'catalog_item_id': 1, 'n': 2}]
        items = MagicMock()
        items.objects.filter.return_value.filter.return_value.values.return_value.annotate.return_value = [
            {'catalog_item_id': 1, 'n': 7}]
        with patch(RCP + 'RFMCampaign', campaigns), patch(RCP + 'InventoryItem', items):
            out = RC._in_use_map([1, 2], now=NOW)
        self.assertEqual(out[1], {'campaigns': 2, 'live_gifts': 7})
        self.assertEqual(out[2], {'campaigns': 0, 'live_gifts': 0})

    def test_empty_ids_never_hit_db(self):
        with patch(RCP + 'RFMCampaign', MagicMock()) as campaigns:
            self.assertEqual(RC._in_use_map([]), {})
            self.assertFalse(campaigns.objects.filter.called)
