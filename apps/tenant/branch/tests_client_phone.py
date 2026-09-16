"""
POST/DELETE /api/v1/client/phone/ — телефон гостя с согласия через ВК (№78).

БД нужна только для shared `guest.Client` (public-схема); тенантные модели не трогаем.
Вью вызывается напрямую через APIRequestFactory, доказанный id гостя подставляется
в `request.vk_user_id`, как это делает VKLaunchParamsMiddleware.
"""

import base64
import hashlib

from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory

from apps.shared.guest.models import Client
from apps.tenant.branch.api.client_phone import ClientPhoneView

APP_ID = 53418653
SECRET = 'test-protected-key'
VK_ID = 123456789
PHONE = '79991234567'
E164 = '+79991234567'


def _sign(app_id=APP_ID, secret=SECRET, user_id=VK_ID, phone=PHONE) -> str:
    raw = f'{app_id}{secret}{user_id}phone_number{phone}'.encode('utf-8')
    return base64.b64encode(hashlib.sha256(raw).digest()).decode('ascii')


@override_settings(VK_MINI_APP_ID=APP_ID, VK_SECRET=SECRET,
                   GUEST_PHONE_ENABLED=True, GUEST_PHONE_SIGN_ENFORCE='off')
class ClientPhoneViewTest(TestCase):

    def setUp(self):
        self.factory = APIRequestFactory()
        self.guest = Client.objects.create(vk_id=VK_ID, first_name='Тест')
        self.view = ClientPhoneView.as_view()

    def _post(self, body, proven=None):
        request = self.factory.post('/api/v1/client/phone/', body, format='json')
        request.vk_user_id = proven
        return self.view(request)

    def _delete(self, body, proven=None):
        request = self.factory.delete('/api/v1/client/phone/', body, format='json')
        request.vk_user_id = proven
        return self.view(request)

    def _guest(self):
        return Client.objects.get(pk=self.guest.pk)

    # ── флаг ─────────────────────────────────────────────────────────
    @override_settings(GUEST_PHONE_ENABLED=False)
    def test_feature_off_is_404(self):
        r = self._post({'vk_id': VK_ID, 'phone_number': PHONE, 'sign': _sign()})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.data['code'], 'feature_disabled')
        self.assertEqual(self._guest().phone, '')

    # ── подпись телефона сошлась ─────────────────────────────────────
    def test_sign_ok_saves_verified_without_header(self):
        r = self._post({'vk_id': VK_ID, 'phone_number': PHONE, 'sign': _sign()})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['phone'], E164)
        self.assertTrue(r.data['verified'])
        self.assertFalse(r.data['proven'])
        g = self._guest()
        self.assertEqual((g.phone, g.phone_source), (E164, 'vk'))
        self.assertIsNotNone(g.phone_consent_at)

    def test_sign_ok_with_proven_header(self):
        r = self._post({'vk_id': VK_ID, 'phone_number': PHONE, 'sign': _sign()}, proven=VK_ID)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data['proven'])
        self.assertEqual(self._guest().phone_source, 'vk')

    def test_raw_phone_is_normalized_but_signed_as_is(self):
        raw = '+7 (999) 123-45-67'
        r = self._post({'vk_id': VK_ID, 'phone_number': raw, 'sign': _sign(phone=raw)})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self._guest().phone, E164)

    # ── доказанный гость и чужой vk_id ───────────────────────────────
    def test_proven_mismatch_is_403(self):
        r = self._post({'vk_id': VK_ID, 'phone_number': PHONE, 'sign': _sign()}, proven=999)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data['code'], 'vk_id_mismatch')
        self.assertEqual(self._guest().phone, '')

    # ── подпись телефона не сошлась ──────────────────────────────────
    def test_bad_sign_unproven_is_403(self):
        """Ни подписи телефона, ни заголовка запуска — чужой номер вписать нельзя."""
        r = self._post({'vk_id': VK_ID, 'phone_number': PHONE, 'sign': 'zzz'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data['code'], 'phone_sign_invalid')
        self.assertEqual(self._guest().phone, '')

    def test_bad_sign_proven_observation_saves_unverified(self):
        r = self._post({'vk_id': VK_ID, 'phone_number': PHONE, 'sign': 'zzz'}, proven=VK_ID)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertFalse(r.data['verified'])
        g = self._guest()
        self.assertEqual((g.phone, g.phone_source), (E164, 'vk_unverified'))

    @override_settings(GUEST_PHONE_SIGN_ENFORCE='on')
    def test_bad_sign_proven_enforce_is_403(self):
        r = self._post({'vk_id': VK_ID, 'phone_number': PHONE, 'sign': 'zzz'}, proven=VK_ID)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data['code'], 'phone_sign_invalid')
        self.assertEqual(self._guest().phone, '')

    @override_settings(GUEST_PHONE_SIGN_ENFORCE='on')
    def test_enforce_requires_header_even_when_sign_ok(self):
        r = self._post({'vk_id': VK_ID, 'phone_number': PHONE, 'sign': _sign()})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data['code'], 'vk_sign_required')

    @override_settings(GUEST_PHONE_SIGN_ENFORCE='on')
    def test_enforce_ok_when_both_proven(self):
        r = self._post({'vk_id': VK_ID, 'phone_number': PHONE, 'sign': _sign()}, proven=VK_ID)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self._guest().phone_source, 'vk')

    # ── валидация ────────────────────────────────────────────────────
    def test_phone_invalid_is_400(self):
        r = self._post({'vk_id': VK_ID, 'phone_number': 'abc', 'sign': _sign(phone='abc')})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data['code'], 'phone_invalid')

    def test_missing_fields_is_400(self):
        r = self._post({'vk_id': VK_ID})
        self.assertEqual(r.status_code, 400)

    def test_unknown_guest_is_404(self):
        r = self._post({'vk_id': 555, 'phone_number': PHONE, 'sign': _sign(user_id=555)})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.data['code'], 'client_not_found')

    # ── отзыв согласия ───────────────────────────────────────────────
    def test_delete_revokes_for_proven_guest(self):
        self._post({'vk_id': VK_ID, 'phone_number': PHONE, 'sign': _sign()})
        self.assertEqual(self._guest().phone, E164)
        r = self._delete({'vk_id': VK_ID}, proven=VK_ID)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data['revoked'])
        g = self._guest()
        self.assertEqual((g.phone, g.phone_source, g.phone_consent_at), ('', '', None))

    def test_delete_unproven_is_403(self):
        self._post({'vk_id': VK_ID, 'phone_number': PHONE, 'sign': _sign()})
        r = self._delete({'vk_id': VK_ID})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.data['code'], 'vk_sign_required')
        self.assertEqual(self._guest().phone, E164)

    @override_settings(GUEST_PHONE_ENABLED=False)
    def test_delete_feature_off_is_404(self):
        r = self._delete({'vk_id': VK_ID}, proven=VK_ID)
        self.assertEqual(r.status_code, 404)
