"""
Tests for apps.tenant.catalog — models, services, serializers.

SimpleTestCase (no DB) is used for everything except tests that call functions
decorated with @transaction.atomic (buy_product, claim_super_prize), which need
a real DB connection and therefore use TestCase — matching the existing project
pattern in inventory/tests.py.
"""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase


# ── Patch paths ───────────────────────────────────────────────────────────────

_SVC        = 'apps.tenant.catalog.api.services'
_BRANCH_OBJ = f'{_SVC}.Branch.objects'
_CB_MODEL   = f'{_SVC}.ClientBranch'
_COOLDOWN   = f'{_SVC}.Cooldown'
_PROD_MODEL = f'{_SVC}.Product'
_COIN_MODEL = f'{_SVC}.CoinTransaction'
_ITEM_MODEL = f'{_SVC}.InventoryItem'


# ── Shared factories ──────────────────────────────────────────────────────────

def _branch(is_active=True, branch_id=1):
    b = MagicMock()
    b.is_active = is_active
    b.branch_id = branch_id
    return b


def _cb(branch=None, vk_id=11111, branch_id=1):
    cb = MagicMock()
    cb.branch = branch or _branch(branch_id=branch_id)
    cb.client.vk_id = vk_id
    return cb


def _product(pk=1, price=50, is_active=True):
    p = MagicMock()
    p.pk = pk
    p.price = price
    p.is_active = is_active
    return p


# ── Product model ─────────────────────────────────────────────────────────────

class ProductStrTest(SimpleTestCase):

    def test_str_with_price(self):
        from apps.tenant.catalog.models import Product
        p = Product(name='Латте', price=120)
        self.assertEqual(str(p), 'Латте (120 ★)')

    def test_str_free(self):
        from apps.tenant.catalog.models import Product
        p = Product(name='Стикер', price=0)
        self.assertEqual(str(p), 'Стикер (0 ★)')


# ── ProductBranch model ───────────────────────────────────────────────────────

class ProductBranchStrTest(SimpleTestCase):
    """
    Django's FK descriptor rejects MagicMock assignments, so we bypass it via
    set_cached_value — the internal mechanism the ORM itself uses for eager
    loading.
    """

    def _make_pb(self, product_name, branch_str):
        from apps.tenant.catalog.models import ProductBranch
        pb = ProductBranch()
        FakeProduct = type('P', (), {'name': product_name})
        FakeBranch  = type('B', (), {'__str__': lambda _: branch_str})
        ProductBranch._meta.get_field('product').set_cached_value(pb, FakeProduct())
        ProductBranch._meta.get_field('branch').set_cached_value(pb,  FakeBranch())
        return pb

    def test_str_contains_product_name_and_branch(self):
        pb = self._make_pb('Капучино', 'Кофе-Хаус')
        self.assertIn('Капучино', str(pb))
        self.assertIn('Кофе-Хаус', str(pb))

    def test_str_format(self):
        pb = self._make_pb('Пирог', 'Точка №2')
        self.assertEqual(str(pb), 'Пирог → Точка №2')


# ── get_active_products ───────────────────────────────────────────────────────

class GetActiveProductsTest(SimpleTestCase):

    def _call(self, branch_id=1):
        from apps.tenant.catalog.api.services import get_active_products
        return get_active_products(branch_id)

    @patch(_BRANCH_OBJ)
    def test_raises_branch_not_found(self, mock_branch):
        from apps.tenant.catalog.api.services import BranchNotFound
        from apps.tenant.branch.models import Branch
        mock_branch.get.side_effect = Branch.DoesNotExist
        with self.assertRaises(BranchNotFound):
            self._call(999)

    @patch(_BRANCH_OBJ)
    def test_raises_branch_inactive(self, mock_branch):
        from apps.tenant.catalog.api.services import BranchInactive
        branch = _branch(is_active=False)
        mock_branch.get.return_value = branch
        with self.assertRaises(BranchInactive):
            self._call()

    @patch(_PROD_MODEL + '.objects')
    @patch(_BRANCH_OBJ)
    def test_filters_by_branch_assignments_not_branch(self, mock_branch, mock_prod):
        """
        CRITICAL: after M2M refactor, the filter must use branch_assignments__branch,
        NOT the old branch= FK lookup.
        """
        branch = _branch()
        mock_branch.get.return_value = branch
        mock_prod.filter.return_value.annotate.return_value.order_by.return_value = MagicMock()

        self._call()

        call_kwargs = mock_prod.filter.call_args.kwargs
        self.assertIn('branch_assignments__branch', call_kwargs,
                      'Must filter via M2M through-table, not direct FK')
        self.assertNotIn('branch', call_kwargs,
                         'Old FK field branch no longer exists on Product')
        self.assertEqual(call_kwargs['branch_assignments__branch'], branch)

    @patch(_PROD_MODEL + '.objects')
    @patch(_BRANCH_OBJ)
    def test_filters_only_active_assignments(self, mock_branch, mock_prod):
        branch = _branch()
        mock_branch.get.return_value = branch
        mock_prod.filter.return_value.annotate.return_value.order_by.return_value = MagicMock()

        self._call()

        call_kwargs = mock_prod.filter.call_args.kwargs
        self.assertIs(call_kwargs.get('branch_assignments__is_active'), True)

    @patch(_PROD_MODEL + '.objects')
    @patch(_BRANCH_OBJ)
    def test_annotates_branch_category_and_ordering(self, mock_branch, mock_prod):
        """Queryset must carry branch_category_id/name/ordering and branch_ordering annotations."""
        branch = _branch()
        mock_branch.get.return_value = branch
        annotated = MagicMock()
        mock_prod.filter.return_value.annotate.return_value = annotated
        annotated.order_by.return_value = MagicMock()

        self._call()

        annotate_kwargs = mock_prod.filter.return_value.annotate.call_args.kwargs
        self.assertIn('branch_category_id', annotate_kwargs)
        self.assertIn('branch_category_name', annotate_kwargs)
        self.assertIn('branch_category_ordering', annotate_kwargs)
        self.assertIn('branch_ordering', annotate_kwargs)

    @patch(_PROD_MODEL + '.objects')
    @patch(_BRANCH_OBJ)
    def test_returns_ordered_queryset(self, mock_branch, mock_prod):
        branch = _branch()
        mock_branch.get.return_value = branch
        final_qs = MagicMock()
        mock_prod.filter.return_value.annotate.return_value.order_by.return_value = final_qs

        result = self._call()

        self.assertIs(result, final_qs)

    @patch(_PROD_MODEL + '.objects')
    @patch(_BRANCH_OBJ)
    def test_orders_by_category_then_branch_ordering_then_name(self, mock_branch, mock_prod):
        branch = _branch()
        mock_branch.get.return_value = branch
        mock_prod.filter.return_value.annotate.return_value.order_by.return_value = MagicMock()

        self._call()

        order_args = mock_prod.filter.return_value.annotate.return_value.order_by.call_args.args
        self.assertEqual(order_args, ('branch_category_ordering', 'branch_ordering', 'name'))


# ── buy_product ───────────────────────────────────────────────────────────────

_ATOMIC_ENTER = 'django.db.transaction.Atomic.__enter__'
_ATOMIC_EXIT  = 'django.db.transaction.Atomic.__exit__'


class BuyProductTest(SimpleTestCase):

    _VK_ID      = 11111
    _BRANCH_ID  = 1
    _PRODUCT_ID = 42

    def _call(self, vk_id=None, branch_id=None, product_id=None,
              cb=None, cooldown_active=False, product=None, product_found=True,
              price=50):
        from apps.tenant.catalog.api.services import buy_product

        class _DoesNotExist(Exception):
            pass

        cb = cb or _cb()
        prod = product or _product(pk=self._PRODUCT_ID, price=price)

        with patch(_ATOMIC_ENTER, return_value=None), \
             patch(_ATOMIC_EXIT,  return_value=False), \
             patch(_CB_MODEL) as MockCB, \
             patch(_COOLDOWN) as MockCooldown, \
             patch(_PROD_MODEL) as MockProd, \
             patch(_COIN_MODEL) as MockCoin, \
             patch(_ITEM_MODEL) as MockItem:
            MockCB.objects.select_for_update.return_value \
                .select_related.return_value \
                .get.return_value = cb

            mock_cooldown_instance = MagicMock()
            mock_cooldown_instance.is_active = cooldown_active
            MockCooldown.objects.filter.return_value.first.return_value = (
                mock_cooldown_instance if cooldown_active else None
            )
            MockCooldown.objects.get_or_create.return_value = (MagicMock(), True)

            MockProd.DoesNotExist = _DoesNotExist
            if product_found:
                MockProd.objects.get.return_value = prod
            else:
                MockProd.objects.get.side_effect = _DoesNotExist

            created_item = MagicMock()
            MockItem.objects.create.return_value = created_item
            MockItem.objects.select_related.return_value.get.return_value = created_item

            result = buy_product(
                vk_id=vk_id or self._VK_ID,
                branch_id=branch_id or self._BRANCH_ID,
                product_id=product_id or self._PRODUCT_ID,
            )
        return result, MockCB, MockCooldown, MockProd, MockCoin, MockItem

    def test_raises_client_not_found(self):
        from apps.tenant.catalog.api.services import ClientNotFound, buy_product

        class _DNE(Exception):
            pass

        with patch(_ATOMIC_ENTER, return_value=None), \
             patch(_ATOMIC_EXIT,  return_value=False), \
             patch(_CB_MODEL) as MockCB:
            MockCB.DoesNotExist = _DNE
            MockCB.objects.select_for_update.return_value \
                .select_related.return_value \
                .get.side_effect = _DNE
            with self.assertRaises(ClientNotFound):
                buy_product(vk_id=self._VK_ID, branch_id=self._BRANCH_ID, product_id=self._PRODUCT_ID)

    def test_raises_shop_on_cooldown(self):
        from apps.tenant.catalog.api.services import ShopOnCooldown
        with self.assertRaises(ShopOnCooldown):
            self._call(cooldown_active=True)

    def test_raises_product_not_found(self):
        from apps.tenant.catalog.api.services import ProductNotFound
        with self.assertRaises(ProductNotFound):
            self._call(product_found=False)

    def test_product_validated_via_branch_assignments_not_branch(self):
        """
        CRITICAL: product lookup must use branch_assignments__branch (M2M),
        not the old branch= FK.
        """
        cb = _cb()
        _, _, _, MockProd, _, _ = self._call(cb=cb)

        call_kwargs = MockProd.objects.get.call_args.kwargs
        self.assertIn('branch_assignments__branch', call_kwargs,
                      'Product must be validated via M2M through-table')
        self.assertNotIn('branch', call_kwargs,
                         'Old FK branch no longer exists on Product')
        self.assertEqual(call_kwargs['branch_assignments__branch'], cb.branch)

    def test_product_validated_requires_active_assignment(self):
        """is_active lives on ProductBranch, not Product — must filter accordingly."""
        _, _, _, MockProd, _, _ = self._call()

        call_kwargs = MockProd.objects.get.call_args.kwargs
        self.assertIs(call_kwargs.get('branch_assignments__is_active'), True)

    def test_creates_inventory_item_on_success(self):
        _, _, _, _, _, MockItem = self._call()
        MockItem.objects.create.assert_called_once()
        create_kwargs = MockItem.objects.create.call_args.kwargs
        self.assertEqual(create_kwargs['acquired_from'], 'purchase')

    def test_deducts_coins_when_price_is_positive(self):
        _, _, _, _, MockCoin, _ = self._call(price=100)
        MockCoin.objects.create_transfer.assert_called_once()
        transfer_kwargs = MockCoin.objects.create_transfer.call_args.kwargs
        self.assertEqual(transfer_kwargs['amount'], 100)

    def test_no_coin_deduction_for_free_product(self):
        _, _, _, _, MockCoin, _ = self._call(price=0)
        MockCoin.objects.create_transfer.assert_not_called()

    def test_raises_insufficient_balance(self):
        from apps.tenant.catalog.api.services import InsufficientBalance
        from django.core.exceptions import ValidationError

        cb = _cb()
        prod = _product(price=500)

        class _DoesNotExist(Exception):
            pass

        with patch(_ATOMIC_ENTER, return_value=None), \
             patch(_ATOMIC_EXIT,  return_value=False), \
             patch(_CB_MODEL) as MockCB, \
             patch(_COOLDOWN) as MockCooldown, \
             patch(_PROD_MODEL) as MockProd, \
             patch(_COIN_MODEL) as MockCoin, \
             patch(_ITEM_MODEL):
            MockCB.objects.select_for_update.return_value \
                .select_related.return_value \
                .get.return_value = cb
            MockCooldown.objects.filter.return_value.first.return_value = None
            MockProd.DoesNotExist = _DoesNotExist
            MockProd.objects.get.return_value = prod
            MockCoin.objects.create_transfer.side_effect = ValidationError('insufficient')

            with self.assertRaises(InsufficientBalance):
                from apps.tenant.catalog.api.services import buy_product
                buy_product(vk_id=self._VK_ID, branch_id=self._BRANCH_ID,
                            product_id=self._PRODUCT_ID)

    def test_activates_shop_cooldown_after_purchase(self):
        _, _, MockCooldown, _, _, _ = self._call()
        MockCooldown.objects.get_or_create.assert_called_once()


# ── ProductSerializer ─────────────────────────────────────────────────────────

def _product_mock(branch_category_id=5, branch_category_name='Напитки'):
    """Product instance with M2M branch annotations pre-applied."""
    p = MagicMock()
    p.id = 1
    p.name = 'Капучино'
    p.description = 'Вкусный кофе'
    p.image = None
    p.price = 80
    p.is_super_prize = False
    p.is_birthday_prize = False
    p.branch_category_id = branch_category_id
    p.branch_category_name = branch_category_name
    return p


class ProductSerializerTest(SimpleTestCase):

    def _serialize(self, product):
        from apps.tenant.catalog.api.serializers import ProductSerializer
        return ProductSerializer(product).data

    def test_category_id_reads_from_branch_annotation(self):
        data = self._serialize(_product_mock(branch_category_id=7))
        self.assertEqual(data['category_id'], 7)

    def test_category_name_reads_from_branch_annotation(self):
        data = self._serialize(_product_mock(branch_category_name='Десерты'))
        self.assertEqual(data['category_name'], 'Десерты')

    def test_null_category_id(self):
        data = self._serialize(_product_mock(branch_category_id=None))
        self.assertIsNone(data['category_id'])

    def test_null_category_name(self):
        data = self._serialize(_product_mock(branch_category_name=None))
        self.assertIsNone(data['category_name'])

    def test_product_fields_present(self):
        data = self._serialize(_product_mock())
        for field in ('id', 'name', 'description', 'image_url',
                      'price', 'is_super_prize', 'is_birthday_prize',
                      'category_id', 'category_name'):
            self.assertIn(field, data, f'Missing field: {field}')

    def test_image_url_none_when_no_image(self):
        data = self._serialize(_product_mock())
        self.assertIsNone(data['image_url'])

    def test_flags_serialized(self):
        p = _product_mock()
        p.is_super_prize = True
        p.is_birthday_prize = False
        data = self._serialize(p)
        self.assertTrue(data['is_super_prize'])
        self.assertFalse(data['is_birthday_prize'])


# ── Inventory services — M2M branch_assignments regression ───────────────────

_INV_SVC   = 'apps.tenant.inventory.api.services'
_INV_CB    = f'{_INV_SVC}.ClientBranch'
_INV_ITEM  = f'{_INV_SVC}.InventoryItem'
_INV_PROD  = f'{_INV_SVC}.Product'


def _established_cb(today=None):
    from datetime import date, timedelta
    from apps.tenant.inventory.api.services import BIRTHDAY_WINDOW_DAYS
    today = today or date(2024, 6, 15)
    cb = MagicMock()
    cb.birth_date = today
    cb.birth_date_set_at = today - timedelta(days=31)
    cb.client.vk_id = 22222
    cb.branch.branch_id = 1
    # Окно ДР задаём явно: у автомока это MagicMock, он выигрывает резолв как
    # «переопределение точки» и сравнение с числом дней падает TypeError.
    cb.branch.config.birthday_window_days = BIRTHDAY_WINDOW_DAYS
    return cb


class GetBirthdayProductsBranchAssignmentsTest(SimpleTestCase):
    """
    get_birthday_products must filter via branch_assignments__branch (M2M),
    not the old branch= FK.
    """

    _today = __import__('datetime').date(2024, 6, 15)

    def _call(self, cb):
        from apps.tenant.inventory.api.services import get_birthday_products
        from apps.tenant.inventory.api.services import _get_client_branch
        with patch(f'{_INV_SVC}._get_client_branch', return_value=cb), \
             patch(f'{_INV_SVC}.timezone') as mock_tz, \
             patch(_INV_ITEM) as MockItem, \
             patch(_INV_PROD) as MockProd:
            mock_tz.localdate.return_value = self._today
            MockItem.objects.filter.return_value.exists.return_value = False
            MockProd.objects.filter.return_value \
                .annotate.return_value \
                .order_by.return_value = MagicMock()
            result = get_birthday_products(cb.client.vk_id, cb.branch.branch_id)
        return result, MockProd

    def test_filters_via_branch_assignments_not_branch(self):
        cb = _established_cb(self._today)
        _, MockProd = self._call(cb)

        call_kwargs = MockProd.objects.filter.call_args.kwargs
        self.assertIn('branch_assignments__branch', call_kwargs,
                      'Must filter via M2M through-table branch_assignments')
        self.assertNotIn('branch', call_kwargs,
                         'Old FK branch no longer exists on Product')
        self.assertEqual(call_kwargs['branch_assignments__branch'], cb.branch)

    def test_annotates_branch_ordering(self):
        cb = _established_cb(self._today)
        _, MockProd = self._call(cb)

        annotate_kwargs = MockProd.objects.filter.return_value.annotate.call_args.kwargs
        self.assertIn('branch_ordering', annotate_kwargs)


class ClaimSuperPrizeBranchAssignmentsTest(SimpleTestCase):
    """claim_super_prize must validate product via branch_assignments__branch."""

    def _call(self, product_found=True):
        from apps.tenant.inventory.api.services import claim_super_prize

        class _DoesNotExist(Exception):
            pass

        cb = MagicMock()
        cb.client.vk_id = 33333
        cb.branch.branch_id = 1

        entry = MagicMock()
        entry.status = 'pending'

        with patch(_ATOMIC_ENTER, return_value=None), \
             patch(_ATOMIC_EXIT,  return_value=False), \
             patch(_INV_CB) as MockCB, \
             patch(f'{_INV_SVC}.SuperPrizeEntry') as MockEntry, \
             patch(_INV_PROD) as MockProd, \
             patch(_INV_ITEM):
            MockCB.objects.select_for_update.return_value \
                .select_related.return_value \
                .get.return_value = cb
            MockEntry.objects.select_for_update.return_value \
                .select_related.return_value \
                .filter.return_value \
                .order_by.return_value \
                .first.return_value = entry
            MockProd.DoesNotExist = _DoesNotExist
            if product_found:
                MockProd.objects.get.return_value = MagicMock()
            else:
                MockProd.objects.get.side_effect = _DoesNotExist

            if product_found:
                claim_super_prize(
                    vk_id=cb.client.vk_id,
                    branch_id=cb.branch.branch_id,
                    product_id=1,
                )
        return MockProd, cb

    def test_validates_product_via_branch_assignments(self):
        MockProd, _ = self._call(product_found=True)

        call_kwargs = MockProd.objects.get.call_args.kwargs
        self.assertIn('branch_assignments__branch', call_kwargs,
                      'Must use M2M branch_assignments to validate product ownership')
        self.assertNotIn('branch', call_kwargs,
                         'Old FK field branch no longer exists on Product')
