"""Волна 3: отложенные посты, картинка к посту, контент-план."""
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant.marketer.api import views as M
from apps.tenant.marketer.models import MarketerPostStatus
from apps.tenant.marketer.tests_api import MP, _Cache, _call, _cfg, _post, _posts_patch, _settings_patch, _user


class ScheduleTest(SimpleTestCase):
    def schedule(self, post, value, user=None):
        objects_patch, _ = _posts_patch(post)
        with objects_patch, _settings_patch(_cfg()), patch(MP + '_group_id', return_value=123):
            return _call(M.MarketerPostScheduleAPIView, 'post', '/x/', data={'scheduled_at': value},
                         pk=post.pk, user=user)

    def test_future_time_schedules(self):
        post = _post()
        when = (timezone.now() + timedelta(days=1)).isoformat()
        resp = self.schedule(post, when)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(post.status, MarketerPostStatus.SCHEDULED)
        self.assertIsNotNone(post.scheduled_at)

    def test_past_time_rejected(self):
        resp = self.schedule(_post(), (timezone.now() - timedelta(minutes=1)).isoformat())
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))

    def test_unschedule_back_to_draft(self):
        post = _post(status=MarketerPostStatus.SCHEDULED)
        post.scheduled_at = timezone.now() + timedelta(days=1)
        resp = self.schedule(post, None)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(post.status, MarketerPostStatus.DRAFT)
        self.assertIsNone(post.scheduled_at)

    def test_published_cannot_be_scheduled(self):
        resp = self.schedule(_post(status=MarketerPostStatus.PUBLISHED),
                             (timezone.now() + timedelta(days=1)).isoformat())
        self.assertEqual(resp.status_code, 409)

    def test_empty_text_cannot_be_scheduled(self):
        resp = self.schedule(_post(text=''), (timezone.now() + timedelta(days=1)).isoformat())
        self.assertEqual(resp.status_code, 409)

    def test_client_role_forbidden(self):
        resp = self.schedule(_post(), (timezone.now() + timedelta(days=1)).isoformat(),
                             user=_user(role='client'))
        self.assertEqual(resp.status_code, 403)


class ImageTest(SimpleTestCase):
    def upload(self, post, f):
        objects_patch, _ = _posts_patch(post)
        request = APIRequestFactory().post('/x/', {'image': f}, format='multipart')
        force_authenticate(request, user=_user())
        with objects_patch, _settings_patch(_cfg()), patch(MP + 'post_to_dict', return_value={}):
            return M.MarketerPostImageAPIView.as_view()(request, pk=post.pk)

    def test_not_image_rejected(self):
        resp = self.upload(_post(), SimpleUploadedFile('a.txt', b'hello', content_type='text/plain'))
        self.assertEqual(resp.status_code, 400)

    def test_image_saved(self):
        post = _post()
        resp = self.upload(post, SimpleUploadedFile('a.png', b'\x89PNG', content_type='image/png'))
        self.assertEqual(resp.status_code, 200)
        post.save.assert_called_with(update_fields=['image', 'updated_at'])


class PlanTest(SimpleTestCase):
    def test_slots_spread_over_week(self):
        start = date(2026, 10, 20)
        slots = M.plan_slots(start, 7, 3, 12)
        self.assertEqual(len(slots), 3)
        self.assertEqual([timezone.localtime(s).date() for s in slots],
                         [date(2026, 10, 20), date(2026, 10, 22), date(2026, 10, 25)])
        self.assertTrue(all(timezone.localtime(s).hour == 12 for s in slots))
        self.assertEqual(len(M.plan_slots(start, 31, 7, 12)), 31)

    def test_plan_queues_task(self):
        task = MagicMock()
        start = (timezone.localdate() + timedelta(days=1)).isoformat()
        with _settings_patch(_cfg()), patch(MP + 'cache', _Cache()), \
             patch(MP + 'current_schema_name', return_value='levone'), \
             patch('apps.tenant.marketer.tasks.build_content_plan_task', task):
            resp = _call(M.MarketerContentPlanAPIView, 'post', '/x/',
                         data={'start': start, 'days': 7, 'per_week': 2, 'hour': 11})
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(len(resp.data['slots']), 2)
        task.delay.assert_called_once()

    def test_plan_validates(self):
        with _settings_patch(_cfg()), patch(MP + 'cache', _Cache()):
            for data in ({'start': '2020-01-01'},
                         {'start': timezone.localdate().isoformat(), 'days': 40}):
                resp = _call(M.MarketerContentPlanAPIView, 'post', '/x/', data=data)
                self.assertEqual(resp.status_code, 400, data)


class PublisherScheduledTest(SimpleTestCase):
    def test_scheduled_post_is_publishable_and_image_failure_is_honest(self):
        from apps.tenant.marketer import publisher as P
        post = _post(status=MarketerPostStatus.SCHEDULED)
        post.image = MagicMock(name='img')
        with patch.object(P.MarketerSettings, 'objects') as objects, \
             patch.object(P, 'upload_wall_photo', return_value=(None, 'нет прав photos')), \
             patch.object(P.requests, 'post') as wall:
            objects.first.return_value = _cfg()
            self.assertFalse(P.publish_post(post))
        wall.assert_not_called()
        self.assertEqual(post.status, MarketerPostStatus.FAILED)
        self.assertIn('Картинка', post.error)

    def test_attachment_sent(self):
        from apps.tenant.marketer import publisher as P
        post = _post(status=MarketerPostStatus.SCHEDULED)
        post.image = MagicMock(name='img')
        resp = MagicMock()
        resp.json.return_value = {'response': {'post_id': 99}}
        with patch.object(P.MarketerSettings, 'objects') as objects, \
             patch.object(P, 'upload_wall_photo', return_value=('photo-1_2', '')), \
             patch.object(P.requests, 'post', return_value=resp) as wall:
            objects.first.return_value = _cfg()
            self.assertTrue(P.publish_post(post))
        self.assertEqual(wall.call_args.kwargs['data']['attachments'], 'photo-1_2')
        self.assertEqual(post.status, MarketerPostStatus.PUBLISHED)


class DuePublishTaskTest(SimpleTestCase):
    def test_publishes_due_once_and_skips_disabled(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from apps.tenant.marketer import tasks as T
        from apps.tenant.marketer.models import MarketerPost, MarketerSettings
        post = _post(status=MarketerPostStatus.SCHEDULED)
        posts = MagicMock()
        posts.filter.return_value.order_by.return_value.values_list.return_value = [post.pk]
        posts.select_for_update.return_value.filter.return_value.first.return_value = post
        cfgs = MagicMock()
        cfgs.first.side_effect = [_cfg(), _cfg(is_enabled=False)]
        publish = MagicMock(return_value=True)
        tenants = [SimpleNamespace(schema_name='levone'), SimpleNamespace(schema_name='off')]
        with patch.object(T, 'beat_tenants', return_value=tenants), \
             patch('django_tenants.utils.schema_context', lambda s: nullcontext()), \
             patch('django.db.transaction.atomic', lambda: nullcontext()), \
             patch.object(MarketerPost, 'objects', posts), \
             patch.object(MarketerSettings, 'objects', cfgs), \
             patch('apps.tenant.marketer.publisher.publish_post', publish):
            result = T.publish_due_marketer_posts_task.run()
        self.assertEqual(result['published'], 1)
        publish.assert_called_once_with(post)
