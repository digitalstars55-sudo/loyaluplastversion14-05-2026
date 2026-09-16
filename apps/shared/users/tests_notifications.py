"""
Лента уведомлений: курсор before_id, since, type, unread — на настоящих строках
(Notification — public-схема, таблица в тест-БД есть). Без параметров ответ
как раньше: последние limit штук.
"""
from datetime import timedelta
from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.shared.users.api.views import NotificationListAPIView
from apps.shared.users.models import Notification, User


class NotificationCursorTest(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='notif-t', password='x')
        other = User.objects.create_user(username='notif-o', password='x')
        now = timezone.now()
        self.ids = []
        for i in range(7):
            n = Notification.objects.create(
                user=self.user, type='review_new' if i % 2 == 0 else 'draft_ready',
                title=f'n{i}', body='', data={}, read_at=None if i >= 5 else now,
            )
            Notification.objects.filter(pk=n.pk).update(created_at=now - timedelta(minutes=60 - i))
            self.ids.append(n.pk)
        Notification.objects.create(user=other, type='review_new', title='чужое')

    def _get(self, **params):
        request = APIRequestFactory().get('/api/v1/notifications/', params)
        force_authenticate(request, user=self.user)
        r = NotificationListAPIView.as_view()(request)
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def test_default_is_newest_first_and_only_mine(self):
        d = self._get()
        self.assertEqual([n['title'] for n in d['notifications']], [f'n{i}' for i in range(6, -1, -1)])
        self.assertEqual(d['unread'], 2)
        self.assertFalse(d['has_more'])
        self.assertIsNone(d['next_before_id'])

    def test_cursor_pages_without_overlap(self):
        first = self._get(limit=3)
        self.assertTrue(first['has_more'])
        self.assertEqual(len(first['notifications']), 3)
        second = self._get(limit=3, before_id=first['next_before_id'])
        ids1 = {n['id'] for n in first['notifications']}
        ids2 = {n['id'] for n in second['notifications']}
        self.assertFalse(ids1 & ids2)
        third = self._get(limit=3, before_id=second['next_before_id'])
        self.assertEqual(len(third['notifications']), 1)
        self.assertFalse(third['has_more'])

    def test_type_and_unread_filters(self):
        d = self._get(type='draft_ready')
        self.assertTrue(all(n['type'] == 'draft_ready' for n in d['notifications']))
        self.assertEqual(len(d['notifications']), 3)
        d = self._get(unread=1)
        self.assertEqual(len(d['notifications']), 2)
        self.assertTrue(all(not n['read'] for n in d['notifications']))

    def test_since_returns_only_newer(self):
        cutoff = (timezone.now() - timedelta(minutes=56, seconds=30)).isoformat()
        d = self._get(since=cutoff)
        self.assertEqual([n['title'] for n in d['notifications']], ['n6', 'n5', 'n4'])

    def test_garbage_params_do_not_break(self):
        d = self._get(limit='abc', before_id='x', since='вчера', type='')
        self.assertEqual(len(d['notifications']), 7)
        self.assertEqual(d['limit'], 50)
