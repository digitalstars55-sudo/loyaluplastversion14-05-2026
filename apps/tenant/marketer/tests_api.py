"""
API AI-маркетолога для кабинета CheckUp (контракт 3б.5, №29).

Модели тенантные, задачи и ВК настоящие — в тестах не идём ни в БД, ни в
очередь, ни в сеть: менеджеры моделей подменяются через `patch.object`,
`publish_post` и задача дайджеста — по своим модулям, кэш — фейком со словарём.

Что проверяем прежде всего:
  • токен стены не утекает в ответ и не принимается на запись;
  • блокировка генерации: второй запуск в окне получает 409, а не второй
    дайджест на стене;
  • публикация идемпотентна и не уходит в `wall.post` повторно;
  • ошибка ВК доходит до кабинета текстом ВК (502), а не как 500.
"""
from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.tenant.marketer.api import views as M
from apps.tenant.marketer.models import MarketerPostStatus, MarketerPostType

MP = 'apps.tenant.marketer.api.views.'
STATUS_LABELS = dict(MarketerPostStatus.choices)
TYPE_LABELS = dict(MarketerPostType.choices)


class _Cache:
    """Кэш-заместитель: важны только add (атомарная установка) и delete."""

    def __init__(self):
        self.store = {}

    def add(self, key, value, timeout=None):
        if key in self.store:
            return False
        self.store[key] = value
        return True

    def delete(self, key):
        self.store.pop(key, None)


def _cfg(**over):
    values = dict(is_enabled=True, vk_group_id=123, vk_wall_token='vk1.a.secret',
                  autopost_enabled=False, digest_enabled=True, digest_weekday=0,
                  digest_hour=12, last_digest_at=None, brand_voice='', extra_facts='')
    values.update(over)
    cfg = SimpleNamespace(**values)
    cfg.save = MagicMock()
    return cfg


def _post(pk=7, status=MarketerPostStatus.DRAFT, post_type=MarketerPostType.DIGEST,
          text='Текст поста', vk_post_id='', error='', created_by='ai'):
    post = SimpleNamespace(
        pk=pk, id=pk, post_type=post_type, status=status, text=text,
        context_snapshot={'scans': 10}, model_used='claude-haiku-4-5-20251001',
        created_by=created_by, published_at=None, vk_post_id=vk_post_id, error=error,
        created_at=datetime(2026, 9, 18, 10, 0, tzinfo=dt_timezone.utc),
        updated_at=datetime(2026, 9, 18, 10, 0, tzinfo=dt_timezone.utc),
    )
    post.get_status_display = lambda: STATUS_LABELS[post.status]
    post.get_post_type_display = lambda: TYPE_LABELS[post.post_type]
    post.save = MagicMock()
    post.refresh_from_db = MagicMock()
    return post


def _user(role='network_admin', full_name='Алина Петрова'):
    return SimpleNamespace(
        is_authenticated=True, is_active=True, is_superuser=False,
        is_superadmin=(role == 'superadmin'), is_network_admin=(role == 'network_admin'),
        is_client=(role == 'client'), role=role, username='alina', pk=1, is_staff=True,
        get_full_name=lambda: full_name,
    )


def _call(view_cls, method, path, *, data=None, params=None, user=None, **kwargs):
    factory = APIRequestFactory()
    if method == 'get':
        request = factory.get(path, params or {})
    else:
        request = getattr(factory, method)(path, data or {}, format='json')
    force_authenticate(request, user=user or _user())
    return view_cls.as_view()(request, **kwargs)


def _settings_patch(cfg):
    objects = MagicMock()
    objects.first.return_value = cfg
    return patch.object(M.MarketerSettings, 'objects', objects)


def _posts_patch(post):
    objects = MagicMock()
    objects.filter.return_value.first.return_value = post
    return patch.object(M.MarketerPost, 'objects', objects), objects


# ── доступ ───────────────────────────────────────────────────────────────────

class AuthTest(SimpleTestCase):

    VIEWS = (M.MarketerSettingsAPIView, M.MarketerPostListAPIView, M.MarketerPostDetailAPIView,
             M.MarketerPostContextAPIView, M.MarketerGenerateAPIView,
             M.MarketerPostPublishAPIView, M.MarketerPostRejectAPIView)

    def test_every_view_requires_auth(self):
        for view in self.VIEWS:
            with self.subTest(view=view.__name__):
                self.assertIn(IsAuthenticated, view.permission_classes)

    def test_anonymous_is_rejected(self):
        factory = APIRequestFactory()
        resp = M.MarketerSettingsAPIView.as_view()(factory.get('/api/v1/marketer/settings/'))
        self.assertIn(resp.status_code, (401, 403))


# ── настройки ────────────────────────────────────────────────────────────────

class SettingsTest(SimpleTestCase):

    def test_token_never_leaves_the_server(self):
        with _settings_patch(_cfg()):
            resp = _call(M.MarketerSettingsAPIView, 'get', '/api/v1/marketer/settings/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('vk_wall_token', resp.data)
        self.assertTrue(resp.data['vk_wall_token_set'])
        self.assertNotIn('secret', str(resp.data))

    def test_token_set_is_false_without_token(self):
        with _settings_patch(_cfg(vk_wall_token='')):
            resp = _call(M.MarketerSettingsAPIView, 'get', '/api/v1/marketer/settings/')
        self.assertFalse(resp.data['vk_wall_token_set'])

    def test_token_cannot_be_written(self):
        with _settings_patch(_cfg()):
            resp = _call(M.MarketerSettingsAPIView, 'patch', '/api/v1/marketer/settings/',
                         data={'vk_wall_token': 'vk1.a.new'})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))
        self.assertIn('vk_wall_token', resp.data['detail'])

    def test_foreign_field_is_rejected(self):
        with _settings_patch(_cfg()):
            resp = _call(M.MarketerSettingsAPIView, 'patch', '/api/v1/marketer/settings/',
                         data={'brand_color': '#fff'})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))
        self.assertEqual(resp.data['editable'], M.EDITABLE_SETTINGS)

    def test_client_cannot_write(self):
        with _settings_patch(_cfg()):
            resp = _call(M.MarketerSettingsAPIView, 'patch', '/api/v1/marketer/settings/',
                         data={'is_enabled': False}, user=_user(role='client'))
        self.assertEqual((resp.status_code, resp.data['code']), (403, 'role_not_allowed'))

    def test_validation(self):
        for payload in ({'digest_weekday': 9}, {'digest_hour': 24}, {'digest_hour': 'позже'},
                        {'vk_group_id': -5}, {'is_enabled': 'может быть'}, {}):
            with self.subTest(payload=payload), _settings_patch(_cfg()):
                resp = _call(M.MarketerSettingsAPIView, 'patch', '/api/v1/marketer/settings/',
                             data=payload)
                self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))

    def test_patch_saves_only_given_fields(self):
        cfg = _cfg()
        with _settings_patch(cfg):
            resp = _call(M.MarketerSettingsAPIView, 'patch', '/api/v1/marketer/settings/',
                         data={'digest_weekday': 4, 'brand_voice': 'по-дружески'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(cfg.digest_weekday, 4)
        self.assertEqual(cfg.brand_voice, 'по-дружески')
        cfg.save.assert_called_once_with(update_fields=['brand_voice', 'digest_weekday',
                                                        'updated_at'])
        self.assertEqual(resp.data['digest_weekday_label'], 'Пятница')

    def test_group_id_can_be_cleared(self):
        cfg = _cfg()
        with _settings_patch(cfg):
            _call(M.MarketerSettingsAPIView, 'patch', '/api/v1/marketer/settings/',
                  data={'vk_group_id': None})
        self.assertIsNone(cfg.vk_group_id)


# ── лента и карточка ─────────────────────────────────────────────────────────

class PostsTest(SimpleTestCase):

    def test_list_filters_are_validated(self):
        objects = MagicMock()
        objects.all.return_value.order_by.return_value = MagicMock()
        with patch.object(M.MarketerPost, 'objects', objects), _settings_patch(_cfg()):
            for params in ({'status': 'опубликован'}, {'post_type': 'сторис'}):
                with self.subTest(params=params):
                    resp = _call(M.MarketerPostListAPIView, 'get', '/api/v1/marketer/posts/',
                                 params=params)
                    self.assertEqual((resp.status_code, resp.data['code']),
                                     (400, 'invalid_payload'))

    def test_list_returns_rows_with_vk_url(self):
        post = _post(status=MarketerPostStatus.PUBLISHED, vk_post_id='456')
        qs = MagicMock()
        qs.count.return_value = 1
        qs.__getitem__ = lambda self_, item: [post]
        objects = MagicMock()
        objects.all.return_value.order_by.return_value = qs
        with patch.object(M.MarketerPost, 'objects', objects), _settings_patch(_cfg()), \
             patch(MP + '_group_id', return_value=123):
            resp = _call(M.MarketerPostListAPIView, 'get', '/api/v1/marketer/posts/')
        self.assertEqual(resp.data['total'], 1)
        row = resp.data['results'][0]
        self.assertEqual(row['vk_post_url'], 'https://vk.com/wall-123_456')
        self.assertEqual(row['created_by_label'], 'ИИ')
        self.assertFalse(row['is_editable'], 'опубликованный пост не правим')

    def test_limit_is_capped(self):
        qs = MagicMock()
        qs.count.return_value = 0
        qs.__getitem__ = lambda self_, item: []
        objects = MagicMock()
        objects.all.return_value.order_by.return_value = qs
        with patch.object(M.MarketerPost, 'objects', objects), _settings_patch(_cfg()), \
             patch(MP + '_group_id', return_value=None):
            resp = _call(M.MarketerPostListAPIView, 'get', '/api/v1/marketer/posts/',
                         params={'limit': '9999'})
        self.assertEqual(resp.data['limit'], M.MAX_LIMIT)

    def test_detail_404(self):
        objects_patch, _ = _posts_patch(None)
        with objects_patch, _settings_patch(_cfg()):
            resp = _call(M.MarketerPostDetailAPIView, 'get', '/api/v1/marketer/posts/7/', pk=7)
        self.assertEqual((resp.status_code, resp.data['code']), (404, 'not_found'))

    def test_context_returns_snapshot(self):
        objects_patch, _ = _posts_patch(_post())
        with objects_patch:
            resp = _call(M.MarketerPostContextAPIView, 'get',
                         '/api/v1/marketer/posts/7/context/', pk=7)
        self.assertEqual(resp.data['context'], {'scans': 10})

    def test_patch_text_of_failed_keeps_status(self):
        post = _post(status=MarketerPostStatus.FAILED, error='VK error 214: Access denied')
        objects_patch, _ = _posts_patch(post)
        with objects_patch, _settings_patch(_cfg()), patch(MP + '_group_id', return_value=123):
            resp = _call(M.MarketerPostDetailAPIView, 'patch', '/api/v1/marketer/posts/7/',
                         data={'text': 'Исправленный текст'}, pk=7)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(post.text, 'Исправленный текст')
        self.assertEqual(post.status, MarketerPostStatus.FAILED, 'правка текста не лечит статус')
        post.save.assert_called_once_with(update_fields=['text', 'updated_at'])

    def test_patch_published_is_409(self):
        post = _post(status=MarketerPostStatus.PUBLISHED)
        objects_patch, _ = _posts_patch(post)
        with objects_patch, _settings_patch(_cfg()):
            resp = _call(M.MarketerPostDetailAPIView, 'patch', '/api/v1/marketer/posts/7/',
                         data={'text': 'правка'}, pk=7)
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'not_editable'))
        post.save.assert_not_called()

    def test_patch_empty_text_is_400(self):
        post = _post()
        objects_patch, _ = _posts_patch(post)
        with objects_patch, _settings_patch(_cfg()):
            resp = _call(M.MarketerPostDetailAPIView, 'patch', '/api/v1/marketer/posts/7/',
                         data={'text': '   '}, pk=7)
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'invalid_payload'))

    def test_client_cannot_edit(self):
        objects_patch, _ = _posts_patch(_post())
        with objects_patch, _settings_patch(_cfg()):
            resp = _call(M.MarketerPostDetailAPIView, 'patch', '/api/v1/marketer/posts/7/',
                         data={'text': 'правка'}, user=_user(role='client'), pk=7)
        self.assertEqual((resp.status_code, resp.data['code']), (403, 'role_not_allowed'))


# ── генерация ────────────────────────────────────────────────────────────────

class GenerateTest(SimpleTestCase):

    def _generate(self, cfg, cache_obj, task=None):
        task = task or MagicMock()
        with _settings_patch(cfg), patch(MP + 'cache', cache_obj), \
             patch(MP + 'current_schema_name', return_value='levone'), \
             patch('apps.tenant.marketer.tasks.run_marketer_digest_for_tenant_task', task):
            resp = _call(M.MarketerGenerateAPIView, 'post', '/api/v1/marketer/posts/generate/')
        return resp, task

    def test_disabled_marketer_is_409(self):
        resp, task = self._generate(_cfg(is_enabled=False), _Cache())
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'marketer_disabled'))
        task.delay.assert_not_called()

    def test_disabled_digest_is_409(self):
        resp, _ = self._generate(_cfg(digest_enabled=False), _Cache())
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'marketer_disabled'))

    def test_first_call_queues_task(self):
        cache_obj = _Cache()
        resp, task = self._generate(_cfg(), cache_obj)
        self.assertEqual(resp.status_code, 202)
        self.assertTrue(resp.data['queued'])
        task.delay.assert_called_once_with('levone')
        self.assertIn('marketer-generate:levone', cache_obj.store)

    def test_second_call_in_the_window_is_409(self):
        cache_obj = _Cache()
        self._generate(_cfg(), cache_obj)
        resp, task = self._generate(_cfg(), cache_obj)
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'already_generating'))
        self.assertEqual(resp.data['retry_after'], M.GENERATE_LOCK_SECONDS)
        task.delay.assert_not_called()

    def test_queue_failure_releases_the_lock(self):
        cache_obj = _Cache()
        task = MagicMock()
        task.delay.side_effect = RuntimeError('broker down')
        resp, _ = self._generate(_cfg(), cache_obj, task=task)
        self.assertEqual((resp.status_code, resp.data['code']), (503, 'queue_unavailable'))
        self.assertEqual(cache_obj.store, {}, 'блокировку надо снять, иначе 409 на пять минут')

    def test_client_cannot_generate(self):
        with _settings_patch(_cfg()), patch(MP + 'cache', _Cache()):
            resp = _call(M.MarketerGenerateAPIView, 'post', '/api/v1/marketer/posts/generate/',
                         user=_user(role='client'))
        self.assertEqual((resp.status_code, resp.data['code']), (403, 'role_not_allowed'))


# ── публикация и отклонение ──────────────────────────────────────────────────

class PublishTest(SimpleTestCase):

    def _publish(self, post, cfg=None, publish_result=True, data=None):
        cfg = cfg or _cfg()
        objects_patch, _ = _posts_patch(post)
        publisher = MagicMock(return_value=publish_result)
        with objects_patch, _settings_patch(cfg), patch(MP + '_group_id', return_value=123), \
             patch('apps.tenant.marketer.publisher.publish_post', publisher):
            resp = _call(M.MarketerPostPublishAPIView, 'post',
                         '/api/v1/marketer/posts/7/publish/',
                         data=data if data is not None else {'confirm': True}, pk=7)
        return resp, publisher

    def test_confirm_required(self):
        resp, publisher = self._publish(_post(), data={})
        self.assertEqual((resp.status_code, resp.data['code']), (400, 'confirm_required'))
        publisher.assert_not_called()

    def test_already_published_is_idempotent(self):
        post = _post(status=MarketerPostStatus.PUBLISHED, vk_post_id='456')
        resp, publisher = self._publish(post, data={})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['already_published'])
        publisher.assert_not_called()

    def test_rejected_is_not_publishable(self):
        resp, publisher = self._publish(_post(status=MarketerPostStatus.REJECTED))
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'not_publishable'))
        publisher.assert_not_called()

    def test_empty_text_is_not_publishable(self):
        resp, publisher = self._publish(_post(text='   '))
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'not_publishable'))
        publisher.assert_not_called()

    def test_disabled_marketer_does_not_mark_post_failed(self):
        post = _post()
        resp, publisher = self._publish(post, cfg=_cfg(is_enabled=False))
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'marketer_disabled'))
        publisher.assert_not_called()
        self.assertEqual(post.status, MarketerPostStatus.DRAFT, 'статус не портим настройкой')

    def test_missing_token_is_409(self):
        resp, publisher = self._publish(_post(), cfg=_cfg(vk_wall_token=''))
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'no_vk_token'))
        publisher.assert_not_called()

    def test_vk_error_is_502_with_vk_text(self):
        post = _post()

        def _fail(p):
            p.status = MarketerPostStatus.FAILED
            p.error = 'VK error 214: Access denied'
            return False

        objects_patch, _ = _posts_patch(post)
        with objects_patch, _settings_patch(_cfg()), patch(MP + '_group_id', return_value=123), \
             patch('apps.tenant.marketer.publisher.publish_post', side_effect=_fail):
            resp = _call(M.MarketerPostPublishAPIView, 'post',
                         '/api/v1/marketer/posts/7/publish/', data={'confirm': True}, pk=7)
        self.assertEqual((resp.status_code, resp.data['code']), (502, 'vk_error'))
        self.assertEqual(resp.data['detail'], 'VK error 214: Access denied')
        self.assertEqual(resp.data['post']['status'], MarketerPostStatus.FAILED)

    def test_failed_post_can_be_published_again(self):
        post = _post(status=MarketerPostStatus.FAILED, error='VK error 6: Too many requests')

        def _ok(p):
            p.status = MarketerPostStatus.PUBLISHED
            p.vk_post_id = '789'
            p.error = ''
            return True

        objects_patch, _ = _posts_patch(post)
        with objects_patch, _settings_patch(_cfg()), patch(MP + '_group_id', return_value=123), \
             patch('apps.tenant.marketer.publisher.publish_post', side_effect=_ok):
            resp = _call(M.MarketerPostPublishAPIView, 'post',
                         '/api/v1/marketer/posts/7/publish/', data={'confirm': True}, pk=7)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['status'], MarketerPostStatus.PUBLISHED)
        self.assertEqual(resp.data['vk_post_url'], 'https://vk.com/wall-123_789')

    def test_client_cannot_publish(self):
        objects_patch, _ = _posts_patch(_post())
        with objects_patch, _settings_patch(_cfg()):
            resp = _call(M.MarketerPostPublishAPIView, 'post',
                         '/api/v1/marketer/posts/7/publish/', data={'confirm': True},
                         user=_user(role='client'), pk=7)
        self.assertEqual((resp.status_code, resp.data['code']), (403, 'role_not_allowed'))


class RejectTest(SimpleTestCase):

    def _reject(self, post):
        objects_patch, _ = _posts_patch(post)
        with objects_patch, _settings_patch(_cfg()), patch(MP + '_group_id', return_value=123):
            return _call(M.MarketerPostRejectAPIView, 'post',
                         '/api/v1/marketer/posts/7/reject/', pk=7)

    def test_draft_becomes_rejected(self):
        post = _post()
        resp = self._reject(post)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(post.status, MarketerPostStatus.REJECTED)
        post.save.assert_called_once_with(update_fields=['status', 'updated_at'])

    def test_published_cannot_be_rejected(self):
        post = _post(status=MarketerPostStatus.PUBLISHED)
        resp = self._reject(post)
        self.assertEqual((resp.status_code, resp.data['code']), (409, 'not_rejectable'))
        post.save.assert_not_called()

    def test_already_rejected_is_idempotent(self):
        post = _post(status=MarketerPostStatus.REJECTED)
        resp = self._reject(post)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['already_rejected'])
        post.save.assert_not_called()


class AuthorLabelTest(SimpleTestCase):

    def test_full_name_wins_over_username(self):
        self.assertEqual(M._author(_user()), 'Алина Петрова')

    def test_username_when_no_full_name(self):
        self.assertEqual(M._author(_user(full_name='')), 'alina')

    def test_broken_user_does_not_crash(self):
        user = SimpleNamespace(username='u', get_full_name=lambda: 1 / 0)
        self.assertEqual(M._author(user), 'u')
