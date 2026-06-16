"""
Тесты loyalty-API.

Запускать на окружении с Python 3.12 + Postgres (как прод). django-tenants:
использовать TenantTestCase (создаёт тестовую схему). Скелет:

    from django_tenants.test.cases import TenantTestCase
    from apps.shared.guest.models import Client
    from apps.tenant.branch.models import Branch, ClientBranch
    from apps.tenant.loyalty import services

    class LoyaltyServiceTests(TenantTestCase):
        def setUp(self):
            self.branch = Branch.objects.create(name='LevOne Набережная')
            self.client_obj = Client.objects.create(vk_id=777, first_name='Тест')

        def test_accrue_then_balance(self):
            res, _ = services.accrue(777, self.branch.pk, 'O-1', 1000,
                                     'accrue:O-1')           # 10% → 100
            self.assertEqual(res['points_earned'], 100)
            self.assertEqual(services.network_balance(self.client_obj), 100)

        def test_accrue_idempotent(self):
            services.accrue(777, self.branch.pk, 'O-2', 1000, 'accrue:O-2')
            _, replayed = services.accrue(777, self.branch.pk, 'O-2', 1000, 'accrue:O-2')
            self.assertTrue(replayed)
            self.assertEqual(services.network_balance(self.client_obj), 100)

        def test_redeem_insufficient(self):
            with self.assertRaises(services.InsufficientBalance):
                services.redeem(777, self.branch.pk, 'O-3', 50, 'redeem:O-3')

        def test_refund_reverses(self):
            services.accrue(777, self.branch.pk, 'O-4', 1000, 'accrue:O-4')  # +100
            services.redeem(777, self.branch.pk, 'O-4', 40, 'redeem:O-4')    # -40 → 60
            res, _ = services.refund(777, self.branch.pk, 'O-4', 'refund:O-4')
            self.assertEqual(res['reversed_earned'], 100)
            self.assertEqual(res['restored_spent'], 40)
            self.assertEqual(services.network_balance(self.client_obj), 0)
            self.assertEqual(services.spend_total(777, 90)['spend_total'], 0)
"""
