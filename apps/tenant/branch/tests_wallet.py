"""Wallet: файл карты подписан и верен, веб-служба пускает только по ключу, флаг выключен — 404."""
import hashlib
import io
import json
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone as dt_tz
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory

from apps.shared.guest.models import Client

from . import wallet
from .models import WalletDevice, WalletPass


def _серт(папка, имя):
    ключ = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    название = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, имя)])
    теперь = datetime.now(dt_tz.utc)
    серт = (x509.CertificateBuilder().subject_name(название).issuer_name(название).public_key(ключ.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(теперь - timedelta(days=1)).not_valid_after(теперь + timedelta(days=1)).sign(ключ, hashes.SHA256()))
    (Path(папка) / f'{имя}.pem').write_bytes(серт.public_bytes(serialization.Encoding.PEM))
    (Path(папка) / f'{имя}.key').write_bytes(ключ.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return str(Path(папка) / f'{имя}.pem'), str(Path(папка) / f'{имя}.key')


class _Клиент:
    """Вызов вьюх напрямую: сеть в тестах одна, маршрутизация по домену не нужна."""
    фабрика = APIRequestFactory()

    def _вью(self, путь):
        from .api import urls
        for p in urls.urlpatterns:
            m = p.resolve(путь.removeprefix('/api/v1/'))
            if m:
                return m.func, m.kwargs
        raise AssertionError(путь)

    def get(self, путь, data=None, **kw):
        f, a = self._вью(путь)
        return f(self.фабрика.get(путь, data or {}, **kw), **a)

    def post(self, путь, data=None, content_type=None, **kw):
        f, a = self._вью(путь)
        return f(self.фабрика.post(путь, data or {}, format='json', **kw), **a)

    def delete(self, путь, **kw):
        f, a = self._вью(путь)
        return f(self.фабрика.delete(путь, **kw), **a)


class WalletTest(TestCase):
    @classmethod
    def setUpClass(cls):
        # Таблицы сети в тестовой базе есть только в схемах сетей: заводим две свои на время класса.
        with connection.schema_editor() as e:
            e.create_model(WalletPass)
            e.create_model(WalletDevice)
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pem, cls.key = _серт(cls.tmp.name, 'pass')
        cls.wwdr, _ = _серт(cls.tmp.name, 'wwdr')

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        super().tearDownClass()
        with connection.schema_editor() as e:
            e.delete_model(WalletDevice)
            e.delete_model(WalletPass)

    def setUp(self):
        self.ключи = override_settings(WALLET_PASS_TYPE_ID='pass.ru.loyalup.card', WALLET_TEAM_ID='TEAM', WALLET_PASS_CERT=self.pem, WALLET_PASS_KEY=self.key, WALLET_WWDR_CERT=self.wwdr)
        self.ключи.enable()
        self.addCleanup(self.ключи.disable)
        self.client_http = _Клиент()
        for имя, значение in (('баланс', 120), ('код_на_кассе', ''), ('_точки_рядом', []), ('_сеть', 'LevOne'), ('_конфиг', None)):
            заплатка = mock.patch.object(wallet, имя, return_value=значение)
            заплатка.start()
            self.addCleanup(заплатка.stop)
        self.guest = Client.objects.create(vk_id=555001, first_name='Анна', last_name='К')
        self.wp = wallet.карта_гостя(self.guest, 'levone.levelupapp.ru')

    def test_файл_собран_хэши_сходятся(self):
        z = zipfile.ZipFile(io.BytesIO(wallet.собрать_pkpass(self.guest, self.wp)))
        манифест = json.loads(z.read('manifest.json'))
        self.assertIn('icon.png', манифест)
        for имя, хэш in манифест.items():
            self.assertEqual(hashlib.sha1(z.read(имя)).hexdigest(), хэш)
        self.assertGreater(len(z.read('signature')), 500)
        карта = json.loads(z.read('pass.json'))
        self.assertEqual(карта['serialNumber'], self.wp.serial)
        self.assertEqual(карта['storeCard']['primaryFields'][0]['value'], 120)
        self.assertEqual(карта['webServiceURL'], 'https://levone.levelupapp.ru/api/v1/wallet/apple')

    def test_подпись_проверяется_openssl(self):
        import shutil
        import subprocess
        if not shutil.which('openssl'):
            self.skipTest('нет openssl')
        z = zipfile.ZipFile(io.BytesIO(wallet.собрать_pkpass(self.guest, self.wp)))
        with tempfile.TemporaryDirectory() as d:
            z.extractall(d)
            r = subprocess.run(['openssl', 'cms', '-verify', '-binary', '-noverify', '-inform', 'DER', '-in', f'{d}/signature', '-content', f'{d}/manifest.json', '-out', '/dev/null'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_скачать_только_с_ключом(self):
        url = f'/api/v1/wallet/pass/{self.wp.serial}/'
        self.assertEqual(self.client_http.get(url, {'t': 'чужой'}).status_code, 404)
        r = self.client_http.get(url, {'t': self.wp.auth_token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r['Content-Type'], 'application/vnd.apple.pkpass')

    def test_регистрация_и_список_изменившихся(self):
        url = f'/api/v1/wallet/apple/v1/devices/dev1/registrations/pass.ru.loyalup.card/{self.wp.serial}'
        self.assertEqual(self.client_http.post(url, {'pushToken': 'abc'}, content_type='application/json').status_code, 401)
        шапка = {'HTTP_AUTHORIZATION': f'ApplePass {self.wp.auth_token}'}
        self.assertEqual(self.client_http.post(url, {'pushToken': 'abc'}, content_type='application/json', **шапка).status_code, 201)
        self.assertEqual(self.client_http.post(url, {'pushToken': 'abc'}, content_type='application/json', **шапка).status_code, 200)
        self.assertEqual(WalletDevice.objects.count(), 1)
        список = self.client_http.get('/api/v1/wallet/apple/v1/devices/dev1/registrations/pass.ru.loyalup.card')
        self.assertEqual(список.data['serialNumbers'], [self.wp.serial])
        метка = список.data['lastUpdated']
        self.assertEqual(self.client_http.get('/api/v1/wallet/apple/v1/devices/dev1/registrations/pass.ru.loyalup.card', {'passesUpdatedSince': метка}).status_code, 204)
        self.assertEqual(self.client_http.delete(url, **шапка).status_code, 200)
        self.assertEqual(WalletDevice.objects.count(), 0)

    def test_свежая_карта_по_ключу(self):
        url = f'/api/v1/wallet/apple/v1/passes/pass.ru.loyalup.card/{self.wp.serial}'
        self.assertEqual(self.client_http.get(url).status_code, 401)
        self.assertEqual(self.client_http.get(url, HTTP_AUTHORIZATION=f'ApplePass {self.wp.auth_token}').status_code, 200)

    def test_гостю_выключенный_флаг_404(self):
        self.assertFalse(wallet.включён())
        self.assertEqual(self.client_http.get('/api/v1/client/wallet/', {'vk_id': 555001}).status_code, 404)

    def test_без_ключей_не_предлагается(self):
        with override_settings(WALLET_PASS_CERT='', WALLET_GOOGLE_SA_JSON=''):
            with mock.patch.object(wallet, '_конфиг', return_value=mock.Mock(wallet_enabled=True)):
                self.assertFalse(wallet.включён())

    def test_монеты_двигаются_карта_отмечена(self):
        было = WalletPass.objects.get(pk=self.wp.pk).changed_at
        with mock.patch('apps.tenant.branch.tasks.wallet_push.delay') as пуш:
            wallet.отметить_изменение(self.guest.id)
        пуш.assert_called_once()
        self.assertGreaterEqual(WalletPass.objects.get(pk=self.wp.pk).changed_at, было)
