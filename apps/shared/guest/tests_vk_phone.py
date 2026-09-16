"""Подпись телефона из `VKWebAppGetPhoneNumber` и нормализация номера (№78). Без БД."""

import base64
import hashlib
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from apps.shared.guest.vk_phone import (
    check_phone_sign,
    guest_phone_enabled,
    normalize_phone,
    phone_sign_candidates,
    phone_sign_enforce,
    tenant_guest_phone_enabled,
)

APP_ID = 53418653
SECRET = 'test-protected-key'
USER_ID = 123456789
PHONE = '79991234567'


def _vk_sign(app_id=APP_ID, secret=SECRET, user_id=USER_ID, phone=PHONE) -> str:
    """Эталон по документации ВК: base64(sha256(app_id + secret + user_id + 'phone_number' + phone))."""
    raw = f'{app_id}{secret}{user_id}phone_number{phone}'.encode('utf-8')
    return base64.b64encode(hashlib.sha256(raw).digest()).decode('ascii')


class NormalizePhoneTest(SimpleTestCase):

    def test_russian_variants_to_e164(self):
        for raw in ('+7 (999) 123-45-67', '8 999 123 45 67', '79991234567', '9991234567', '+79991234567'):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone(raw), '+79991234567')

    def test_foreign_kept_with_plus(self):
        self.assertEqual(normalize_phone('+375 29 123 45 67'), '+375291234567')

    def test_garbage_is_none(self):
        for raw in (None, '', 'abc', '123', '1' * 16):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_phone(raw))


@override_settings(VK_MINI_APP_ID=APP_ID, VK_SECRET=SECRET)
class CheckPhoneSignTest(SimpleTestCase):

    def test_ok_base64_of_raw_digest(self):
        self.assertEqual(check_phone_sign(USER_ID, PHONE, _vk_sign()), 'ok:b64')

    def test_ok_other_encodings_named(self):
        cands = phone_sign_candidates(APP_ID, SECRET, USER_ID, PHONE)
        self.assertEqual(check_phone_sign(USER_ID, PHONE, cands['hex']), 'ok:hex')
        # base64 и base64url совпадают, если в дайджесте нет '+'/'/', — тогда
        # первым сходится 'b64_nopad'; важно лишь, что вариант без '=' принят.
        self.assertIn(check_phone_sign(USER_ID, PHONE, cands['b64url_nopad']),
                      ('ok:b64_nopad', 'ok:b64url_nopad'))

    def test_user_id_from_body_cannot_forge(self):
        """Подпись выдана для другого user_id — с доказанным id гостя не сходится."""
        self.assertEqual(check_phone_sign(USER_ID, PHONE, _vk_sign(user_id=999)), 'mismatch')

    def test_phone_tampered(self):
        self.assertEqual(check_phone_sign(USER_ID, '79990000000', _vk_sign()), 'mismatch')

    def test_wrong_secret(self):
        self.assertEqual(check_phone_sign(USER_ID, PHONE, _vk_sign(secret='other')), 'mismatch')

    def test_empty_inputs_mismatch(self):
        self.assertEqual(check_phone_sign(USER_ID, PHONE, ''), 'mismatch')
        self.assertEqual(check_phone_sign(None, PHONE, _vk_sign()), 'mismatch')
        self.assertEqual(check_phone_sign(USER_ID, '', _vk_sign()), 'mismatch')

    @override_settings(VK_SECRET='')
    def test_no_secret_is_off(self):
        self.assertEqual(check_phone_sign(USER_ID, PHONE, _vk_sign()), 'off')

    def test_signature_uses_raw_phone_not_normalized(self):
        """ВК подписывает phone_number как вернул; нормализованный '+7…' подпись не даст."""
        self.assertEqual(check_phone_sign(USER_ID, '+79991234567', _vk_sign(phone=PHONE)), 'mismatch')


class FlagsTest(SimpleTestCase):

    def test_defaults_off(self):
        with override_settings(GUEST_PHONE_ENABLED=False, GUEST_PHONE_SIGN_ENFORCE='off'):
            self.assertFalse(guest_phone_enabled())
            self.assertFalse(phone_sign_enforce())

    def test_on_needs_both_platform_and_tenant(self):
        with override_settings(GUEST_PHONE_ENABLED=True, GUEST_PHONE_SIGN_ENFORCE='ON'):
            self.assertTrue(phone_sign_enforce())
            with patch('apps.shared.guest.vk_phone.tenant_guest_phone_enabled', return_value=True):
                self.assertTrue(guest_phone_enabled())
            with patch('apps.shared.guest.vk_phone.tenant_guest_phone_enabled', return_value=False):
                self.assertFalse(guest_phone_enabled())

    def test_platform_off_wins_over_tenant(self):
        with override_settings(GUEST_PHONE_ENABLED=False), \
                patch('apps.shared.guest.vk_phone.tenant_guest_phone_enabled', return_value=True):
            self.assertFalse(guest_phone_enabled())

    def test_tenant_flag_outside_tenant_is_false(self):
        """В тестах connection.tenant не задан / без конфига → False, а не исключение."""
        self.assertFalse(tenant_guest_phone_enabled())
