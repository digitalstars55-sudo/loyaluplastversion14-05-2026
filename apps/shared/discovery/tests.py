"""
Tests for apps.shared.discovery — блокировка гостя в сетевом входе из каталога VK.

Стиль как в apps/tenant/branch/api/tests.py: mock ORM-менеджеров, без БД.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase

from . import services as svc


class CreateWelcomeGiftBlockedTest(TestCase):
    """Заблокированный на платформе гость не получает профиль и приз через каталог."""

    @patch('apps.shared.guest.models.Client.objects')
    @patch('apps.tenant.branch.models.Branch.objects')
    @patch('apps.tenant.catalog.models.Product.objects')
    def test_blocked_guest_raises_guest_blocked(self, mock_product, mock_branch, mock_client):
        mock_product.filter.return_value.order_by.return_value.first.return_value = MagicMock()
        mock_branch.filter.return_value.order_by.return_value.first.return_value = MagicMock()
        mock_client.get_or_create.return_value = (MagicMock(is_active=False), False)

        with patch('apps.tenant.branch.models.ClientBranch.objects') as mock_cb:
            with self.assertRaises(svc.GuestBlocked):
                svc._create_welcome_gift(111)
            # Профиль в тенанте НЕ создаётся — дыра из инцидента 23.08 закрыта.
            mock_cb.get_or_create.assert_not_called()

    @patch('apps.shared.guest.models.Client.objects')
    @patch('apps.tenant.branch.models.Branch.objects')
    @patch('apps.tenant.catalog.models.Product.objects')
    def test_new_guest_is_not_blocked(self, mock_product, mock_branch, mock_client):
        """Свежесозданный Client (is_active по умолчанию True у новых) проходит дальше."""
        mock_product.filter.return_value.order_by.return_value.first.return_value = MagicMock()
        mock_branch.filter.return_value.order_by.return_value.first.return_value = MagicMock()
        # created=True — проверка блокировки не применяется даже при странном моке.
        mock_client.get_or_create.return_value = (MagicMock(is_active=False), True)

        with patch('apps.tenant.branch.models.ClientBranch.objects') as mock_cb, \
             patch('apps.tenant.inventory.api.story_services._resolve_story_settings') as mock_settings, \
             patch('apps.tenant.inventory.models.StoryGiftEntry.objects') as mock_entry:
            mock_cb.get_or_create.return_value = (MagicMock(), True)
            mock_settings.return_value = {'min_order_amount': 0, 'activation_minutes': 30}
            mock_entry.get_or_create.return_value = (MagicMock(played_at=None, selected_at=None), True)

            svc._create_welcome_gift(200002444631)  # bigint VK ID из инцидента 17.08

            mock_cb.get_or_create.assert_called_once()
