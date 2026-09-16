"""Карточка/правка точки (api/branch_edit.py): права, разбор тела — без базы."""
from types import SimpleNamespace

from django.test import SimpleTestCase
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIRequestFactory, force_authenticate

from .api.branch_edit import BRANCH_FIELDS, CONFIG_FIELDS, BranchDetailAPIView, can_edit_branches, split_payload


def _patch(body, user):
    request = APIRequestFactory().patch('/api/v1/mobile/branches/5/', body, format='json')
    force_authenticate(request, user=user)
    return BranchDetailAPIView.as_view()(request, pk=5)


ADMIN = SimpleNamespace(is_authenticated=True, is_superuser=False, role='network_admin', username='adm', pk=1)
CLIENT = SimpleNamespace(is_authenticated=True, is_superuser=False, role='client', username='cl', pk=2)


class SplitPayloadTest(SimpleTestCase):

    def test_split(self):
        b, c, ro, unk = split_payload({'name': 'X', 'phone': '+7', 'branch_id': 9, 'foo': 1})
        self.assertEqual(b, {'name': 'X'})
        self.assertEqual(c, {'phone': '+7'})
        self.assertEqual(ro, ['branch_id'])
        self.assertEqual(unk, ['foo'])

    def test_not_dict(self):
        self.assertEqual(split_payload([1])[3], ['<body>'])

    def test_field_sets_disjoint(self):
        self.assertFalse(set(BRANCH_FIELDS) & set(CONFIG_FIELDS))


class PermissionsTest(SimpleTestCase):

    def test_declares_auth(self):
        self.assertIn(IsAuthenticated, BranchDetailAPIView.permission_classes)

    def test_can_edit(self):
        self.assertTrue(can_edit_branches(ADMIN))
        self.assertTrue(can_edit_branches(SimpleNamespace(is_superuser=True, role='client')))
        self.assertFalse(can_edit_branches(CLIENT))

    def test_client_role_403_before_db(self):
        r = _patch({'phone': '+7'}, CLIENT)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data['code'], 'forbidden')

    def test_read_only_and_unknown_400_before_db(self):
        r = _patch({'branch_id': 1, 'foo': 2, 'name': 'x'}, ADMIN)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data['read_only'], ['branch_id'])
        self.assertEqual(r.data['unknown'], ['foo'])

    def test_empty_body_400(self):
        r = _patch({}, ADMIN)
        self.assertEqual(r.status_code, 400)
