"""
Студия видео 19.10.2026: сценарий словами, правки версиями, стиль бренда, очередь сборщика.

Правила:
  1) в сценарий попадают только свои материалы; длины сцен в пределах, кусок
     видео не выходит за длину файла, фото без движения получает наезд;
  2) цены в титрах — только из базы знаний, задания или ответа владельца;
     чужая цена → вопрос, а не сборка;
  3) ИИ спросил — ролик ждёт ответа, версии нет;
  4) правка одной сцены не трогает остальные; новая версия снимает согласование;
  5) «вернуть версию» — без пересборки; «запомнить» поднимает версию стиля,
     повтор того же правила версию не поднимает;
  6) сборщик: без токена очередь выключена, чужой токен — 401; замещённая
     версия не собирается; ролик готов только после «готово» с файлами;
  7) API: рядовой не создаёт ролик; пока ИИ пишет — вторая правка 409.
"""
import json
import tempfile
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, override_settings
from django_tenants.test.cases import FastTenantTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant.marketer import studio, studio_worker as W
from apps.tenant.marketer.api import studio_views as V
from apps.tenant.marketer.models import (
    StudioAsset,
    StudioProject,
    StudioStyleVersion,
    StudioVersion,
)

MEDIA = tempfile.mkdtemp(prefix='studio-test-')
SCHEMA = 'studio_test'


class _Схема(FastTenantTestCase):
    """Общая тестовая схема сети (тестов здесь нет — только схема).

    FastTenantTestCase не зовёт setUpClass Django: декоратор override_settings на
    классе не включился бы, поэтому настройки включаются здесь руками.
    """
    extra_settings = {}

    @classmethod
    def setUpClass(cls):
        cls._over = override_settings(MEDIA_ROOT=MEDIA, **cls.extra_settings)
        cls._over.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._over.disable()

    @classmethod
    def get_test_schema_name(cls):
        return SCHEMA

    @classmethod
    def get_test_tenant_domain(cls):
        return 'studio.fast-test.com'

    @classmethod
    def setup_tenant(cls, tenant):
        tenant.client_id = 990019
        tenant.name = 'Студия тест'
        tenant.paid_until = date(2030, 1, 1)
        tenant.is_active = True


KB = 'Меню: Хачапури по-аджарски — 590 ₽. Сет «Большой» — 1 290 ₽.'


def _asset(project, kind='video', duration=10.0, title='кадр'):
    return StudioAsset.objects.create(
        project=project, kind=kind, title=title, duration=duration,
        file=SimpleUploadedFile(f'a.{"mp4" if kind == "video" else "jpg"}', b'x'))


def _project(**kw):
    values = dict(title='Хачапури', brief='Ролик про хачапури', duration=10)
    values.update(kw)
    return StudioProject.objects.create(**values)


def _scn(assets, captions=('', '')):
    return {'title': 'Хачапури', 'scenes': [
        {'asset_id': assets[i % len(assets)].pk, 'start': 0, 'length': 2.5, 'caption': c, 'motion': 'none'}
        for i, c in enumerate(captions)], 'final': {'offer': 'Ждём вас'}, 'music': 'brand'}


def _user(role='network_admin'):
    return SimpleNamespace(is_authenticated=True, is_active=True, is_superuser=False,
                           is_superadmin=False, is_network_admin=(role == 'network_admin'),
                           is_client=(role == 'client'), role=role, username='alina', pk=1,
                           get_full_name=lambda: 'Алина')


class СценарийЧистится(_Схема):

    def test_чужой_материал_и_длины(self):
        p = _project()
        video = _asset(p, 'video', duration=3.0)
        photo = _asset(p, 'photo', duration=None)
        raw = {'scenes': [
            {'asset_id': 9999, 'length': 2},                           # выдуманный
            {'asset_id': video.pk, 'start': 2.8, 'length': 5},         # за край файла
            {'asset_id': photo.pk, 'start': 4, 'length': 0.1},         # слишком коротко
        ]}
        scn = studio.clean_scenario(raw, [video, photo], 10, 'Ленина, 1')
        self.assertEqual([s['asset_id'] for s in scn['scenes']], [video.pk, photo.pk])
        v, ph = scn['scenes']
        self.assertLessEqual(v['start'] + v['length'], 3.0 + 0.01)
        self.assertEqual(ph['start'], 0.0)
        self.assertEqual(ph['length'], studio.MIN_SCENE)
        self.assertEqual(ph['motion'], 'zoom_in')
        self.assertEqual(scn['final']['address'], 'Ленина, 1')

    def test_пустой_сценарий_ошибка(self):
        with self.assertRaises(studio.StudioError):
            studio.clean_scenario({'scenes': [{'asset_id': 5}]}, [], 10)

    def test_цены_только_из_меню(self):
        scn = {'scenes': [{'caption': 'Хачапури 590 ₽'}, {'caption': 'Сет 1 290 ₽'}],
               'final': {'offer': 'Скидка 490 руб'}}
        self.assertEqual(studio.unknown_prices(scn, KB), ['490'])
        self.assertEqual(studio.unknown_prices(scn, KB, 'акция 490 ₽'), [])


@patch('apps.tenant.marketer.studio._kb_text', return_value=KB)
class План(_Схема):

    def test_вопрос_ИИ(self, _kb):
        p = _project()
        with patch('apps.tenant.marketer.studio._ask_ai', return_value={'question': 'Какая цена?'}):
            studio.plan(p)
        p.refresh_from_db()
        self.assertEqual(p.status, 'asking')
        self.assertEqual(p.question, 'Какая цена?')
        self.assertFalse(p.versions.exists())

    def test_цены_нет_в_меню__вопрос(self, _kb):
        p = _project()
        a = _asset(p)
        with patch('apps.tenant.marketer.studio._ask_ai',
                   return_value={'scenario': _scn([a], ('Хачапури 490 ₽', ''))}):
            studio.plan(p)
        p.refresh_from_db()
        self.assertEqual(p.status, 'asking')
        self.assertIn('490 ₽', p.question)
        self.assertFalse(p.versions.exists())

    def test_первая_версия_в_очереди(self, _kb):
        p = _project()
        a = _asset(p)
        with patch('apps.tenant.marketer.studio._ask_ai',
                   return_value={'scenario': _scn([a], ('Хачапури 590 ₽', ''))}):
            studio.plan(p)
        p.refresh_from_db()
        self.assertEqual(p.status, 'queued')
        self.assertEqual(p.current_version.number, 1)
        self.assertEqual(p.current_version.status, 'queued')
        self.assertEqual(p.current_version.style_version, studio.style_obj().version)


@patch('apps.tenant.marketer.studio._kb_text', return_value=KB)
class Правки(_Схема):

    def _ready(self):
        p = _project()
        a, b = _asset(p), _asset(p, 'photo', None)
        with patch('apps.tenant.marketer.studio._ask_ai',
                   return_value={'scenario': _scn([a, b], ('Первая', 'Вторая'))}):
            studio.plan(p)
        p.refresh_from_db()
        StudioVersion.objects.filter(pk=p.current_version_id).update(status='ready')
        p.status = 'ready'
        p.approval = 'approved'
        p.save()
        return p, a, b

    def test_одна_сцена(self, _kb):
        p, a, b = self._ready()
        reply = {'scenario': _scn([a, b], ('ИИ переписал', 'Новая вторая')), 'changelog': ['Титр 2'],
                 'remember': 'Логотип крупнее'}
        with patch('apps.tenant.marketer.studio._ask_ai', return_value=reply):
            studio.edit(p, 'другой титр', scene=2)
        p.refresh_from_db()
        v2 = p.current_version
        self.assertEqual(v2.number, 2)
        self.assertEqual([s['caption'] for s in v2.scenario['scenes']], ['Первая', 'Новая вторая'])
        self.assertEqual(v2.changelog, ['Титр 2'])
        self.assertEqual(v2.remember_suggestion, 'Логотип крупнее')
        self.assertEqual(p.approval, 'none')

    def test_вернуть_версию(self, _kb):
        p, a, b = self._ready()
        with patch('apps.tenant.marketer.studio._ask_ai',
                   return_value={'scenario': _scn([a, b]), 'changelog': []}):
            studio.edit(p, 'короче')
        studio.restore(p, 1)
        p.refresh_from_db()
        self.assertEqual(p.current_version.number, 1)
        self.assertEqual(p.status, 'ready')
        self.assertEqual(p.versions.count(), 2)

    def test_запомнить(self, _kb):
        v0 = studio.style_obj().version
        style = studio.remember('Логотип крупнее', 'Алина')
        self.assertEqual(style.version, v0 + 1)
        self.assertTrue(StudioStyleVersion.objects.filter(version=v0 + 1).exists())
        self.assertEqual(studio.remember('логотип крупнее').version, v0 + 1)
        self.assertIn('Логотип крупнее', studio.snapshot(style)['rules'])


@patch('apps.tenant.marketer.studio_worker._schemas', return_value=[SCHEMA])
class Сборщик(_Схема):
    extra_settings = {'STUDIO_WORKER_TOKEN': 'tok'}

    def _post(self, view, path, data=None, token='tok', files=None, **kw):
        rf = RequestFactory()
        headers = {'HTTP_X_STUDIO_WORKER_TOKEN': token} if token else {}
        if files is not None:
            req = rf.post(path, {**(data or {}), **files}, **headers)
        else:
            req = rf.post(path, json.dumps(data or {}), content_type='application/json', **headers)
        return view.as_view()(req, **kw)

    def _queued(self):
        p = _project()
        a = _asset(p)
        studio._new_version(p, studio.clean_scenario(_scn([a]), [a], 10))
        p.refresh_from_db()
        return p

    def test_токен(self, _s):
        with override_settings(STUDIO_WORKER_TOKEN=''):
            self.assertEqual(self._post(W.StudioClaimView, '/c/').status_code, 503)
        self.assertEqual(self._post(W.StudioClaimView, '/c/', token='чужой').status_code, 401)

    def test_полный_круг(self, _s):
        p = self._queued()
        r = self._post(W.StudioClaimView, '/c/', {'worker': 'vps'})
        job = json.loads(r.content)['job']
        self.assertEqual(job['version_id'], p.current_version_id)
        self.assertTrue(job['assets'][0]['url'].endswith(f'/file/asset-{job["assets"][0]["id"]}/'))
        p.refresh_from_db()
        self.assertEqual(p.status, 'rendering')
        # Второй сборщик ту же версию не получит.
        self.assertIsNone(json.loads(self._post(W.StudioClaimView, '/c/').content)['job'])
        kw = {'schema': SCHEMA, 'vid': job['version_id']}
        self.assertEqual(self._post(W.StudioProgressView, '/p/', {'stage': 'Рендер 9:16', 'progress': 40},
                                    **kw).status_code, 200)
        r = self._post(W.StudioOutputView, '/o/', {'fmt': '9x16', 'width': '1080', 'height': '1920',
                                                   'duration': '12.5'},
                       files={'mp4': SimpleUploadedFile('o.mp4', b'mp4', content_type='video/mp4')}, **kw)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self._post(W.StudioDoneView, '/d/', {}, **kw).status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.status, 'ready')
        self.assertEqual(p.current_version.files.get().height, 1920)
        # Готовую версию больше не тронуть.
        self.assertEqual(self._post(W.StudioDoneView, '/d/', {}, **kw).status_code, 409)

    def test_без_файлов_не_готово(self, _s):
        p = self._queued()
        job = json.loads(self._post(W.StudioClaimView, '/c/').content)['job']
        self._post(W.StudioDoneView, '/d/', {}, schema=SCHEMA, vid=job['version_id'])
        p.refresh_from_db()
        self.assertEqual(p.status, 'failed')
        self.assertIn('ни одного файла', p.error)

    def test_замещённая_версия_не_собирается(self, _s):
        p = self._queued()
        old = p.current_version
        studio._new_version(p, old.scenario, instruction='короче')
        job = json.loads(self._post(W.StudioClaimView, '/c/').content)['job']
        self.assertEqual(job['number'], 2)
        old.refresh_from_db()
        self.assertEqual(old.status, 'queued')


class Ручки(_Схема):

    def _call(self, view, method, data=None, user=None, **kw):
        f = APIRequestFactory()
        req = getattr(f, method)('/x/', data or {}, format='json')
        force_authenticate(req, user=user or _user())
        return view.as_view()(req, **kw)

    def test_рядовой_не_создаёт(self):
        r = self._call(V.StudioProjectListAPIView, 'post', {'brief': 'x'}, user=_user('client'))
        self.assertEqual(r.status_code, 403)

    @patch('apps.tenant.marketer.tasks.studio_script_task.delay')
    def test_создание_и_занято(self, delay):
        r = self._call(V.StudioProjectListAPIView, 'post', {'brief': 'Ролик про хачапури', 'duration': 99})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.data['status'], 'writing')
        self.assertEqual(r.data['duration'], studio.MAX_DURATION)
        delay.assert_called_once()
        r = self._call(V.StudioAnswerAPIView, 'post', {'answer': '590'}, pk=r.data['id'])
        self.assertEqual(r.status_code, 409)
