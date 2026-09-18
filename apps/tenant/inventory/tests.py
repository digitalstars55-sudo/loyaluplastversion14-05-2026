"""
Tests for apps.tenant.inventory.api.services — birthday gift logic.

All ORM calls are mocked at the service module level.
"""
from datetime import date, timedelta
from unittest.mock import MagicMock, patch, PropertyMock

from django.test import TestCase

from apps.tenant.inventory.api.services import BIRTHDAY_WINDOW_DAYS


# ── Patch paths ───────────────────────────────────────────────────────────────

_SVC        = 'apps.tenant.inventory.api.services'
_CB_MODEL   = f'{_SVC}.ClientBranch'
_ITEM_MODEL = f'{_SVC}.InventoryItem'
_PROD_MODEL = f'{_SVC}.Product'
_DAILY_MODEL = f'{_SVC}.DailyCode'
_TZ         = f'{_SVC}.timezone'


# ── Shared factories ──────────────────────────────────────────────────────────

def _cb(birth_date=None, birth_date_set_at=None, vk_id=11111, branch_id=1,
        window_days=BIRTHDAY_WINDOW_DAYS):
    """
    Minimal ClientBranch mock.

    window_days задаётся ЯВНО: окно подарка ДР резолвится «точка → сеть →
    константа» (LU-13), и у автомока `branch.config.birthday_window_days`
    оказывался MagicMock — не None, поэтому побеждал как «переопределение
    точки», а сравнение `abs(delta) <= window_days` падало TypeError.
    Явное число ещё и отвязывает тесты от сетевой настройки в БД.
    """
    cb = MagicMock()
    cb.birth_date = birth_date
    cb.birth_date_set_at = birth_date_set_at
    cb.client.vk_id = vk_id
    cb.branch.branch_id = branch_id
    cb.branch.config.birthday_window_days = window_days
    return cb


def _patch_get_cb(cb):
    """Patch _get_client_branch to return cb."""
    return patch(f'{_SVC}._get_client_branch', return_value=cb)


def _patch_tz(today: date):
    """Patch timezone.localdate() to return a fixed date."""
    m = MagicMock()
    m.localdate.return_value = today
    return patch(_TZ, m)


# ── _branch_birthday_window ──────────────────────────────────────────────────

class BranchBirthdayWindowTest(TestCase):
    """
    Окно подарка ДР: точка → сеть → константа (LU-13).

    Эта развилка и сломала 21 тест: у мока точки окно было MagicMock, то есть
    «не None», и всегда побеждало. Значит, саму развилку надо проверять.
    """

    def _call(self, cb):
        from apps.tenant.inventory.api.services import _branch_birthday_window
        return _branch_birthday_window(cb)

    def test_branch_override_wins(self):
        cb = _cb(window_days=10)
        with patch(f'{_SVC}._network_birthday_window', return_value=3):
            self.assertEqual(self._call(cb), 10)

    def test_network_used_when_branch_silent(self):
        cb = _cb(window_days=None)
        with patch(f'{_SVC}._network_birthday_window', return_value=3):
            self.assertEqual(self._call(cb), 3)

    def test_constant_when_nobody_set_it(self):
        cb = _cb(window_days=None)
        with patch(f'{_SVC}._network_birthday_window', return_value=None):
            self.assertEqual(self._call(cb), BIRTHDAY_WINDOW_DAYS)

    def test_branch_zero_is_a_real_window_not_inheritance(self):
        """0 у точки = «только день в день», а не «наследуй сеть»."""
        cb = _cb(window_days=0)
        with patch(f'{_SVC}._network_birthday_window', return_value=5):
            self.assertEqual(self._call(cb), 0)


# ── _is_in_birthday_window ────────────────────────────────────────────────────

class IsBirthdayWindowTest(TestCase):
    """Year-agnostic ±5-day window around the birthday."""

    def _call(self, birth_date, today):
        from apps.tenant.inventory.api.services import _is_in_birthday_window
        return _is_in_birthday_window(birth_date, today)

    def test_exact_birthday(self):
        self.assertTrue(self._call(date(1990, 6, 15), date(2024, 6, 15)))

    def test_five_days_before(self):
        self.assertTrue(self._call(date(1990, 6, 15), date(2024, 6, 10)))

    def test_five_days_after(self):
        self.assertTrue(self._call(date(1990, 6, 15), date(2024, 6, 20)))

    def test_six_days_before_outside_window(self):
        self.assertFalse(self._call(date(1990, 6, 15), date(2024, 6, 9)))

    def test_six_days_after_outside_window(self):
        self.assertFalse(self._call(date(1990, 6, 15), date(2024, 6, 21)))

    def test_year_wrap_dec28_birthday_jan2_today(self):
        """Dec 28 birthday, Jan 2 today — 5-day difference after year wrap."""
        self.assertTrue(self._call(date(1990, 12, 28), date(2025, 1, 2)))

    def test_year_wrap_jan2_birthday_dec28_today(self):
        """Jan 2 birthday, Dec 28 today — 5-day difference before year wrap."""
        self.assertTrue(self._call(date(1990, 1, 2), date(2024, 12, 28)))

    def test_year_wrap_too_far(self):
        """Dec 28 birthday, Jan 8 today — 11 days after wrap, outside window."""
        self.assertFalse(self._call(date(1990, 12, 28), date(2025, 1, 8)))


# ── _birth_date_is_established ────────────────────────────────────────────────

class BirthDateIsEstablishedTest(TestCase):

    def _call(self, cb, today=date(2024, 6, 15)):
        from apps.tenant.inventory.api.services import _birth_date_is_established
        with _patch_tz(today):
            return _birth_date_is_established(cb)

    def test_null_set_at_grandfathered(self):
        """Legacy profile with NULL birth_date_set_at is always established."""
        self.assertTrue(self._call(_cb(birth_date_set_at=None)))

    def test_30_days_exactly(self):
        set_at = date(2024, 6, 15) - timedelta(days=30)
        self.assertTrue(self._call(_cb(birth_date_set_at=set_at)))

    def test_31_days_established(self):
        set_at = date(2024, 6, 15) - timedelta(days=31)
        self.assertTrue(self._call(_cb(birth_date_set_at=set_at)))

    def test_29_days_not_established(self):
        set_at = date(2024, 6, 15) - timedelta(days=29)
        self.assertFalse(self._call(_cb(birth_date_set_at=set_at)))

    def test_same_day_not_established(self):
        self.assertFalse(self._call(_cb(birth_date_set_at=date(2024, 6, 15))))


# ── get_birthday_status ───────────────────────────────────────────────────────

class GetBirthdayStatusTest(TestCase):
    """get_birthday_status returns correct flags for every combination."""

    _today = date(2024, 6, 15)

    def _call(self, cb, item_exists=False):
        from apps.tenant.inventory.api.services import get_birthday_status
        with _patch_get_cb(cb), \
             _patch_tz(self._today), \
             patch(_ITEM_MODEL) as MockItem:
            MockItem.objects.filter.return_value.exists.return_value = item_exists
            result = get_birthday_status(cb.client.vk_id, cb.branch.branch_id)
        return result, MockItem

    def test_no_birth_date_returns_all_false(self):
        result, _ = self._call(_cb(birth_date=None))
        self.assertEqual(result, {
            'is_birthday_window': False,
            'already_claimed': False,
            'can_claim': False,
        })

    def test_in_window_not_claimed_established_can_claim(self):
        cb = _cb(
            birth_date=self._today,
            birth_date_set_at=self._today - timedelta(days=31),
        )
        result, _ = self._call(cb, item_exists=False)
        self.assertTrue(result['is_birthday_window'])
        self.assertFalse(result['already_claimed'])
        self.assertTrue(result['can_claim'])

    def test_already_claimed_cannot_claim(self):
        cb = _cb(
            birth_date=self._today,
            birth_date_set_at=self._today - timedelta(days=31),
        )
        result, _ = self._call(cb, item_exists=True)
        self.assertTrue(result['already_claimed'])
        self.assertFalse(result['can_claim'])

    def test_birth_date_too_recent_cannot_claim(self):
        cb = _cb(
            birth_date=self._today,
            birth_date_set_at=self._today - timedelta(days=5),
        )
        result, _ = self._call(cb, item_exists=False)
        self.assertTrue(result['is_birthday_window'])
        self.assertFalse(result['can_claim'])

    def test_outside_window_cannot_claim(self):
        cb = _cb(
            birth_date=self._today - timedelta(days=10),
            birth_date_set_at=self._today - timedelta(days=60),
        )
        result, _ = self._call(cb, item_exists=False)
        self.assertFalse(result['is_birthday_window'])
        self.assertFalse(result['can_claim'])

    def test_already_claimed_check_uses_client_not_client_branch(self):
        """
        Cross-branch fix: filter must use client_branch__client, not client_branch.
        Verifies that the ORM filter key is client_branch__client=cb.client.
        """
        cb = _cb(
            birth_date=self._today,
            birth_date_set_at=self._today - timedelta(days=31),
        )
        _, MockItem = self._call(cb, item_exists=True)

        call_kwargs = MockItem.objects.filter.call_args.kwargs
        self.assertIn('client_branch__client', call_kwargs)
        self.assertNotIn('client_branch', call_kwargs)
        self.assertEqual(call_kwargs['client_branch__client'], cb.client)


# ── get_birthday_products ─────────────────────────────────────────────────────

class GetBirthdayProductsTest(TestCase):
    """get_birthday_products enforces eligibility and returns correct queryset."""

    _today = date(2024, 6, 15)

    def _call(self, cb, item_exists=False):
        from apps.tenant.inventory.api.services import get_birthday_products
        with _patch_get_cb(cb), \
             _patch_tz(self._today), \
             patch(_ITEM_MODEL) as MockItem, \
             patch(_PROD_MODEL) as MockProd:
            MockItem.objects.filter.return_value.exists.return_value = item_exists
            result = get_birthday_products(cb.client.vk_id, cb.branch.branch_id)
        return result, MockItem, MockProd

    def _established_cb(self):
        return _cb(
            birth_date=self._today,
            birth_date_set_at=self._today - timedelta(days=31),
        )

    def test_raises_not_birthday_window_when_no_birth_date(self):
        from apps.tenant.inventory.api.services import NotBirthdayWindow
        with self.assertRaises(NotBirthdayWindow):
            self._call(_cb(birth_date=None))

    def test_raises_not_birthday_window_outside_range(self):
        from apps.tenant.inventory.api.services import NotBirthdayWindow
        cb = _cb(
            birth_date=self._today - timedelta(days=20),
            birth_date_set_at=self._today - timedelta(days=60),
        )
        with self.assertRaises(NotBirthdayWindow):
            self._call(cb)

    def test_raises_birthday_too_recent(self):
        from apps.tenant.inventory.api.services import BirthdayTooRecent
        cb = _cb(
            birth_date=self._today,
            birth_date_set_at=self._today - timedelta(days=10),
        )
        with self.assertRaises(BirthdayTooRecent):
            self._call(cb)

    def test_raises_already_claimed(self):
        from apps.tenant.inventory.api.services import AlreadyClaimed
        with self.assertRaises(AlreadyClaimed):
            self._call(self._established_cb(), item_exists=True)

    def test_returns_product_queryset_on_success(self):
        _, _, MockProd = self._call(self._established_cb(), item_exists=False)
        MockProd.objects.filter.assert_called_once()

    def test_already_claimed_check_uses_client_not_client_branch(self):
        """Cross-branch fix verification."""
        from apps.tenant.inventory.api.services import AlreadyClaimed
        cb = self._established_cb()
        with self.assertRaises(AlreadyClaimed):
            _, MockItem, _ = self._call(cb, item_exists=True)

        # Verify the filter used client_branch__client
        call_kwargs = None
        from apps.tenant.inventory.api.services import get_birthday_products
        with _patch_get_cb(cb), \
             _patch_tz(self._today), \
             patch(_ITEM_MODEL) as MockItem, \
             patch(_PROD_MODEL):
            MockItem.objects.filter.return_value.exists.return_value = True
            try:
                get_birthday_products(cb.client.vk_id, cb.branch.branch_id)
            except Exception:
                pass
            call_kwargs = MockItem.objects.filter.call_args.kwargs

        self.assertIn('client_branch__client', call_kwargs)
        self.assertNotIn('client_branch', call_kwargs)


# ── claim_birthday_prize ──────────────────────────────────────────────────────

class ClaimBirthdayPrizeTest(TestCase):
    """claim_birthday_prize creates InventoryItem or raises appropriate errors."""

    _today = date(2024, 6, 15)

    def _established_cb(self):
        return _cb(
            birth_date=self._today,
            birth_date_set_at=self._today - timedelta(days=31),
        )

    def _call(self, cb, product_id=1, item_exists=False, product_found=True):
        from apps.tenant.inventory.api.services import claim_birthday_prize

        class _DoesNotExist(Exception):
            pass

        with _patch_tz(self._today), \
             patch(_CB_MODEL) as MockCB, \
             patch(_ITEM_MODEL) as MockItem, \
             patch(_PROD_MODEL) as MockProd:
            MockCB.objects.select_for_update.return_value \
                .select_related.return_value \
                .get.return_value = cb
            MockItem.objects.filter.return_value.exists.return_value = item_exists
            MockProd.DoesNotExist = _DoesNotExist
            if product_found:
                MockProd.objects.get.return_value = MagicMock()
            else:
                MockProd.objects.get.side_effect = _DoesNotExist
            result = claim_birthday_prize(cb.client.vk_id, cb.branch.branch_id, product_id)
        return result, MockItem

    def test_creates_inventory_item_on_success(self):
        cb = self._established_cb()
        result, MockItem = self._call(cb)
        MockItem.objects.create.assert_called_once()
        create_kwargs = MockItem.objects.create.call_args.kwargs
        self.assertEqual(create_kwargs['client_branch'], cb)
        self.assertEqual(create_kwargs['acquired_from'], 'birthday')

    def test_raises_already_claimed_when_item_exists(self):
        from apps.tenant.inventory.api.services import AlreadyClaimed
        with self.assertRaises(AlreadyClaimed):
            self._call(self._established_cb(), item_exists=True)

    def test_raises_not_birthday_window_when_outside(self):
        from apps.tenant.inventory.api.services import NotBirthdayWindow
        cb = _cb(birth_date=self._today - timedelta(days=20),
                 birth_date_set_at=self._today - timedelta(days=60))
        with self.assertRaises(NotBirthdayWindow):
            self._call(cb)

    def test_raises_birthday_too_recent(self):
        from apps.tenant.inventory.api.services import BirthdayTooRecent
        cb = _cb(birth_date=self._today,
                 birth_date_set_at=self._today - timedelta(days=10))
        with self.assertRaises(BirthdayTooRecent):
            self._call(cb)

    def test_raises_product_not_found(self):
        from apps.tenant.inventory.api.services import ProductNotFound
        with self.assertRaises(ProductNotFound):
            self._call(self._established_cb(), product_found=False)

    def test_cross_branch_dedup_filter_key(self):
        """
        CRITICAL: already-claimed check must filter by client_branch__client
        (all branches of the guest), not client_branch (current branch only).

        Without the fix, a guest who claimed at Branch A could claim again
        at Branch B because their ClientBranch objects are different.
        """
        from apps.tenant.inventory.api.services import AlreadyClaimed
        cb = self._established_cb()

        with _patch_tz(self._today), \
             patch(_CB_MODEL) as MockCB, \
             patch(_ITEM_MODEL) as MockItem, \
             patch(_PROD_MODEL):
            MockCB.objects.select_for_update.return_value \
                .select_related.return_value \
                .get.return_value = cb
            MockItem.objects.filter.return_value.exists.return_value = True

            from apps.tenant.inventory.api.services import claim_birthday_prize
            with self.assertRaises(AlreadyClaimed):
                claim_birthday_prize(cb.client.vk_id, cb.branch.branch_id, 1)

            call_kwargs = MockItem.objects.filter.call_args.kwargs

        self.assertIn('client_branch__client', call_kwargs,
                      'Filter must use client_branch__client for cross-branch deduplication')
        self.assertNotIn('client_branch', call_kwargs,
                         'client_branch alone only checks the current branch — allows double-claiming')
        self.assertEqual(call_kwargs['client_branch__client'], cb.client)

    def test_cross_branch_scenario(self):
        """
        Guest claimed at Branch A (cb_a).
        When checking from Branch B (cb_b), the filter uses cb_b.client
        which equals cb_a.client (same guest) → AlreadyClaimed.

        Simulates the real scenario: same guest, two different ClientBranch objects.
        """
        from apps.tenant.inventory.api.services import AlreadyClaimed, claim_birthday_prize

        shared_client = MagicMock()  # Same underlying Client object

        cb_branch_b = MagicMock()
        cb_branch_b.birth_date = self._today
        cb_branch_b.birth_date_set_at = self._today - timedelta(days=31)
        cb_branch_b.client = shared_client  # same guest
        cb_branch_b.branch.branch_id = 2
        cb_branch_b.branch.config.birthday_window_days = BIRTHDAY_WINDOW_DAYS

        with _patch_tz(self._today), \
             patch(_CB_MODEL) as MockCB, \
             patch(_ITEM_MODEL) as MockItem, \
             patch(_PROD_MODEL):
            MockCB.objects.select_for_update.return_value \
                .select_related.return_value \
                .get.return_value = cb_branch_b
            # Simulate: item found for this client (claimed at Branch A)
            MockItem.objects.filter.return_value.exists.return_value = True

            with self.assertRaises(AlreadyClaimed):
                claim_birthday_prize(shared_client.vk_id, 2, 1)

            # Verify the filter uses the shared client, not the branch-specific cb
            call_kwargs = MockItem.objects.filter.call_args.kwargs
            self.assertEqual(call_kwargs.get('client_branch__client'), shared_client)


# ── activate_item (birthday-specific behaviour) ───────────────────────────────

class ActivateBirthdayItemTest(TestCase):
    """Birthday InventoryItems require a daily code; no cooldown is set."""

    _today = date(2024, 6, 15)

    def _make_item(self, acquired_from='birthday', status='pending'):
        from apps.tenant.inventory.models import AcquisitionSource, ItemStatus
        item = MagicMock()
        item.acquired_from = AcquisitionSource.BIRTHDAY
        item.status = ItemStatus.PENDING
        # Срок забора подарка (14 дней) появился ПОЗЖЕ этих тестов: у автомока
        # is_claim_expired правдив, и активация падала GiftClaimExpired.
        item.is_claim_expired = False
        return item

    def test_birthday_item_requires_code(self):
        from apps.tenant.inventory.api.services import activate_item, InvalidCode

        cb = _cb()
        item = self._make_item()

        with patch(_CB_MODEL) as MockCB, \
             patch(_ITEM_MODEL) as MockItem, \
             patch(_DAILY_MODEL) as MockDaily:
            MockCB.objects.select_for_update.return_value \
                .select_related.return_value \
                .get.return_value = cb
            MockItem.objects.select_for_update.return_value \
                .select_related.return_value \
                .get.return_value = item
            # No valid daily code
            MockDaily.objects.filter.return_value.first.return_value = None

            with self.assertRaises(InvalidCode):
                activate_item(cb.client.vk_id, cb.branch.branch_id, item_id=1, code='WRONG')

    def test_birthday_item_no_cooldown_set(self):
        from apps.tenant.inventory.api.services import activate_item

        cb = _cb()
        item = self._make_item()

        daily = MagicMock()
        daily.code = 'ABCDE'

        with patch(_CB_MODEL) as MockCB, \
             patch(_ITEM_MODEL) as MockItem, \
             patch(_DAILY_MODEL) as MockDaily, \
             patch(f'{_SVC}._activate_inventory_cooldown') as mock_cooldown, \
             patch(f'{_SVC}._get_inventory_cooldown'):
            MockCB.objects.select_for_update.return_value \
                .select_related.return_value \
                .get.return_value = cb
            MockItem.objects.select_for_update.return_value \
                .select_related.return_value \
                .get.return_value = item
            MockItem.objects.select_related.return_value.get.return_value = item
            MockDaily.objects.filter.return_value.first.return_value = daily

            activate_item(cb.client.vk_id, cb.branch.branch_id, item_id=1, code='ABCDE')

        mock_cooldown.assert_not_called()
