import importlib
from types import SimpleNamespace

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from apps.shared.users.feature_permissions import HasFeatureAccess


def employee(features, *, superuser=False):
    return SimpleNamespace(is_authenticated=True, is_superuser=superuser,
                           feature_access=features, role='network_admin')


class FeatureAccessBehaviour(SimpleTestCase):
    def check(self, features, method='GET', *, superuser=False):
        gate = HasFeatureAccess.factory('analytics', write_feature='broadcasts')()
        req = SimpleNamespace(user=employee(features, superuser=superuser), method=method)
        return gate.has_permission(req, None)

    def test_unrestricted_existing_users_retain_access(self):
        self.assertTrue(self.check([], 'POST'))
        self.assertTrue(self.check(['reviews'], 'POST', superuser=True))

    def test_explicit_features_gate_both_read_and_write(self):
        self.assertFalse(self.check(['reviews']))
        self.assertTrue(self.check(['analytics']))
        self.assertFalse(self.check(['analytics'], 'POST'))
        self.assertTrue(self.check(['analytics', 'broadcasts'], 'POST'))

    def test_direct_employee_endpoints_deny_disabled_features_before_querying(self):
        factory = APIRequestFactory()
        modules = [
            'apps.tenant.analytics.api.views', 'apps.tenant.analytics.api.summary',
            'apps.tenant.mobile.api.views', 'apps.tenant.marketer.api.views',
            'apps.tenant.senler.api.auto_broadcasts', 'apps.tenant.analytics.api.report_comments',
        ]
        seen = set()
        checked = 0
        for module in modules:
            for name, cls in vars(importlib.import_module(module)).items():
                if not isinstance(cls, type) or not issubclass(cls, APIView) or cls in seen:
                    continue
                seen.add(cls)
                gates = [p for p in cls.permission_classes if isinstance(p, type) and issubclass(p, HasFeatureAccess)]
                if not gates or gates[0].required_feature == 'home':
                    continue
                with self.subTest(view=name):
                    request = factory.get('/api/v1/feature-check/')
                    force_authenticate(request, user=employee(['home']))
                    response = cls.as_view()(request)
                    self.assertEqual(response.status_code, 403)
                    checked += 1
        self.assertGreaterEqual(checked, 75)

    def test_global_search_does_not_query_denied_features_or_tenant(self):
        from unittest.mock import patch
        from apps.tenant.mobile.api.views import GlobalSearchAPIView
        for features, allowed in [(['home'], None), ([], set())]:
            with self.subTest(features=features, allowed=allowed), patch(
                'apps.shared.users.access.user_allowed_branches', return_value=allowed
            ):
                req = APIRequestFactory().get('/api/v1/search/', {'q': 'private'})
                force_authenticate(req, user=employee(features))
                response = GlobalSearchAPIView.as_view()(req)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data['total'], 0)
