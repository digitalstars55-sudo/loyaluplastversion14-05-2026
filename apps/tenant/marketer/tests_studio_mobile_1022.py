"""
Студия в телефоне по макету (22.10.2026).

Правила:
  1) шаги «Собираем ролик» — из того, что реально было: пишется сценарий → шаг 2,
     в очереди → шаг 4, сборщик на втором формате → шаг 5, готово → все шесть;
  2) очередь сети считает только версии, вставшие раньше;
  3) «Остановить» в сборке — версия «Остановлено», ролик возвращается к прежней
     готовой; сборщик остановленную версию больше не трогает; ответ ИИ после
     остановки не применяется;
  4) «На согласование и в контент-план» — ничего не публикует: план предложен,
     ролик ждёт согласования; «Опубликовать» без confirm не работает, с confirm —
     согласует и ставит строки плана, повтор не задваивает;
  5) автор в CheckUp берётся из имени входа `checkup-<id>-<схема>`;
  6) ВК-сторис — канал плана; событие в CheckUp несёт строки плана и кадр.
"""
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant.marketer import studio, studio_mobile as M
from apps.tenant.marketer.api import studio_views as V
from apps.tenant.marketer.models import StudioOutput, StudioProject, StudioVersion
from apps.tenant.marketer.tests_studio_1019 import SCHEMA, _asset, _project, _scn, _user, _Схема


def _call(view, data=None, user=None, method='post', **kw):
    req = getattr(APIRequestFactory(), method)('/x/', data or {}, format='json')
    force_authenticate(req, user=user or _user())
    return view.as_view()(req, **kw)


def _готовый(fmts=('9x16', '1x1', '16x9', '6s')):
    p = _project(post_text='Ждём вас')
    a = _asset(p)
    v = studio._new_version(p, studio.clean_scenario(_scn([a], ('Сет на двоих — 1 290 ₽', '')), [a], 10))
    StudioVersion.objects.filter(pk=v.pk).update(status='ready')
    for f in fmts:
        StudioOutput.objects.create(version=v, fmt=f, file=SimpleUploadedFile('o.mp4', b'mp4'),
                                    width=1080, height=1920, duration=8)
    p.refresh_from_db()
    p.status = 'ready'
    p.save()
    return p


class Шаги(_Схема):

    def test_по_ходу_сборки(self):
        p = _project(status='writing')
        _asset(p)
        шаги = M.steps(p, None)
        self.assertEqual([s['state'] for s in шаги], ['done', 'active', 'pending', 'pending', 'pending', 'pending'])
        a = p.assets.first()
        v = studio._new_version(p, studio.clean_scenario(_scn([a], ('Хачапури', 'Зал')), [a], 10))
        p.refresh_from_db()
        шаги = M.steps(p, v)
        self.assertEqual(шаги[1]['title'], 'Сценарий из 2 сцен')
        self.assertIn('Хачапури', шаги[1]['detail'])
        self.assertEqual(шаги[3]['state'], 'active')
        StudioVersion.objects.filter(pk=v.pk).update(status='rendering', stage='Собираю 1:1 (2 из 4)', progress=40)
        v.refresh_from_db()
        шаги = M.steps(p, v)
        self.assertEqual([s['state'] for s in шаги][:5], ['done'] * 4 + ['active'])
        StudioVersion.objects.filter(pk=v.pk).update(status='ready')
        v.refresh_from_db()
        self.assertTrue(all(s['state'] == 'done' for s in M.steps(p, v)))

    def test_очередь_только_раньше(self):
        p1, p2 = _project(), _project()
        a1, a2 = _asset(p1), _asset(p2)
        v1 = studio._new_version(p1, studio.clean_scenario(_scn([a1]), [a1], 10))
        v2 = studio._new_version(p2, studio.clean_scenario(_scn([a2]), [a2], 10))
        self.assertEqual((M.queue_ahead(v1), M.queue_ahead(v2)), (0, 1))
        self.assertGreater(M.eta_seconds(v2), M.eta_seconds(v1))


class Остановить(_Схема):

    def test_в_очереди__к_прежней_готовой(self):
        p = _готовый()
        старая = p.current_version
        studio._new_version(p, старая.scenario, instruction='короче')
        p.refresh_from_db()
        новая = p.current_version
        r = _call(V.StudioStopAPIView, pk=p.pk)
        self.assertEqual(r.status_code, 200)
        p.refresh_from_db()
        новая.refresh_from_db()
        self.assertEqual((p.status, p.current_version_id), ('ready', старая.pk))
        self.assertEqual(новая.status, 'failed')
        self.assertIn('Остановлено', новая.error)

    def test_без_готовой__ошибка_и_не_занят(self):
        p = _project(status='writing')
        self.assertEqual(_call(V.StudioStopAPIView, pk=p.pk).status_code, 200)
        p.refresh_from_db()
        self.assertEqual((p.status, p.error), ('failed', 'Сборку остановили'))
        self.assertEqual(_call(V.StudioStopAPIView, pk=p.pk).status_code, 409)

    @patch('apps.tenant.marketer.studio._kb_text', return_value='')
    def test_ответ_ИИ_после_остановки_не_применяется(self, _kb):
        p = _project(status='writing')
        a = _asset(p)

        def ии(*_a):
            StudioProject.objects.filter(pk=p.pk).update(status='failed')   # нажали «Остановить»
            return {'scenario': _scn([a])}
        with patch.object(studio, '_ask_ai', side_effect=ии):
            studio.plan(p)
        p.refresh_from_db()
        self.assertEqual((p.status, p.versions.count()), ('failed', 0))


class НаСогласование(_Схема):

    @patch('apps.tenant.marketer.studio_mobile.send_event_later')
    def test_план_предложен_ничего_не_публикуется(self, event):
        p = _готовый()
        r = _call(V.StudioSubmitPlanAPIView, pk=p.pk)
        self.assertEqual(r.status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.approval, 'waiting')
        self.assertEqual([x['channel'] for x in p.proposed_plan], ['vk', 'tg', 'vk_story'])
        self.assertEqual(p.publish_plan, [])
        event.assert_called_once()
        self.assertEqual(event.call_args[0][1], 'approval')
        строки = M.plan_lines(p)
        self.assertEqual(len(строки), 3)
        self.assertIn('ВК клип + пост', строки[0])
        self.assertIn('ВК сторис 6 с', строки[2])
        self.assertIn('1 290 ₽', M.price_line(p.current_version))

    @patch('apps.tenant.marketer.studio_mobile.send_event_later')
    def test_без_сторис_если_нет_формата(self, _e):
        p = _готовый(fmts=('9x16', '1x1'))
        _call(V.StudioSubmitPlanAPIView, pk=p.pk)
        p.refresh_from_db()
        self.assertEqual([x['channel'] for x in p.proposed_plan], ['vk', 'tg'])

    @patch('apps.tenant.marketer.studio_mobile.send_event_later')
    def test_опубликовать_только_с_подтверждением_и_один_раз(self, _e):
        p = _готовый()
        _call(V.StudioSubmitPlanAPIView, pk=p.pk)
        self.assertEqual(_call(V.StudioPublishPlanAPIView, {}, pk=p.pk).status_code, 400)
        r = _call(V.StudioPublishPlanAPIView, {'confirm': True}, pk=p.pk)
        self.assertEqual(r.status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.approval, 'approved')
        self.assertEqual([x['status'] for x in p.publish_plan], ['scheduled'] * 3)
        self.assertEqual(p.proposed_plan, [])
        _call(V.StudioPublishPlanAPIView, {'confirm': True}, pk=p.pk)       # двойное нажатие
        p.refresh_from_db()
        self.assertEqual(len(p.publish_plan), 3)

    def test_рядовой_не_публикует(self):
        p = _готовый()
        r = _call(V.StudioPublishPlanAPIView, {'confirm': True}, user=_user('client'), pk=p.pk)
        self.assertEqual(r.status_code, 403)


class Прочее(_Схема):

    def test_автор_из_входа_CheckUp(self):
        from types import SimpleNamespace
        self.assertEqual(M.checkup_id_of(SimpleNamespace(username=f'checkup-1-{SCHEMA}')), '1')
        self.assertEqual(M.checkup_id_of(SimpleNamespace(username='alina')), '')

    @patch('apps.tenant.marketer.tasks.studio_script_task.delay')
    def test_серия_при_создании_и_в_списке(self, _d):
        r = _call(V.StudioProjectListAPIView, {'brief': 'Осенний сет', 'series': 'реклама блюд'})
        self.assertEqual(r.data['series'], 'реклама блюд')
        r = _call(V.StudioProjectListAPIView, method='get')
        self.assertEqual(r.data['series'], ['реклама блюд'])
        self.assertIn('build', r.data['results'][0])

    def test_сторис_в_плане(self):
        from apps.tenant.marketer import studio_publish as sp
        p = _готовый()
        p.approval = 'approved'
        p.save()
        sp.запланировать(p, [{'channel': 'vk_story'}], None)
        with patch.object(sp, 'в_вк_сторис', return_value='55') as st:
            sp.опубликовать_назревшее()
        st.assert_called_once()
        self.assertEqual(st.call_args[0][1], '6s')

    @patch('apps.tenant.marketer.studio_mobile.requests.Session')
    def test_событие_в_CheckUp(self, sess):
        from django.test import override_settings
        p = _готовый()
        p.proposed_plan = M.propose_plan(p)
        p.author_checkup_id = '1'
        p.save()
        sess.return_value.post.return_value.status_code = 200
        with override_settings(LOYALUP_RELAY_SECRET='s'):
            self.assertTrue(M.send_event(p, 'approval'))
        body = sess.return_value.post.call_args.kwargs['json']
        self.assertEqual((body['event'], body['author_checkup_id'], body['tenant']), ('approval', '1', SCHEMA))
        self.assertEqual(len(body['lines']), 3)
        with override_settings(LOYALUP_RELAY_SECRET=''):
            self.assertFalse(M.send_event(p, 'ready'))


class Фотобанк(_Схема):

    def test_фото_из_фотобанка_и_удаление_не_трогает_файл(self):
        from apps.tenant.marketer.models import MarketerPhoto, StudioAsset
        фото = MarketerPhoto.objects.create(image=SimpleUploadedFile('d.jpg', b'jpg'), title='Сет')
        p = _project()
        r = _call(V.StudioAssetListAPIView, {'photo_id': фото.pk, 'project_id': p.pk})
        self.assertEqual(r.status_code, 201)
        a = StudioAsset.objects.get(pk=r.data['id'])
        self.assertEqual((a.kind, a.file.name, a.project_id), ('photo', фото.image.name, p.pk))
        self.assertEqual(_call(V.StudioAssetDetailAPIView, method='delete', pk=a.pk).status_code, 204)
        фото.refresh_from_db()
        self.assertTrue(фото.image.storage.exists(фото.image.name))
        self.assertEqual(_call(V.StudioAssetListAPIView, {'photo_id': 999999}).status_code, 404)
