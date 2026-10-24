"""
Студия 23.10.2026: ИИ видит видео, режиссёр, «Улучшить», монтажные приёмы.

Правила:
  1) сборщик берёт на просмотр каждое новое видео один раз; зависший просмотр
     через 10 минут отдаётся снова; результат без листа кадров — «не смог»,
     и сценарий пишется вслепую, как раньше;
  2) раскадровка попадает в задание ИИ, отметки «смотрю» и «не смог» — нет;
  3) без сборщика сценарий раскадровку не ждёт;
  4) бриф режиссёра идёт в задание ИИ и в сценарий; сбой режиссёра сценарий
     не роняет;
  5) переходы, стиль титров, цвет и звуки — только из своих списков;
  6) «Улучшить»: рядовой — 403, пусто — 400, модель молчит — 502.
"""
import json
import time
from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from django.utils import timezone

from apps.tenant.marketer import studio, studio_worker as W
from apps.tenant.marketer.api import studio_views as V
from apps.tenant.marketer.models import SEEN_BUSY, SEEN_FAILED, StudioAsset
from apps.tenant.marketer.tests_studio_1019 import (
    KB,
    SCHEMA,
    _asset,
    _project,
    _scn,
    _user,
    _Схема,
)

# Общая схема на время тестов подменяет режиссёра пустым брифом; настоящий нужен правилу 4.
НАСТОЯЩИЙ_РЕЖИССЁР = studio.direct


class Раскадровка(_Схема):
    extra_settings = {'STUDIO_WORKER_TOKEN': 'tok'}

    def _post(self, view, data=None, files=None, **kw):
        req = RequestFactory().post('/s/', {**(data or {}), **(files or {})}, HTTP_X_STUDIO_WORKER_TOKEN='tok')
        return view.as_view()(req, **kw)

    def _claim(self):
        return json.loads(self._post(W.StudioSeeClaimView).content)['see']

    def test_один_раз_и_результат(self):
        p = _project()
        a = _asset(p, duration=12.0)
        job = self._claim()
        self.assertEqual(job['asset_id'], a.pk)
        self.assertIsNone(self._claim())                      # второй сборщик то же видео не получит
        sheet = SimpleUploadedFile('sheet.jpg', b'jpeg', content_type='image/jpeg')
        with patch('apps.tenant.marketer.studio.describe_sheet', return_value='0–2: хачапури крупно') as see:
            r = self._post(W.StudioSeeResultView, {'times': json.dumps([0, 1.5])}, {'sheet': sheet},
                           schema=SCHEMA, aid=a.pk)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(see.call_args.args[1:], ([0.0, 1.5], 12.0))
        a.refresh_from_db()
        self.assertEqual(a.seen, '0–2: хачапури крупно')
        # Уже посмотренное повторно не принимается.
        self.assertEqual(self._post(W.StudioSeeResultView, schema=SCHEMA, aid=a.pk).status_code, 409)

    def test_сбой_и_зависший_просмотр(self):
        p = _project()
        a = _asset(p)
        self._claim()
        StudioAsset.objects.filter(pk=a.pk).update(seen_at=timezone.now() - timedelta(minutes=11))
        self.assertEqual(self._claim()['asset_id'], a.pk)     # зависший отдан снова
        self._post(W.StudioSeeResultView, {'error': 'ffmpeg упал'}, schema=SCHEMA, aid=a.pk)
        a.refresh_from_db()
        self.assertEqual(a.seen, SEEN_FAILED)
        self.assertIsNone(self._claim())

    def test_фото_не_смотрится(self):
        _asset(_project(), 'photo', duration=None)
        self.assertIsNone(self._claim())


class ЗаданиеИИ(_Схема):

    def test_раскадровка_в_задании(self):
        p = _project()
        видел = _asset(p, title='хачапури')
        смотрит = _asset(p, title='зал')
        не_смог = _asset(p, title='кухня')
        StudioAsset.objects.filter(pk=видел.pk).update(seen='0–2: сыр тянется')
        StudioAsset.objects.filter(pk=смотрит.pk).update(seen=SEEN_BUSY)
        StudioAsset.objects.filter(pk=не_смог.pk).update(seen=SEEN_FAILED)
        текст = studio._asset_lines(StudioAsset.objects.filter(project=p).order_by('pk'))
        self.assertEqual(текст.count('РАСКАДРОВКА'), 1)
        self.assertIn('сыр тянется', текст)
        self.assertNotIn(SEEN_BUSY, текст)

    def test_без_сборщика_не_ждёт(self):
        p = _project()
        _asset(p)
        with override_settings(STUDIO_WORKER_TOKEN=''):
            t = time.monotonic()
            studio.wait_seen(list(StudioAsset.objects.filter(project=p)), timeout=30)
            self.assertLess(time.monotonic() - t, 1)

    def test_сборщик_есть__ждёт_только_до_срока(self):
        p = _project()
        _asset(p)
        with override_settings(STUDIO_WORKER_TOKEN='tok'):
            t = time.monotonic()
            out = studio.wait_seen(list(StudioAsset.objects.filter(project=p)), timeout=0.2, tick=0.05)
            self.assertLess(time.monotonic() - t, 2)
        self.assertEqual(len(out), 1)

    @patch('apps.tenant.marketer.studio._kb_text', return_value=KB)
    def test_бриф_режиссёра_в_задании_и_сценарии(self, _kb):
        p = _project()
        a = _asset(p)
        with patch('apps.tenant.marketer.studio.direct', return_value='Хук: сыр тянется 0–1 с'), \
                patch('apps.tenant.marketer.studio._ask_ai',
                      return_value={'scenario': _scn([a], ('Хачапури 590 ₽', ''))}) as ask:
            studio.plan(p)
        self.assertIn('БРИФ РЕЖИССЁРА:\nХук: сыр тянется', ask.call_args.args[1])
        p.refresh_from_db()
        self.assertEqual(p.current_version.scenario['director'], 'Хук: сыр тянется 0–1 с')

    def test_сбой_режиссёра_не_роняет(self):
        p = _project()
        a = _asset(p)
        with patch('apps.tenant.marketer.generator._ask', side_effect=RuntimeError('нет связи')):
            self.assertEqual(НАСТОЯЩИЙ_РЕЖИССЁР(p, [a], studio.snapshot(studio.style_obj()), ''), '')


class Приёмы(_Схема):

    def test_только_свои_списки(self):
        p = _project()
        a = _asset(p)
        raw = _scn([a], ('', '', ''))
        raw['scenes'][0].update(transition='whip', caption_pos='top')
        raw['scenes'][1].update(transition='взрыв', caption_pos='сбоку')
        raw.update(caption_style='karaoke', grade='cinematic', sfx=1)
        scn = studio.clean_scenario(raw, [a], 10)
        self.assertEqual([s['transition'] for s in scn['scenes']], ['whip', 'cut', 'cut'])
        self.assertEqual([s['caption_pos'] for s in scn['scenes']], ['top', 'bottom', 'bottom'])
        self.assertEqual((scn['caption_style'], scn['grade'], scn['sfx']), ('plate', 'cinematic', True))


class Улучшить(_Схема):

    def _call(self, view, method, data=None, user=None, **kw):
        req = getattr(APIRequestFactory(), method)('/x/', data or {}, format='json')
        force_authenticate(req, user=user or _user())
        return view.as_view()(req, **kw)

    def test_права_и_ответы(self):
        r = self._call(V.StudioImproveAPIView, 'post', {'text': 'хачапури'}, user=_user('client'))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._call(V.StudioImproveAPIView, 'post', {'text': ' '}).status_code, 400)
        with patch('apps.tenant.marketer.studio.improve', side_effect=RuntimeError('молчит')):
            self.assertEqual(self._call(V.StudioImproveAPIView, 'post', {'text': 'хачапури'}).status_code, 502)
        with patch('apps.tenant.marketer.studio.improve', return_value='Цель: …') as imp:
            r = self._call(V.StudioImproveAPIView, 'post', {'text': 'хачапури', 'kind': 'reel'})
        self.assertEqual((r.status_code, r.data['text']), (200, 'Цель: …'))
        imp.assert_called_once_with('хачапури', 'reel')
