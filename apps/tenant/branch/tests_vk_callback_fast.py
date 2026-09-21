"""
Быстрый путь VK Callback API (инцидент 21.09.2026). Без БД, на моках.

Главное, что проверяем, — условие приёмки: ручка отвечает 200 ВСЕГДА, кроме
двух осознанных случаев (строка подтверждения и 403 на чужой секрет). ВК
отключает callback-сервер и за 5xx тоже, поэтому «упали внутри» обязано
выглядеть снаружи как «ok».
"""

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory

from apps.tenant.branch.api import vk_callback_fast as fast
from apps.tenant.branch.api.services import VKCallbackConfirmation
from apps.tenant.branch.api.views import VKCallbackView

GROUP = 211202938
SECRET = 's3cr3t'

# Дедуп обязан быть общим для процессов, на проде это Redis. В тестах хватает
# локальной памяти: проверяем логику, а не транспорт.
LOCMEM = {
    'default':     {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'd'},
    'vk_callback': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'vkcb-tests'},
}

STATE_OK = {'state': 'hashes', 'h': [fast._sha(SECRET)]}


def _msg(msg_id=101, text='привет'):
    return {
        'type': 'message_new',
        'group_id': GROUP,
        'secret': SECRET,
        'object': {'message': {'id': msg_id, 'from_id': 77, 'peer_id': 77, 'text': text, 'date': 1}},
    }


@override_settings(CACHES=LOCMEM, VK_CALLBACK_ASYNC=True)
class FastPathViewTest(SimpleTestCase):
    """Поведение ручки целиком."""

    def setUp(self):
        from django.core.cache import caches
        caches['vk_callback'].clear()
        self.factory = APIRequestFactory()
        self.view = VKCallbackView.as_view()

    def _post(self, body):
        return self.view(self.factory.post('/api/v1/vk/callback/', body, format='json'))

    # ── два осознанных исключения из «всегда 200» ────────────────────────────

    def test_confirmation_stays_synchronous(self):
        """ВК ждёт код подтверждения в теле ответа — из очереди его не отдать."""
        with patch('apps.tenant.branch.api.views.handle_vk_callback',
                   side_effect=VKCallbackConfirmation('abc123')) as h, \
             patch.object(fast, 'enqueue') as enq:
            r = self._post({'type': 'confirmation', 'group_id': GROUP})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b'abc123')
        h.assert_called_once()
        enq.assert_not_called()

    def test_wrong_secret_is_403_and_never_queued(self):
        with patch.object(fast, '_load_secret_state', return_value=STATE_OK), \
             patch.object(fast, 'enqueue') as enq:
            body = _msg()
            body['secret'] = 'подделка'
            r = self._post(body)
        self.assertEqual(r.status_code, 403)
        enq.assert_not_called()

    # ── всё остальное — 200 ──────────────────────────────────────────────────

    def test_message_new_is_queued_and_answered_ok(self):
        with patch.object(fast, '_load_secret_state', return_value=STATE_OK), \
             patch.object(fast, 'enqueue', return_value=True) as enq:
            r = self._post(_msg())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, 'ok')
        enq.assert_called_once()

    def test_repeat_of_same_event_is_not_queued_twice(self):
        """Ретраи ВК: 21.09 их было 53 415 на одни и те же события."""
        with patch.object(fast, '_load_secret_state', return_value=STATE_OK), \
             patch.object(fast, 'enqueue', return_value=True) as enq:
            first  = self._post(_msg(msg_id=555))
            second = self._post(_msg(msg_id=555))
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(enq.call_count, 1)

    def test_different_messages_are_both_queued(self):
        with patch.object(fast, '_load_secret_state', return_value=STATE_OK), \
             patch.object(fast, 'enqueue', return_value=True) as enq:
            self._post(_msg(msg_id=1))
            self._post(_msg(msg_id=2))
        self.assertEqual(enq.call_count, 2)

    def test_broker_down_still_answers_ok(self):
        """Не легло в очередь — пишем в лог, но ВК получает 200, иначе отключит."""
        with patch.object(fast, '_load_secret_state', return_value=STATE_OK), \
             patch.object(fast, 'enqueue', return_value=False):
            r = self._post(_msg())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, 'ok')

    def test_unexpected_error_still_answers_ok(self):
        with patch.object(fast, 'check_secret', side_effect=RuntimeError('база легла')):
            r = self._post(_msg())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, 'ok')

    def test_unknown_group_is_ok_without_queue(self):
        with patch.object(fast, '_load_secret_state', return_value={'state': 'none'}), \
             patch.object(fast, 'enqueue') as enq:
            r = self._post(_msg())
        self.assertEqual(r.status_code, 200)
        enq.assert_not_called()

    def test_message_event_handled_in_request_not_queued(self):
        """Кнопка «поделиться номером» (№78): ВК ждёт быстрый ответ."""
        body = {'type': 'message_event', 'group_id': GROUP, 'secret': SECRET,
                'object': {'event_id': 'ev-1', 'user_id': 5, 'peer_id': 5, 'payload': {}}}
        with patch.object(fast, '_load_secret_state', return_value=STATE_OK), \
             patch('apps.tenant.branch.api.views.handle_vk_callback') as h, \
             patch.object(fast, 'enqueue') as enq:
            r = self._post(body)
        self.assertEqual(r.status_code, 200)
        h.assert_called_once()
        enq.assert_not_called()

    def test_body_without_group_is_ignored(self):
        with patch.object(fast, 'enqueue') as enq:
            r = self._post({'type': 'message_new'})
        self.assertEqual(r.status_code, 200)
        enq.assert_not_called()

    @override_settings(VK_CALLBACK_ASYNC=False)
    def test_kill_switch_returns_to_synchronous_behaviour(self):
        with patch('apps.tenant.branch.api.views.handle_vk_callback') as h, \
             patch.object(fast, 'enqueue') as enq:
            r = self._post(_msg())
        self.assertEqual(r.status_code, 200)
        h.assert_called_once()
        enq.assert_not_called()


@override_settings(CACHES=LOCMEM)
class DedupKeyTest(SimpleTestCase):

    def setUp(self):
        from django.core.cache import caches
        caches['vk_callback'].clear()

    def test_message_key_uses_vk_message_id(self):
        key, ttl = fast.dedup_key('levone', _msg(msg_id=42))
        self.assertIn(':message_new:42', key)
        self.assertEqual(ttl, fast.TTL_MESSAGE)

    def test_event_id_wins_when_present(self):
        body = _msg()
        body['event_id'] = 'abc'
        key, _ = fast.dedup_key('levone', body)
        self.assertIn(':eid:abc', key)

    def test_nested_event_id_of_message_event_is_used(self):
        body = {'type': 'message_event', 'group_id': GROUP,
                'object': {'event_id': 'ev-9', 'user_id': 5}}
        key, _ = fast.dedup_key('levone', body)
        self.assertIn(':eid:ev-9', key)

    def test_membership_key_is_short_lived(self):
        body = {'type': 'group_join', 'group_id': GROUP, 'object': {'user_id': 7}}
        key, ttl = fast.dedup_key('levone', body)
        self.assertIn(':group_join:u7', key)
        self.assertEqual(ttl, fast.TTL_MEMBERSHIP)

    def test_schemas_do_not_share_keys(self):
        a, _ = fast.dedup_key('levone', _msg(msg_id=1))
        b, _ = fast.dedup_key('shavuha', _msg(msg_id=1))
        self.assertNotEqual(a, b)

    def test_message_without_id_is_not_deduped(self):
        body = {'type': 'message_new', 'group_id': GROUP, 'object': {'message': {'text': 'x'}}}
        self.assertEqual(fast.dedup_key('levone', body), (None, 0))

    def test_seen_before_is_true_only_on_repeat(self):
        key, ttl = fast.dedup_key('levone', _msg(msg_id=9))
        self.assertFalse(fast.seen_before(key, ttl))
        self.assertTrue(fast.seen_before(key, ttl))

    def test_dedup_failure_does_not_block_processing(self):
        """Redis лёг — обрабатываем с повторами, их отсечёт дедуп в базе."""
        from django.core.cache import caches
        with patch.object(type(caches['vk_callback']), 'add',
                          side_effect=ConnectionError('redis down')):
            self.assertFalse(fast.seen_before('vkcb:x', 10))
        # И если сам кэш-алиас развалился — тоже не мешаем приёму.
        with patch.object(fast, '_cache', side_effect=RuntimeError('cache broken')):
            self.assertFalse(fast.seen_before('vkcb:x', 10))


@override_settings(CACHES=LOCMEM)
class SecretCheckTest(SimpleTestCase):

    def setUp(self):
        from django.core.cache import caches
        caches['vk_callback'].clear()

    def test_accepts_secret_of_any_config_of_the_group(self):
        """Точки одной группы исторически имеют РАЗНЫЕ секреты (инцидент 23.06)."""
        state = {'state': 'hashes', 'h': sorted({fast._sha('a'), fast._sha('b')})}
        with patch.object(fast, '_load_secret_state', return_value=state):
            self.assertEqual(fast.check_secret('levone', GROUP, 'a'), 'ok')
            self.assertEqual(fast.check_secret('levone', GROUP, 'b'), 'ok')
            self.assertEqual(fast.check_secret('levone', GROUP, 'c'), 'forbidden')

    def test_group_without_configs(self):
        with patch.object(fast, '_load_secret_state', return_value={'state': 'none'}):
            self.assertEqual(fast.check_secret('levone', GROUP, 'x'), 'no_config')

    def test_configs_without_secret_accept_anything(self):
        with patch.object(fast, '_load_secret_state', return_value={'state': 'any'}):
            self.assertEqual(fast.check_secret('levone', GROUP, ''), 'ok')

    def test_second_check_does_not_hit_database(self):
        with patch.object(fast, '_load_secret_state', return_value=STATE_OK) as load:
            fast.check_secret('levone', GROUP, SECRET)
            fast.check_secret('levone', GROUP, SECRET)
        self.assertEqual(load.call_count, 1)

    def test_rotated_secret_is_picked_up_without_waiting_for_cache(self):
        """Промах по хешу перечитывает базу — иначе смена секрета дала бы 403."""
        with patch.object(fast, '_load_secret_state', return_value=STATE_OK):
            fast.check_secret('levone', GROUP, SECRET)
        rotated = {'state': 'hashes', 'h': [fast._sha('новый')]}
        with patch.object(fast, '_load_secret_state', return_value=rotated) as load:
            self.assertEqual(fast.check_secret('levone', GROUP, 'новый'), 'ok')
        load.assert_called_once()


class EnqueueTest(SimpleTestCase):

    def test_secret_is_stripped_before_broker(self):
        """Секрет проверен в быстром пути, в брокере ему делать нечего."""
        with patch('apps.tenant.branch.tasks.handle_vk_callback_task.apply_async') as send:
            self.assertTrue(fast.enqueue('levone', _msg()))
        payload = send.call_args[1]['kwargs']['payload']
        self.assertNotIn('secret', payload)
        self.assertEqual(payload['type'], 'message_new')

    def test_goes_to_its_own_queue(self):
        """В общую очередь нельзя: там рассылки и мониторинг на двух слотах."""
        with patch('apps.tenant.branch.tasks.handle_vk_callback_task.apply_async') as send:
            fast.enqueue('levone', _msg())
        self.assertEqual(send.call_args[1]['queue'], 'vkcb')

    def test_broker_failure_is_reported_not_raised(self):
        with patch('apps.tenant.branch.tasks.handle_vk_callback_task.apply_async',
                   side_effect=OSError('broker unreachable')):
            self.assertFalse(fast.enqueue('levone', _msg()))
