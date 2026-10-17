"""
Каналы гостя — склейка гостя, шаг А (17.10.2026): одна карточка, в ней все
каналы связи. Держим:
  1. ВК, телефон и Телеграм пишутся сами — по сохранению гостя и привязке;
  2. недоказанный телефон — канал, но не «доказан»; снятый номер уходит;
  3. один канал — одному гостю: чужой не перехватывается (это склейка, шаг Б);
  4. перепривязанный Телеграм переезжает вместе с привязкой;
  5. служебный номер ВК каналом «ВКонтакте» не считается;
  6. карточка отдаёт каналы по порядку.
"""
from django.test import TestCase

from apps.shared.guest.channels import каналы, отметить
from apps.shared.guest.models import Client, GuestChannel, TelegramLink


class КаналыГостя(TestCase):

    def setUp(self):
        self.гость = Client.objects.create(vk_id=700001, first_name='Аня')

    def вид(self, client=None):
        return {(c['kind'], c['external_id'], c['verified']) for c in каналы(client or self.гость)}

    def test_вк_телефон_и_телеграм_сами(self):
        self.assertEqual(self.вид(), {('vk', '700001', True)})
        self.гость.phone, self.гость.phone_source = '+79100000001', 'vk'
        self.гость.save()
        TelegramLink.objects.create(tg_user_id=42, client=self.гость)
        self.assertEqual(self.вид(), {('vk', '700001', True), ('phone', '+79100000001', True), ('tg', '42', True)})
        self.assertEqual([c['kind'] for c in каналы(self.гость)], ['tg', 'vk', 'phone'])

    def test_недоказанный_телефон_и_снятие(self):
        self.гость.phone, self.гость.phone_source = '+79100000002', 'vk_unverified'
        self.гость.save()
        self.assertIn(('phone', '+79100000002', False), self.вид())
        self.гость.phone, self.гость.phone_source = '', ''
        self.гость.save()
        self.assertFalse(GuestChannel.objects.filter(kind='phone').exists())

    def test_чужой_канал_не_перехватывается(self):
        другой = Client.objects.create(vk_id=700002)
        self.assertIsNone(отметить(другой, 'vk', 700001))
        self.assertEqual(GuestChannel.objects.get(kind='vk', external_id='700001').client, self.гость)

    def test_телеграм_переезжает_с_привязкой(self):
        link = TelegramLink.objects.create(tg_user_id=77, client=self.гость)
        новый = Client.objects.create(vk_id=700003)
        link.client = новый
        link.save()
        self.assertEqual(GuestChannel.objects.get(kind='tg', external_id='77').client, новый)

    def test_служебный_номер_вк_не_канал(self):
        тг = Client.objects.create(vk_id=9_000_000_000_042, is_synthetic_vk=True)
        self.assertFalse(GuestChannel.objects.filter(client=тг, kind='vk').exists())
