from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, models
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from apps.shared.secret_fields import EncryptedSecretField, PREFIX

TEST_KEY = Fernet.generate_key().decode()


@override_settings(LOYALUP_SECRET_KEYS=[TEST_KEY])
class SecretFieldBehaviour(SimpleTestCase):
    def setUp(self):
        self.field = EncryptedSecretField(max_length=512)

    def test_ciphertext_does_not_contain_secret_and_roundtrips(self):
        raw = 'integration-token-very-private'
        stored = self.field.get_prep_value(raw)
        self.assertTrue(stored.startswith(PREFIX))
        self.assertNotIn(raw, stored)
        self.assertEqual(self.field.from_db_value(stored, None, None), raw)
        self.assertNotEqual(stored, self.field.get_prep_value(raw))

    def test_plaintext_legacy_values_are_readable_until_backfill(self):
        self.assertEqual(self.field.from_db_value('old-token', None, None), 'old-token')
        self.assertEqual(self.field.get_prep_value(''), '')
        self.assertIsNone(self.field.get_prep_value(None))

    def test_wrong_key_fails_instead_of_returning_unusable_token(self):
        stored = self.field.get_prep_value('secret')
        with override_settings(LOYALUP_SECRET_KEYS=[Fernet.generate_key().decode()]):
            with self.assertRaises(ImproperlyConfigured):
                self.field.from_db_value(stored, None, None)

    @override_settings(LOYALUP_SECRET_KEYS=[])
    def test_missing_key_cannot_write_plaintext(self):
        with self.assertRaises(ImproperlyConfigured):
            self.field.get_prep_value('secret')

    def test_already_encrypted_value_is_not_double_encrypted(self):
        stored = self.field.get_prep_value('secret')
        self.assertEqual(self.field.get_prep_value(stored), stored)


class SecretRoundTripModel(models.Model):
    secret = EncryptedSecretField(max_length=512)

    class Meta:
        app_label = 'config'
        db_table = 'test_loyalup_encrypted_secret'


@override_settings(LOYALUP_SECRET_KEYS=[TEST_KEY])
class SecretDatabaseBehaviour(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.schema_editor() as editor:
            editor.create_model(SecretRoundTripModel)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            editor.delete_model(SecretRoundTripModel)
        super().tearDownClass()

    def test_real_storage_is_text_and_refresh_returns_original_token(self):
        raw = 'x' * 512
        obj = SecretRoundTripModel.objects.create(secret=raw)
        with connection.cursor() as cursor:
            cursor.execute('SELECT secret FROM test_loyalup_encrypted_secret WHERE id=%s', [obj.pk])
            stored = cursor.fetchone()[0]
        self.assertTrue(stored.startswith(PREFIX))
        self.assertGreater(len(stored), 512)
        obj.refresh_from_db()
        self.assertEqual(obj.secret, raw)

    def test_backfill_is_dry_by_default_and_idempotent(self):
        from apps.shared.config.management.commands.encrypt_integration_secrets import encrypt_model
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO test_loyalup_encrypted_secret (secret) VALUES (%s) RETURNING id", ['legacy-secret'])
            pk = cursor.fetchone()[0]
        self.assertEqual(encrypt_model(SecretRoundTripModel), {'plaintext': 1, 'encrypted': 0, 'updated': 0})
        with connection.cursor() as cursor:
            cursor.execute('SELECT secret FROM test_loyalup_encrypted_secret WHERE id=%s', [pk])
            self.assertEqual(cursor.fetchone()[0], 'legacy-secret')
        self.assertEqual(encrypt_model(SecretRoundTripModel, commit=True)['updated'], 1)
        self.assertEqual(encrypt_model(SecretRoundTripModel, commit=True), {'plaintext': 0, 'encrypted': 1, 'updated': 0})
        self.assertEqual(SecretRoundTripModel.objects.get(pk=pk).secret, 'legacy-secret')
