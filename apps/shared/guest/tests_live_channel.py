"""
Рассылка в живой канал (шаг В склейки гостя, 17.10.2026). Держим:
  1. служебная карточка (гость без ВК) — сообщение в Телеграм, ВК не трогаем;
  2. ВК закрыт, а бот открыт — запасной путь в Телеграм; выключатель его гасит;
  3. ВК закрыт и Телеграма нет — пропуск, как раньше;
  4. гость остановил бота (403) — канал помечен «нельзя писать», больше не пробуем;
  5. кнопки-ссылки ВК переносятся, номер сообщения ВК не выдумывается (None).
"""
from unittest import mock

from django.test import TestCase, override_settings

from apps.shared.guest import live_channel as L
from apps.shared.guest.channels import СЛУЖЕБНЫЙ_VK_ОТ
from apps.shared.guest.models import Client, GuestChannel, TelegramLink
from apps.tenant.senler import services as S

TG = 'test-token'


def _ответ(ok=True, code=None):
    r = mock.Mock()
    r.json.return_value = {'ok': True, 'result': {'message_id': 7}} if ok else {
        'ok': False, 'error_code': code, 'description': 'Forbidden: bot was blocked by the user'}
    return r


@override_settings(TELEGRAM_GUEST_BOT_TOKEN=TG, GUEST_LIVE_CHANNEL='on')
class ЖивойКанал(TestCase):
    def setUp(self):
        self.cfg = mock.Mock(vk_group_id=1, vk_community_token='x')

    def test_служебная_карточка_уходит_в_телеграм(self):
        c = Client.objects.create(vk_id=СЛУЖЕБНЫЙ_VK_ОТ + 42)
        TelegramLink.objects.create(tg_user_id=42, client=c)
        with mock.patch('requests.post', return_value=_ответ()) as post, \
                mock.patch.object(S, '_is_messages_allowed') as vk:
            ok, err, mid = S.send_vk_message(self.cfg, c.vk_id, 'Привет', keyboard={
                'inline': True, 'buttons': [[{'action': {'type': 'open_link', 'link': 'https://x.ru', 'label': 'Меню'}}]]})
        self.assertTrue(ok, err)
        self.assertIsNone(mid)
        vk.assert_not_called()
        url, = post.call_args.args
        self.assertIn('api.telegram.org', url)
        body = post.call_args.kwargs['json']
        self.assertEqual(body['chat_id'], 42)
        self.assertEqual(body['reply_markup']['inline_keyboard'][0][0], {'text': 'Меню', 'url': 'https://x.ru'})

    def test_вк_закрыт_бот_открыт_запасной_путь(self):
        c = Client.objects.create(vk_id=700100)
        TelegramLink.objects.create(tg_user_id=77, client=c)
        with mock.patch.object(S, '_is_messages_allowed', return_value=(False, 'opt-in')), \
                mock.patch('requests.post', return_value=_ответ()) as post:
            ok, _, _ = S.send_vk_message(self.cfg, 700100, 'Акция')
        self.assertTrue(ok)
        self.assertEqual(post.call_args.kwargs['json']['chat_id'], 77)
        with override_settings(GUEST_LIVE_CHANNEL='off'), \
                mock.patch.object(S, '_is_messages_allowed', return_value=(False, 'opt-in')), \
                mock.patch('requests.post') as post2:
            ok, err, _ = S.send_vk_message(self.cfg, 700100, 'Акция')
        self.assertFalse(ok)
        self.assertEqual(err, 'opt-in')
        post2.assert_not_called()

    def test_вк_закрыт_телеграма_нет_пропуск(self):
        Client.objects.create(vk_id=700200)
        with mock.patch.object(S, '_is_messages_allowed', return_value=(False, 'opt-in')), \
                mock.patch('requests.post') as post:
            ok, err, _ = S.send_vk_message(self.cfg, 700200, 'Акция')
        self.assertFalse(ok)
        self.assertEqual(err, 'opt-in')
        post.assert_not_called()

    def test_бот_остановлен_больше_не_пишем(self):
        c = Client.objects.create(vk_id=СЛУЖЕБНЫЙ_VK_ОТ + 9)
        TelegramLink.objects.create(tg_user_id=9, client=c)
        self.assertTrue(GuestChannel.objects.filter(kind='tg', external_id='9').exists())
        with mock.patch('requests.post', return_value=_ответ(ok=False, code=403)):
            ok, err, _ = L.отправить(c.vk_id, 'x')
        self.assertFalse(ok)
        self.assertEqual(err, L.СТОП)
        self.assertIs(GuestChannel.objects.get(kind='tg', external_id='9').can_message, False)
        self.assertIsNone(L.телеграм_гостя(c.vk_id))
