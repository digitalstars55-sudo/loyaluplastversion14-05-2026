"""Integration secrets encrypted at rest; historical plaintext remains readable."""
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

PREFIX = 'luenc:v1:'


@lru_cache(maxsize=4)
def _cipher(keys):
    if not keys:
        raise ImproperlyConfigured('LOYALUP_SECRET_KEYS is required to store integration secrets.')
    try:
        return MultiFernet([Fernet(key.encode('ascii')) for key in keys])
    except (ValueError, UnicodeError) as exc:
        raise ImproperlyConfigured('Invalid LOYALUP_SECRET_KEYS configuration.') from exc


def cipher():
    return _cipher(tuple(getattr(settings, 'LOYALUP_SECRET_KEYS', ()) or ()))


class EncryptedSecretField(models.TextField):
    """TEXT storage avoids truncation of ciphertext; forms retain logical limits."""
    def __init__(self, *args, max_length=None, **kwargs):
        self.logical_max_length = max_length
        super().__init__(*args, max_length=max_length, **kwargs)

    def _decrypt(self, value):
        if not isinstance(value, str) or not value.startswith(PREFIX):
            return value
        try:
            return cipher().decrypt(value[len(PREFIX):].encode('ascii')).decode('utf-8')
        except (InvalidToken, UnicodeError) as exc:
            # Returning ciphertext as a usable token silently breaks integrations.
            raise ImproperlyConfigured('Integration secret cannot be decrypted; check LOYALUP_SECRET_KEYS.') from exc

    def from_db_value(self, value, expression, connection):
        return self._decrypt(value)

    def to_python(self, value):
        return self._decrypt(super().to_python(value))

    def get_prep_value(self, value):
        if value is None or value == '':
            return value
        if isinstance(value, str) and value.startswith(PREFIX):
            self._decrypt(value)
            return value
        value = super().get_prep_value(value)
        return PREFIX + cipher().encrypt(value.encode('utf-8')).decode('ascii')
