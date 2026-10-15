"""
Маркетолог 16.10.2026: план по базе знаний, фирменный стиль картинок, фотобанк,
ход генерации, без второго поста на день.

Правила:
  1) план: по пункту на каждую дату; чужие даты и мусор отбрасываются, пропуск
     закрывается запасной темой; тип поста только из списка;
  2) пост приходит вместе с «что на картинке»;
  3) образ картинки = общий стиль + фирменный стиль сети + сцена из базы знаний;
  4) фото из банка подбирается по словам из названия и меток, при равенстве —
     реже ставившееся; ничего не совпало — фото нет;
  5) день, где уже стоит живой пост, план пропускает (отклонённый не мешает);
  6) образцы стиля не приняты — картинка всё равно рисуется, без них;
  7) зависшая генерация показывается упавшей, а не «идёт» вечно;
  8) «откуда брать картинку» принимает только ai / bank_first.
"""
import base64
import json
from datetime import datetime, timedelta, timezone as dtz
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from apps.shared.ai.openai_compat import _response_message
from apps.tenant.marketer import bank, generator, images, progress
from apps.tenant.marketer.api import views as M
from apps.tenant.marketer.models import MarketerPostStatus
from apps.tenant.marketer.tasks import free_slots

DATES = ['2026-10-20', '2026-10-22', '2026-10-25']


class PlanParseTest(SimpleTestCase):

    def test_по_пункту_на_дату_и_мусор_отброшен(self):
        raw = json.dumps({'items': [
            {'date': '2026-10-22', 'topic': 'Хачапури', 'post_type': 'insight', 'task': 'про хачапури'},
            {'date': '2026-10-20', 'topic': 'Осень', 'post_type': 'weird', 'task': 'про осень'},
            {'date': '2026-11-30', 'topic': 'Чужая дата', 'post_type': 'promo', 'task': 'x'},
            {'date': '2026-10-20', 'topic': 'Дубль', 'post_type': 'promo', 'task': 'y'},
            'мусор',
        ]})
        plan = generator.parse_plan(raw, DATES)
        self.assertEqual(plan[0]['topic'], 'Осень')
        self.assertEqual(plan[0]['post_type'], 'custom')            # неизвестный тип → custom
        self.assertEqual(plan[1]['topic'], 'Хачапури')
        # Пункт с чужой датой встаёт на свободную дату, а не теряется.
        self.assertEqual(plan[2]['topic'], 'Чужая дата')

    def test_нечитаемый_ответ_даёт_пропуски(self):
        self.assertEqual(generator.parse_plan('не json', DATES), [None, None, None])
        self.assertEqual(generator.parse_plan('{"items": []}', DATES), [None, None, None])


@override_settings(OPENAI_API_KEY='synthetic-test-key')
class PostVisualTest(SimpleTestCase):

    def test_пост_приходит_с_картинкой_словами(self):
        output = [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(
            {'text': 'Хачапури уже в печи', 'visual': 'Хачапури крупно, утренний свет'})}]}]
        message = _response_message({'output': output, 'usage': {}})
        sent = {}

        def create(**kw):
            sent.update(kw)
            return message
        fake = SimpleNamespace(messages=SimpleNamespace(create=create))
        with patch('apps.shared.ai.openai_compat.Anthropic', return_value=fake), \
             patch('apps.shared.ai.usage.log_usage'):
            out = generator.generate_post_ex({'brand_name': 'X'}, kb_text='Рубрика: блюдо недели')
        self.assertEqual(out['text'], 'Хачапури уже в печи')
        self.assertEqual(out['visual'], 'Хачапури крупно, утренний свет')
        self.assertIn('БАЗА ЗНАНИЙ', sent['messages'][0]['content'])
        self.assertIn('"visual"', sent['system'])


class PromptTest(SimpleTestCase):

    def test_образ_собирается_из_стиля_сцены_и_темы(self):
        post = SimpleNamespace(topic='Завтраки', text='Текст поста', visual='Сырники на светлом столе')
        p = images.prompt_for(post, 'тёплое золото, тёмное дерево, без людей', with_references=True)
        self.assertIn(images.STYLE, p)
        self.assertIn(images.REFERENCE_NOTE, p)
        self.assertIn('тёплое золото', p)
        self.assertIn('Scene: Сырники на светлом столе', p)
        self.assertIn('Theme: Завтраки.', p)
        self.assertNotIn(images.REFERENCE_NOTE, images.prompt_for(post))


def _ph(pk, title, tags='', used=0):
    return SimpleNamespace(pk=pk, title=title, tags=tags, used_count=used)


class BankMatchTest(SimpleTestCase):

    def test_выбирает_по_словам_и_реже_ставившееся(self):
        photos = [_ph(1, 'Хачапури по-аджарски', 'завтрак', used=3),
                  _ph(2, 'Паста карбонара'),
                  _ph(3, 'Хачапури с сыром', used=0)]
        best = bank.best_match('Хачапури по-аджарски и кофе', '', photos)
        self.assertEqual(best.pk, 1)                                   # два слова совпали
        best = bank.best_match('Горячие хачапури ждут', '', photos)
        self.assertEqual(best.pk, 3)                                   # по одному — реже ставившееся

    def test_ничего_общего_нет_фото(self):
        self.assertIsNone(bank.best_match('Подарки в приложении', '', [_ph(1, 'Паста')]))
        # Общие слова («кафе», «блюдо») не считаются совпадением.
        self.assertIsNone(bank.best_match('Наше кафе и блюдо дня', '', [_ph(1, 'Блюдо кафе')]))


def _slot(day):
    return timezone.make_aware(datetime(2026, 10, day, 12))


class FreeSlotsTest(SimpleTestCase):

    def test_второй_пост_на_день_не_ставится(self):
        posts = [SimpleNamespace(scheduled_at=_slot(16), status=MarketerPostStatus.DRAFT),
                 SimpleNamespace(scheduled_at=_slot(18), status=MarketerPostStatus.REJECTED),
                 SimpleNamespace(scheduled_at=_slot(21), status=MarketerPostStatus.SCHEDULED)]
        free, taken = free_slots([_slot(16), _slot(18), _slot(21), _slot(23)], posts)
        self.assertEqual([s.day for s in free], [18, 23])               # отклонённый не мешает
        self.assertEqual([s.day for s in taken], [16, 21])


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


@override_settings(OPENAI_API_KEY='k', OPENAI_BASE_URL='https://proxy.example/v1')
class DrawFallbackTest(SimpleTestCase):

    def test_образцы_не_приняты_рисуем_без_них(self):
        ok = _Resp(200, {'data': [{'b64_json': base64.b64encode(b'JPG').decode()}], 'usage': {}})
        sess = MagicMock()
        sess.post.side_effect = [_Resp(400, {'error': {'message': 'edits not supported'}}), ok]
        ref = SimpleNamespace(image=MagicMock(name='f'))
        ref.image.name = 'marketer_bank/a.jpg'
        ref.image.read.return_value = b'REF'
        with patch.object(images, '_session', return_value=sess):
            raw, _ = images._draw(images.REFERENCE_NOTE + ' Scene: суп', [ref])
        self.assertEqual(raw, b'JPG')
        first, second = sess.post.call_args_list
        self.assertTrue(first.args[0].endswith('/images/edits'))
        self.assertTrue(second.args[0].endswith('/images/generations'))
        self.assertNotIn(images.REFERENCE_NOTE, second.kwargs['json']['prompt'])


class ProgressTest(SimpleTestCase):

    def test_зависшая_генерация_показывается_упавшей(self):
        old = timezone.now() - timedelta(minutes=30)
        job = SimpleNamespace(pk=1, kind='image', status='running', stage='draw', stage_label='Рисую',
                              step=0, total=0, post_id=7, result={}, error='', created_by='',
                              created_at=old, updated_at=old, finished_at=None)
        d = progress.to_dict(job)
        self.assertEqual(d['status'], 'failed')
        self.assertTrue(d['error'])
        self.assertEqual([s['key'] for s in d['stages']], ['bank', 'compose', 'draw', 'save'])
        job.updated_at = timezone.now()
        self.assertEqual(progress.to_dict(job)['status'], 'running')


class SettingsTest(SimpleTestCase):

    def test_источник_картинки_только_из_списка(self):
        self.assertEqual(M._parse_settings({'image_source': 'bank_first', 'image_style': 'тёплый свет'}),
                         {'image_source': 'bank_first', 'image_style': 'тёплый свет'})
        with self.assertRaises(M.PayloadError):
            M._parse_settings({'image_source': 'stock'})
