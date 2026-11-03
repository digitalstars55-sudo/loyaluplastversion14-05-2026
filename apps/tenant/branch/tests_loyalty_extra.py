"""Этап 1 (02.11.2026): награда за друга один раз, уровень считается по визитам, сертификат не уходит в минус."""
from datetime import date, timedelta
from unittest import mock

from django_tenants.test.cases import TenantTestCase
from django.utils import timezone

from apps.shared.guest.models import Client

from . import loyalty_extra as LE
from .models import (
    Branch, ClientBranch, ClientBranchVisit, CoinTransaction, GiftCertificate, ReferralReward, TransactionSource,
    TransactionType,
)


class _Сеть:
    def __init__(self, **kw):
        self.referral_enabled = kw.get('referral_enabled', False)
        self.referral_inviter_coins = kw.get('referral_inviter_coins', 100)
        self.referral_friend_coins = kw.get('referral_friend_coins', 50)
        self.levels_enabled = kw.get('levels_enabled', False)
        self.levels = kw.get('levels', [])


class _Сетевой(TenantTestCase):
    @classmethod
    def setup_tenant(cls, tenant):
        tenant.client_id = 990102
        tenant.name = 'Этап 1'
        tenant.paid_until = date(2099, 1, 1)
        return tenant


class Друг(_Сетевой):

    def setUp(self):
        self.точка = Branch.objects.create(name='Точка', branch_id=901)
        self.он = ClientBranch.objects.create(client=Client.objects.create(vk_id=9101, first_name='Он'), branch=self.точка)
        self.друг = ClientBranch.objects.create(client=Client.objects.create(vk_id=9102, first_name='Друг'), branch=self.точка,
                                                invited_by=self.он)
        self.сеть = _Сеть(referral_enabled=True)
        патч = mock.patch.object(LE, '_конфиг', return_value=self.сеть)
        патч.start()
        self.addCleanup(патч.stop)

    def заказ(self, кто, сумма=200, источник=TransactionSource.RECEIPT):
        return CoinTransaction.objects.create_transfer(кто, сумма, TransactionType.INCOME, источник, 'заказ')

    def test_монеты_обоим_один_раз(self):
        self.assertEqual(self.он.coins_balance, 0)
        self.заказ(self.друг)
        self.заказ(self.друг)
        self.assertEqual(ReferralReward.objects.count(), 1)
        self.assertEqual(self.он.coins_balance, 100)
        self.assertEqual(self.друг.coins_balance, 200 + 200 + 50)

    def test_выключено_наград_нет(self):
        self.сеть.referral_enabled = False
        self.заказ(self.друг)
        self.assertEqual(ReferralReward.objects.count(), 0)

    def test_переход_без_заказа_наград_нет(self):
        CoinTransaction.objects.create_transfer(self.друг, 10, TransactionType.INCOME, TransactionSource.GAME, 'игра')
        self.assertEqual(ReferralReward.objects.count(), 0)

    def test_старый_гость_не_считается_приглашённым(self):
        ClientBranch.objects.filter(pk=self.друг.pk).update(created_at=timezone.now() - timedelta(days=60))
        self.друг.refresh_from_db()
        self.заказ(self.друг)
        self.assertEqual(ReferralReward.objects.count(), 0)

    def test_уровень_и_бонус_отдельной_строкой(self):
        self.сеть.levels_enabled = True
        self.сеть.referral_enabled = False
        for _ in range(5):
            ClientBranchVisit.objects.create(client=self.он)
        уровень = LE.уровень_гостя(self.он.client_id, self.сеть)
        self.assertEqual((уровень['name'], уровень['bonus_pct'], уровень['next_name'], уровень['next_left']),
                         ('Свой', 5, 'Постоянный', 10))
        self.заказ(self.он, 200)
        self.assertEqual(self.он.coins_balance, 210)
        self.assertTrue(CoinTransaction.objects.filter(client=self.он, source=TransactionSource.LEVEL, amount=10).exists())


class Сертификат(_Сетевой):

    def setUp(self):
        from django.contrib.auth import get_user_model
        У = get_user_model()
        self.кассир = У.objects.create_user(username='cert-cashier', password='x')
        патч = mock.patch('apps.tenant.branch.api.loyalty_extra_views._config',
                          return_value=mock.Mock(certificates_enabled=True))
        патч.start()
        self.addCleanup(патч.stop)

    def вью(self, cls, метод, путь, данные=None, **kw):
        from rest_framework.test import APIRequestFactory, force_authenticate
        запрос = getattr(APIRequestFactory(), метод)(путь, данные or {}, format='json')
        force_authenticate(запрос, self.кассир)
        return cls.as_view()(запрос, **kw)

    def test_выпуск_и_частичное_списание_без_минуса(self):
        from .api.loyalty_extra_views import CertificateListView, CertificateRedeemView
        r = self.вью(CertificateListView, 'post', '/c/', {'nominal': 1000, 'recipient_name': 'Анна'})
        self.assertEqual(r.status_code, 201)
        pk = r.data['id']
        r = self.вью(CertificateRedeemView, 'post', '/r/', {'amount': 400}, pk=pk)
        self.assertEqual(r.data['balance'], 600)
        r = self.вью(CertificateRedeemView, 'post', '/r/', {'amount': 601}, pk=pk)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(GiftCertificate.objects.get(pk=pk).balance, 600)

    def test_просроченный_и_отменённый_не_списываются(self):
        from .api.loyalty_extra_views import CertificateRedeemView, CertificateVoidView
        с = GiftCertificate.objects.create(nominal=500, balance=500, expires_at=timezone.localdate() - timedelta(days=1))
        self.assertEqual(self.вью(CertificateRedeemView, 'post', '/r/', {'amount': 1}, pk=с.pk).status_code, 409)
        в = GiftCertificate.objects.create(nominal=500, balance=500)
        self.assertEqual(self.вью(CertificateVoidView, 'post', '/v/', pk=в.pk).data['status'], 'void')
        self.assertEqual(self.вью(CertificateRedeemView, 'post', '/r/', {'amount': 1}, pk=в.pk).status_code, 409)

    def test_свой_номинал_и_своя_дата(self):
        from .api.loyalty_extra_views import CertificateListView
        срок = (timezone.localdate() + timedelta(days=45)).isoformat()
        r = self.вью(CertificateListView, 'post', '/c/', {'nominal': 2750, 'expires_at': срок, 'message': 'С днём рождения'})
        self.assertEqual(r.status_code, 201)
        self.assertEqual((r.data['nominal'], r.data['expires_at'], r.data['message']), (2750, срок, 'С днём рождения'))

    def test_настройки_сети_ограничивают_списание(self):
        from . import certificates as CERT
        с = GiftCertificate.objects.create(nominal=500, balance=500)
        кфг = mock.Mock(certificate_settings={'partial': False})
        self.assertEqual(CERT.почему_нельзя_списать(с, 100, None, кфг)[0], 'only_full')
        self.assertIsNone(CERT.почему_нельзя_списать(с, 500, None, кфг))

    def test_настройки_проверяются(self):
        from . import certificates as CERT
        итог = CERT.проверить_настройки({}, {'nominals': ['3000', 1000], 'period_months': 6, 'logo_corner': 'bl'})
        self.assertEqual((итог['nominals'], итог['period_months'], итог['logo_corner']), ([1000, 3000], 6, 'bl'))
        for плохое in ({'nominals': []}, {'nominals': [0]}, {'period_months': -1}, {'logo_corner': 'x'}, {'redeem_scope': 'x'}):
            with self.assertRaises(CERT.НеверныеНастройки):
                CERT.проверить_настройки({}, плохое)

    def test_не_картинка_отказ(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from . import certificates as CERT
        with self.assertRaises(CERT.НеверныеНастройки):
            CERT.подготовить_файл(SimpleUploadedFile('a.jpg', b'not image'), 'design')

    def test_загрузка_убирает_лишнее_и_держит_логотип_прозрачным(self):
        import io

        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image

        from . import certificates as CERT
        буфер = io.BytesIO()
        Image.new('RGBA', (3000, 1000), (1, 2, 3, 0)).save(буфер, 'PNG')
        файл = CERT.подготовить_файл(SimpleUploadedFile('a.png', буфер.getvalue()), 'logo')
        картинка = Image.open(io.BytesIO(файл.read()))
        self.assertEqual((картинка.size[0], картинка.mode), (2000, 'RGBA'))
