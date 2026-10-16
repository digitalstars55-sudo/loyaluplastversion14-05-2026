"""
16.10.2026 — задание для ИИ в окне правки поста и кнопки карт в ручном ответе.

Правила:
  1) переписывание получает задание, текущий текст и базу знаний; возвращает
     только новый текст (JSON-ответ модели тоже понимается);
  2) пустое задание — ошибка, а не «перегенерировать как-нибудь»;
  3) ручка только ВОЗВРАЩАЕТ текст: пост в базе не меняется;
  4) ручной ответ с attach_links=true уходит с той же фразой и кнопками
     Яндекс/2ГИС, что у автоответа; без флага — как раньше.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.shared.ai.openai_compat import _response_message
from apps.tenant.marketer import generator
from apps.tenant.marketer.api import views as M
from apps.tenant.marketer.models import MarketerPostStatus


def _fake(text, sent):
    message = _response_message({'output': [{'type': 'message', 'content': [
        {'type': 'output_text', 'text': text}]}], 'usage': {}})

    def create(**kw):
        sent.update(kw)
        return message
    return SimpleNamespace(messages=SimpleNamespace(create=create))


class RewritePostTest(SimpleTestCase):

    def _run(self, reply, **kw):
        sent = {}
        with patch('apps.shared.ai.openai_compat.Anthropic', return_value=_fake(reply, sent)), \
             patch('apps.shared.ai.usage.log_usage'):
            out = generator.rewrite_post(kw.pop('text', 'Старый пост про хачапури'),
                                         kw.pop('instruction', 'Короче и без эмодзи'),
                                         kb_text='Меню: хачапури по-аджарски', **kw)
        return out, sent

    def test_задание_текст_и_база_уходят_модели(self):
        out, sent = self._run('Хачапури по-аджарски — уже в печи.')
        self.assertEqual(out['text'], 'Хачапури по-аджарски — уже в печи.')
        msg = sent['messages'][0]['content']
        self.assertIn('Короче и без эмодзи', msg)
        self.assertIn('Старый пост про хачапури', msg)
        self.assertIn('хачапури по-аджарски', msg)

    def test_json_ответ_модели_понимается(self):
        out, _ = self._run(json.dumps({'text': 'Новый текст'}))
        self.assertEqual(out['text'], 'Новый текст')

    def test_пустое_задание_ошибка(self):
        with self.assertRaises(RuntimeError):
            generator.rewrite_post('текст', '   ', kb_text='')


class RewriteViewDoesNotSaveTest(SimpleTestCase):

    def test_ручка_возвращает_текст_и_не_сохраняет(self):
        post = MagicMock(pk=7, text='Старый', status=MarketerPostStatus.DRAFT)
        req = APIRequestFactory().post('/api/v1/marketer/posts/7/rewrite/',
                                       {'instruction': 'Сделай теплее'}, format='json')
        force_authenticate(req, user=MagicMock(is_authenticated=True))
        with patch.object(M, '_may_write', return_value=True), \
             patch.object(M, '_post_or_none', return_value=post), \
             patch.object(M, '_settings_row', return_value=MagicMock(brand_voice='')), \
             patch.object(M, 'current_schema_name', return_value='t'), \
             patch.object(M.MarketerPostRewriteAPIView, 'permission_classes', []), \
             patch('apps.tenant.marketer.generator.rewrite_post',
                   return_value={'text': 'Тёплый текст', 'model': 'm'}) as rw:
            resp = M.MarketerPostRewriteAPIView.as_view()(req, pk=7)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['text'], 'Тёплый текст')
        self.assertEqual(rw.call_args.args[:2], ('Старый', 'Сделай теплее'))
        post.save.assert_not_called()


class ManualReplyLinksTest(SimpleTestCase):

    def _post(self, data):
        from apps.tenant.mobile.api import views as V
        conv = MagicMock(pk=5, vk_sender_id='123', branch_id=1)
        req = APIRequestFactory().post('/api/v1/mobile/reviews/5/reply/', data, format='json')
        force_authenticate(req, user=MagicMock(is_authenticated=True))
        kb = {'inline': True, 'buttons': [[{'action': {'type': 'open_link', 'link': 'y', 'label': 'Я'}}]]}
        cfg = MagicMock(auto_send_links_text='Будем рады отзыву — кнопки ниже')
        with patch.object(V, 'get_object_or_404', return_value=conv), \
             patch.object(V, '_check_conv_access', return_value=True), \
             patch.object(V.MobileReviewReplyAPIView, 'permission_classes', []), \
             patch('apps.tenant.analytics.auto_reply.build_review_links_keyboard', return_value=kb), \
             patch('apps.tenant.branch.models.ReviewAutoReplyConfig.get_singleton', return_value=cfg), \
             patch('apps.tenant.branch.api.services.send_vk_reply') as send:
            send.return_value = MagicMock(pk=1)
            try:
                V.MobileReviewReplyAPIView.as_view()(req, review_id=5)
            except Exception:
                pass                       # ответ сериализует MagicMock — нам важен вызов
        return send, kb

    def test_с_флагом_фраза_и_кнопки(self):
        send, kb = self._post({'text': 'Спасибо!', 'attach_links': True})
        text = send.call_args.args[1]
        self.assertIn('Спасибо!', text)
        self.assertIn('кнопки ниже', text)
        self.assertEqual(send.call_args.kwargs['keyboard'], kb)

    def test_без_флага_как_раньше(self):
        send, _ = self._post({'text': 'Спасибо!'})
        self.assertEqual(send.call_args.args[1], 'Спасибо!')
        self.assertIsNone(send.call_args.kwargs['keyboard'])
