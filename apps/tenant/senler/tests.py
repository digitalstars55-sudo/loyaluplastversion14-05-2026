"""
Tests for senler auto-broadcast tasks and VK API services.

Patch strategy
──────────────
`get_tenant_model`, `schema_context`, `ClientBranch`, `ClientAttempt`,
`AutoBroadcastTemplate`, `AutoBroadcastLog`, `BroadcastSend`,
`send_vk_message`, `upload_vk_photo` are imported *inside* task function
bodies (lazy imports).  patch() must target the original module where the
name lives, not `apps.tenant.senler.tasks.*`.

`time` and `timezone` are module-level imports in tasks.py, so they CAN be
patched via `apps.tenant.senler.tasks.time / .timezone`.
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase


# ── Shared helpers ────────────────────────────────────────────────────────────

def _qs(items):
    """
    Queryset-like mock supporting:
      for x in qs          →  iteration (fresh iterator each time)
      qs.exists()          →  bool
      qs.values_list(...)  →  list (used by AutoBroadcastLog dedup)
    """
    m = MagicMock()
    m.__iter__ = MagicMock(side_effect=lambda: iter(items))
    m.exists.return_value = bool(items)
    m.values_list.return_value = items
    return m


def _tenant(schema='tenant_a'):
    t = MagicMock()
    t.schema_name = schema
    return t


def _schema_ctx():
    ctx = MagicMock()
    ctx.__enter__ = lambda s: s
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _cb(vk_id=11111, cfg_pk=1, cfg_active=True):
    """Mock ClientBranch with a vk_id and senler_config."""
    cb = MagicMock()
    cb.client.vk_id = vk_id
    cb.branch.senler_config.pk = cfg_pk
    cb.branch.senler_config.is_active = cfg_active
    return cb


# ── VK API service tests ───────────────────────────────────────────────────────

class TestVkCall(TestCase):
    """_vk_call wraps requests.post, calls raise_for_status, returns JSON."""

    def test_returns_parsed_json_on_success(self):
        from apps.tenant.senler.services import _vk_call

        mock_resp = MagicMock()
        mock_resp.json.return_value = {'response': 123}

        with patch('requests.post', return_value=mock_resp):
            result = _vk_call('messages.send', {'v': '5.131'})

        self.assertEqual(result, {'response': 123})
        mock_resp.raise_for_status.assert_called_once()

    def test_raises_on_http_error(self):
        """raise_for_status propagates — callers must handle it."""
        from apps.tenant.senler.services import _vk_call
        import requests as req_lib

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req_lib.HTTPError('503')

        with patch('requests.post', return_value=mock_resp):
            with self.assertRaises(req_lib.HTTPError):
                _vk_call('messages.send', {})

    def test_passes_correct_method_in_url(self):
        from apps.tenant.senler.services import _vk_call

        mock_resp = MagicMock()
        mock_resp.json.return_value = {}

        with patch('requests.post', return_value=mock_resp) as mock_post:
            _vk_call('photos.saveMessagesPhoto', {'server': 1})

        url = mock_post.call_args[0][0]
        self.assertIn('photos.saveMessagesPhoto', url)


class TestSendVkMessage(TestCase):
    """send_vk_message returns (True, '', msg_id) on success or (False, error, None) on failure."""

    def _cfg(self):
        c = MagicMock()
        c.vk_community_token = 'tok123'
        return c

    def test_success_returns_true(self):
        from apps.tenant.senler.services import send_vk_message

        mock_resp = MagicMock()
        mock_resp.json.return_value = {'response': 12345}

        with patch('requests.post', return_value=mock_resp):
            ok, err, _msg_id = send_vk_message(self._cfg(), 99999, 'Hello')

        self.assertTrue(ok)
        self.assertEqual(err, '')
        self.assertEqual(_msg_id, 12345)

    def test_vk_api_error_returns_false(self):
        from apps.tenant.senler.services import send_vk_message

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'error': {'error_code': 7, 'error_msg': 'Permission denied'}
        }

        with patch('requests.post', return_value=mock_resp):
            ok, err, _msg_id = send_vk_message(self._cfg(), 99999, 'Hello')

        self.assertFalse(ok)
        self.assertEqual(err, 'Permission denied')

    def test_network_exception_returns_false(self):
        from apps.tenant.senler.services import send_vk_message

        with patch('requests.post', side_effect=ConnectionError('timeout')):
            ok, err, _msg_id = send_vk_message(self._cfg(), 99999, 'Hello')

        self.assertFalse(ok)
        self.assertIn('timeout', err)

    def test_attachment_included_when_provided(self):
        from apps.tenant.senler.services import send_vk_message

        mock_resp = MagicMock()
        mock_resp.json.return_value = {'response': 1}

        with patch('requests.post', return_value=mock_resp) as mock_post:
            send_vk_message(self._cfg(), 99999, 'Hi', attachment='photo-1_2')

        payload = mock_post.call_args.kwargs['data']
        self.assertEqual(payload['attachment'], 'photo-1_2')

    def test_no_attachment_key_when_none(self):
        from apps.tenant.senler.services import send_vk_message

        mock_resp = MagicMock()
        mock_resp.json.return_value = {'response': 1}

        with patch('requests.post', return_value=mock_resp) as mock_post:
            send_vk_message(self._cfg(), 99999, 'Hi', attachment=None)

        payload = mock_post.call_args.kwargs['data']
        self.assertNotIn('attachment', payload)


class TestUploadVkPhoto(TestCase):
    """upload_vk_photo follows the 3-step VK photo upload protocol."""

    def _cfg(self):
        c = MagicMock()
        c.vk_community_token = 'tok'
        return c

    def _image(self):
        img = MagicMock()
        img.name = 'path/img.jpg'
        img.open.return_value.__enter__ = lambda s: s
        img.open.return_value.__exit__ = MagicMock(return_value=False)
        return img

    def test_success_returns_attachment_string(self):
        from apps.tenant.senler.services import upload_vk_photo

        upload_server_resp = {'response': {'upload_url': 'https://upload.vk.com/x'}}
        save_resp = {'response': [{'owner_id': -123, 'id': 456}]}
        upload_file_resp = MagicMock()
        upload_file_resp.json.return_value = {'server': 1, 'photo': 'data', 'hash': 'abc'}

        with patch('apps.tenant.senler.services._vk_call',
                   side_effect=[upload_server_resp, save_resp]), \
             patch('requests.post', return_value=upload_file_resp):
            att, err = upload_vk_photo(self._cfg(), self._image())

        self.assertEqual(att, 'photo-123_456')
        self.assertEqual(err, '')

    def test_step1_vk_error_returns_none(self):
        from apps.tenant.senler.services import upload_vk_photo

        with patch('apps.tenant.senler.services._vk_call',
                   return_value={'error': {'error_msg': 'Access denied'}}):
            att, err = upload_vk_photo(self._cfg(), self._image())

        self.assertIsNone(att)
        self.assertIn('Access denied', err)

    def test_step1_network_exception_returns_none(self):
        from apps.tenant.senler.services import upload_vk_photo

        with patch('apps.tenant.senler.services._vk_call',
                   side_effect=ConnectionError('network error')):
            att, err = upload_vk_photo(self._cfg(), self._image())

        self.assertIsNone(att)
        self.assertIn('network error', err)

    def test_step3_vk_error_returns_none(self):
        from apps.tenant.senler.services import upload_vk_photo

        upload_server_resp = {'response': {'upload_url': 'https://upload.vk.com/x'}}
        save_error_resp = {'error': {'error_msg': 'Save failed'}}
        upload_file_resp = MagicMock()
        upload_file_resp.json.return_value = {'server': 1, 'photo': 'data', 'hash': 'abc'}

        with patch('apps.tenant.senler.services._vk_call',
                   side_effect=[upload_server_resp, save_error_resp]), \
             patch('requests.post', return_value=upload_file_resp):
            att, err = upload_vk_photo(self._cfg(), self._image())

        self.assertIsNone(att)
        self.assertIn('Save failed', err)


# ── Birthday broadcast task tests ─────────────────────────────────────────────

class SendBirthdayBroadcastsTaskTest(TestCase):
    """send_birthday_broadcasts_task sends birthday VK messages."""

    # Patch paths — lazy imports live in their original modules
    _GTM    = 'django_tenants.utils.get_tenant_model'
    _CTX    = 'django_tenants.utils.schema_context'
    _TPL    = 'apps.tenant.senler.models.AutoBroadcastTemplate'
    _CB     = 'apps.tenant.branch.models.ClientBranch'
    _LOG    = 'apps.tenant.senler.models.AutoBroadcastLog'
    _BS     = 'apps.tenant.senler.models.BroadcastSend'
    _SEND   = 'apps.tenant.senler.services.send_vk_message'
    _UPLOAD = 'apps.tenant.senler.services.upload_vk_photo'
    _TIME   = 'apps.tenant.senler.tasks.time'

    def _run(self, tenants, tpl_list, candidates_items,
             already_sent_ids=None, send_result=(True, '', 12345),
             upload_result=('photo1_2', '')):
        """
        Runs send_birthday_broadcasts_task with full mocking.
        Returns (result, MockLog, MockBS, mock_send, mock_upload, mock_time).
        """
        from apps.tenant.senler.tasks import send_birthday_broadcasts_task

        with patch(self._GTM) as mock_gtm, \
             patch(self._CTX, return_value=_schema_ctx()), \
             patch(self._TPL) as MockTpl, \
             patch(self._CB) as MockCB, \
             patch(self._LOG) as MockLog, \
             patch(self._BS) as MockBS, \
             patch(self._SEND, return_value=send_result) as mock_send, \
             patch(self._UPLOAD, return_value=upload_result) as mock_upload, \
             patch(self._TIME) as mock_time:

            mock_gtm.return_value.objects.exclude.return_value = tenants
            MockTpl.objects.filter.return_value = tpl_list
            MockCB.objects.filter.return_value.select_related.return_value = (
                _qs(candidates_items)
            )
            MockLog.objects.filter.return_value.values_list.return_value = (
                already_sent_ids or []
            )

            result = send_birthday_broadcasts_task()
            return result, MockLog, MockBS, mock_send, mock_upload, mock_time

    def test_returns_zero_when_no_tenants(self):
        result, *_ = self._run(tenants=[], tpl_list=[], candidates_items=[])
        self.assertEqual(result['sent'], 0)

    def test_returns_zero_when_no_active_templates(self):
        result, *_ = self._run(
            tenants=[_tenant()], tpl_list=[], candidates_items=[]
        )
        self.assertEqual(result['sent'], 0)

    def test_sends_message_and_creates_log_on_success(self):
        from apps.tenant.senler.models import AutoBroadcastType

        template = MagicMock()
        template.type = AutoBroadcastType.BIRTHDAY
        template.message_text = 'С ДР!'
        template.image = None

        result, MockLog, _, mock_send, _, _ = self._run(
            tenants=[_tenant()],
            tpl_list=[template],
            candidates_items=[_cb(vk_id=99999)],
        )

        self.assertEqual(result['sent'], 1)
        mock_send.assert_called_once()
        MockLog.objects.create.assert_called_once_with(
            trigger_type=AutoBroadcastType.BIRTHDAY, vk_id=99999
        )

    def test_skips_already_sent_this_year(self):
        from apps.tenant.senler.models import AutoBroadcastType

        template = MagicMock()
        template.type = AutoBroadcastType.BIRTHDAY
        template.image = None

        result, MockLog, _, mock_send, _, _ = self._run(
            tenants=[_tenant()],
            tpl_list=[template],
            candidates_items=[_cb(vk_id=99999)],
            already_sent_ids=[99999],
        )

        mock_send.assert_not_called()
        MockLog.objects.create.assert_not_called()
        self.assertEqual(result['sent'], 0)

    def test_skips_when_no_candidates(self):
        """BroadcastSend must NOT be created when candidates queryset is empty."""
        from apps.tenant.senler.models import AutoBroadcastType

        template = MagicMock()
        template.type = AutoBroadcastType.BIRTHDAY
        template.image = None

        _, _, MockBS, mock_send, _, _ = self._run(
            tenants=[_tenant()],
            tpl_list=[template],
            candidates_items=[],
        )

        MockBS.objects.create.assert_not_called()
        mock_send.assert_not_called()

    def test_skips_inactive_senler_config(self):
        from apps.tenant.senler.models import AutoBroadcastType

        template = MagicMock()
        template.type = AutoBroadcastType.BIRTHDAY
        template.image = None

        result, _, _, mock_send, _, _ = self._run(
            tenants=[_tenant()],
            tpl_list=[template],
            candidates_items=[_cb(vk_id=11111, cfg_active=False)],
        )

        mock_send.assert_not_called()
        self.assertEqual(result['sent'], 0)

    def test_failed_send_increments_failed_count(self):
        from apps.tenant.senler.models import AutoBroadcastType

        template = MagicMock()
        template.type = AutoBroadcastType.BIRTHDAY
        template.image = None
        bs_mock = MagicMock()

        with patch(self._GTM) as mock_gtm, \
             patch(self._CTX, return_value=_schema_ctx()), \
             patch(self._TPL) as MockTpl, \
             patch(self._CB) as MockCB, \
             patch(self._LOG) as MockLog, \
             patch(self._BS) as MockBS, \
             patch(self._BR), \
             patch(self._SEND, return_value=(False, 'VK error', None)), \
             patch(self._TIME):

            mock_gtm.return_value.objects.exclude.return_value = [_tenant()]
            MockTpl.objects.filter.return_value = [template]
            MockCB.objects.filter.return_value.select_related.return_value = _qs([_cb()])
            MockLog.objects.filter.return_value.values_list.return_value = []
            MockBS.objects.create.return_value = bs_mock

            from apps.tenant.senler.tasks import send_birthday_broadcasts_task
            send_birthday_broadcasts_task()

        self.assertEqual(bs_mock.failed_count, 1)
        self.assertEqual(bs_mock.sent_count, 0)

    def test_image_uploaded_once_for_same_community(self):
        from apps.tenant.senler.models import AutoBroadcastType

        template = MagicMock()
        template.type = AutoBroadcastType.BIRTHDAY
        template.message_text = 'Hey!'
        template.image = MagicMock()

        _, _, _, _, mock_upload, _ = self._run(
            tenants=[_tenant()],
            tpl_list=[template],
            candidates_items=[_cb(vk_id=11111, cfg_pk=10), _cb(vk_id=22222, cfg_pk=10)],
        )
        self.assertEqual(mock_upload.call_count, 1)

    def test_image_uploaded_per_unique_community(self):
        from apps.tenant.senler.models import AutoBroadcastType

        template = MagicMock()
        template.type = AutoBroadcastType.BIRTHDAY
        template.message_text = 'Hey!'
        template.image = MagicMock()

        _, _, _, _, mock_upload, _ = self._run(
            tenants=[_tenant()],
            tpl_list=[template],
            candidates_items=[_cb(vk_id=11111, cfg_pk=10), _cb(vk_id=22222, cfg_pk=20)],
        )
        self.assertEqual(mock_upload.call_count, 2)

    def test_rate_limit_sleep_called_per_recipient(self):
        from apps.tenant.senler.models import AutoBroadcastType

        template = MagicMock()
        template.type = AutoBroadcastType.BIRTHDAY
        template.image = None

        _, _, _, _, _, mock_time = self._run(
            tenants=[_tenant()],
            tpl_list=[template],
            candidates_items=[_cb(vk_id=11111), _cb(vk_id=22222)],
        )

        mock_time.sleep.assert_called_with(0.05)
        self.assertEqual(mock_time.sleep.call_count, 2)

    def test_tenant_exception_does_not_abort_other_tenants(self):
        """RuntimeError in tenant A is caught; tenant B is still processed."""
        from apps.tenant.senler.tasks import send_birthday_broadcasts_task

        call_order = []

        def schema_ctx_side_effect(schema_name):
            call_order.append(schema_name)
            if schema_name == 'bad':
                raise RuntimeError('DB exploded')
            return _schema_ctx()

        with patch(self._GTM) as mock_gtm, \
             patch(self._CTX, side_effect=schema_ctx_side_effect), \
             patch(self._TPL) as MockTpl:

            mock_gtm.return_value.objects.exclude.return_value = [
                _tenant('bad'), _tenant('good'),
            ]
            MockTpl.objects.filter.return_value = []
            result = send_birthday_broadcasts_task()

        self.assertIn('sent', result)
        self.assertIn('good', call_order)

    def test_multiple_triggers_sent_in_one_run(self):
        """All three birthday triggers are processed for a single tenant."""
        from apps.tenant.senler.tasks import send_birthday_broadcasts_task
        from apps.tenant.senler.models import AutoBroadcastType

        templates = [
            MagicMock(type=AutoBroadcastType.BIRTHDAY_7_DAYS, image=None, message_text='7d'),
            MagicMock(type=AutoBroadcastType.BIRTHDAY_1_DAY,  image=None, message_text='1d'),
            MagicMock(type=AutoBroadcastType.BIRTHDAY,        image=None, message_text='today'),
        ]

        with patch(self._GTM) as mock_gtm, \
             patch(self._CTX, return_value=_schema_ctx()), \
             patch(self._TPL) as MockTpl, \
             patch(self._CB) as MockCB, \
             patch(self._LOG) as MockLog, \
             patch(self._BS), \
             patch(self._SEND, return_value=(True, '', 12345)) as mock_send, \
             patch(self._TIME):

            mock_gtm.return_value.objects.exclude.return_value = [_tenant()]
            MockTpl.objects.filter.return_value = templates
            MockCB.objects.filter.return_value.select_related.return_value = _qs([_cb(vk_id=1)])
            MockLog.objects.filter.return_value.values_list.return_value = []

            result = send_birthday_broadcasts_task()

        self.assertEqual(result['sent'], 3)
        self.assertEqual(mock_send.call_count, 3)


# ── After-game broadcast task tests ───────────────────────────────────────────

class SendAfterGameBroadcastTaskTest(TestCase):
    """send_after_game_broadcast_task respects 09:00–21:00 send window."""

    _GTM     = 'django_tenants.utils.get_tenant_model'
    _CTX     = 'django_tenants.utils.schema_context'
    _TPL     = 'apps.tenant.senler.models.AutoBroadcastTemplate'
    _ATTEMPT = 'apps.tenant.game.models.ClientAttempt'
    _LOG     = 'apps.tenant.senler.models.AutoBroadcastLog'
    _BS      = 'apps.tenant.senler.models.BroadcastSend'
    _SEND    = 'apps.tenant.senler.services.send_vk_message'
    _UPLOAD  = 'apps.tenant.senler.services.upload_vk_photo'
    _TZ      = 'apps.tenant.senler.tasks.timezone'
    _TIME    = 'apps.tenant.senler.tasks.time'

    def _mock_tz(self, hour=14):
        from datetime import date as _date
        m = MagicMock()
        local = MagicMock()
        local.hour = hour
        local.date.return_value = _date(2024, 6, 15)  # real date for evening-mode localize()
        # Set astimezone AFTER accessing now.return_value so we don't override it
        m.now.return_value.astimezone.return_value = local
        return m

    def _run_normal(self, tenants, attempts_items,
                    already_sent_ids=None, send_result=(True, '', 12345),
                    upload_result=('photo_x', ''), hour=14,
                    template_image=None):
        """Runs after-game task in normal (non-evening) mode with full mocking."""
        from apps.tenant.senler.tasks import send_after_game_broadcast_task

        with patch(self._GTM) as mock_gtm, \
             patch(self._CTX, return_value=_schema_ctx()), \
             patch(self._TZ, self._mock_tz(hour)), \
             patch(self._TPL) as MockTpl, \
             patch(self._ATTEMPT) as MockAttempt, \
             patch(self._LOG) as MockLog, \
             patch(self._BS) as MockBS, \
             patch(self._SEND, return_value=send_result) as mock_send, \
             patch(self._UPLOAD, return_value=upload_result) as mock_upload, \
             patch(self._TIME) as mock_time:

            mock_gtm.return_value.objects.exclude.return_value = tenants
            MockTpl.objects.get.return_value = MagicMock(
                image=template_image, message_text='Hey!'
            )
            MockAttempt.objects.filter.return_value \
                .select_related.return_value \
                .distinct.return_value = _qs(attempts_items)
            MockLog.objects.filter.return_value.values_list.return_value = (
                already_sent_ids or []
            )

            result = send_after_game_broadcast_task(process_evening=False)
            return result, MockLog, MockBS, mock_send, mock_upload, mock_time

    # ── Window enforcement ────────────────────────────────────────────────────

    def test_skips_outside_window_at_night(self):
        from apps.tenant.senler.tasks import send_after_game_broadcast_task
        with patch(self._TZ, self._mock_tz(23)):
            result = send_after_game_broadcast_task(process_evening=False)
        self.assertEqual(result['reason'], 'outside_send_window')

    def test_skips_outside_window_before_morning(self):
        from apps.tenant.senler.tasks import send_after_game_broadcast_task
        with patch(self._TZ, self._mock_tz(7)):
            result = send_after_game_broadcast_task(process_evening=False)
        self.assertEqual(result['reason'], 'outside_send_window')

    def test_skips_at_boundary_hour_21(self):
        """Hour 21 is outside window (condition is 9 ≤ hour < 21)."""
        from apps.tenant.senler.tasks import send_after_game_broadcast_task
        with patch(self._TZ, self._mock_tz(21)):
            result = send_after_game_broadcast_task(process_evening=False)
        self.assertEqual(result['reason'], 'outside_send_window')

    def test_processes_at_boundary_hour_9(self):
        from apps.tenant.senler.tasks import send_after_game_broadcast_task
        with patch(self._GTM) as mock_gtm, \
             patch(self._TZ, self._mock_tz(9)):
            mock_gtm.return_value.objects.exclude.return_value = []
            result = send_after_game_broadcast_task(process_evening=False)
        self.assertNotIn('reason', result)

    def test_processes_within_window(self):
        from apps.tenant.senler.tasks import send_after_game_broadcast_task
        with patch(self._GTM) as mock_gtm, \
             patch(self._TZ, self._mock_tz(14)):
            mock_gtm.return_value.objects.exclude.return_value = []
            result = send_after_game_broadcast_task(process_evening=False)
        self.assertNotIn('reason', result)

    def test_evening_mode_bypasses_window_check(self):
        from apps.tenant.senler.tasks import send_after_game_broadcast_task
        with patch(self._GTM) as mock_gtm, \
             patch(self._TZ, self._mock_tz(23)):
            mock_gtm.return_value.objects.exclude.return_value = []
            result = send_after_game_broadcast_task(process_evening=True)
        self.assertNotIn('reason', result)

    # ── Core sending logic ────────────────────────────────────────────────────

    def test_sends_message_and_creates_log(self):
        from apps.tenant.senler.models import AutoBroadcastType

        cb = _cb(vk_id=55555)
        attempt = MagicMock(); attempt.client = cb

        result, MockLog, _, mock_send, _, _ = self._run_normal(
            tenants=[_tenant()], attempts_items=[attempt]
        )

        self.assertEqual(result['sent'], 1)
        mock_send.assert_called_once()
        MockLog.objects.create.assert_called_once_with(
            trigger_type=AutoBroadcastType.AFTER_GAME_3H, vk_id=55555
        )

    def test_skips_already_sent_today(self):
        cb = _cb(vk_id=55555)
        attempt = MagicMock(); attempt.client = cb

        result, MockLog, _, mock_send, _, _ = self._run_normal(
            tenants=[_tenant()],
            attempts_items=[attempt],
            already_sent_ids=[55555],
        )

        mock_send.assert_not_called()
        MockLog.objects.create.assert_not_called()
        self.assertEqual(result['sent'], 0)

    def test_no_attempts_skips_broadcast_send_creation(self):
        """BroadcastSend must NOT be created when attempts queryset is empty."""
        _, _, MockBS, mock_send, _, _ = self._run_normal(
            tenants=[_tenant()], attempts_items=[]
        )

        MockBS.objects.create.assert_not_called()
        mock_send.assert_not_called()

    def test_skips_inactive_senler_config(self):
        cb = _cb(vk_id=77777, cfg_active=False)
        attempt = MagicMock(); attempt.client = cb

        result, _, _, mock_send, _, _ = self._run_normal(
            tenants=[_tenant()], attempts_items=[attempt]
        )

        mock_send.assert_not_called()
        self.assertEqual(result['sent'], 0)

    def test_no_template_skips_tenant(self):
        from apps.tenant.senler.tasks import send_after_game_broadcast_task

        with patch(self._GTM) as mock_gtm, \
             patch(self._CTX, return_value=_schema_ctx()), \
             patch(self._TZ, self._mock_tz(14)), \
             patch(self._TPL) as MockTpl, \
             patch(self._ATTEMPT) as MockAttempt, \
             patch(self._TIME):

            mock_gtm.return_value.objects.exclude.return_value = [_tenant()]
            MockTpl.objects.get.side_effect = MockTpl.DoesNotExist

            result = send_after_game_broadcast_task(process_evening=False)

        MockAttempt.objects.filter.assert_not_called()
        self.assertEqual(result['sent'], 0)

    # ── Image / attachment caching ────────────────────────────────────────────

    def test_image_uploaded_per_unique_community(self):
        """Two guests from different communities → 2 uploads."""
        cb1 = _cb(vk_id=11111, cfg_pk=10)
        cb2 = _cb(vk_id=22222, cfg_pk=20)
        attempt1 = MagicMock(); attempt1.client = cb1
        attempt2 = MagicMock(); attempt2.client = cb2

        _, _, _, _, mock_upload, _ = self._run_normal(
            tenants=[_tenant()],
            attempts_items=[attempt1, attempt2],
            template_image=MagicMock(),
        )
        self.assertEqual(mock_upload.call_count, 2)

    def test_image_uploaded_once_for_same_community(self):
        """Two guests from the same community → 1 upload."""
        cb1 = _cb(vk_id=11111, cfg_pk=10)
        cb2 = _cb(vk_id=22222, cfg_pk=10)
        attempt1 = MagicMock(); attempt1.client = cb1
        attempt2 = MagicMock(); attempt2.client = cb2

        _, _, _, _, mock_upload, _ = self._run_normal(
            tenants=[_tenant()],
            attempts_items=[attempt1, attempt2],
            template_image=MagicMock(),
        )
        self.assertEqual(mock_upload.call_count, 1)

    def test_no_image_upload_when_template_has_no_image(self):
        cb = _cb(vk_id=33333)
        attempt = MagicMock(); attempt.client = cb

        _, _, _, _, mock_upload, _ = self._run_normal(
            tenants=[_tenant()],
            attempts_items=[attempt],
            template_image=None,
        )
        mock_upload.assert_not_called()

    # ── Rate limiting ─────────────────────────────────────────────────────────

    def test_rate_limit_sleep_called_per_recipient(self):
        cb1 = _cb(vk_id=11111)
        cb2 = _cb(vk_id=22222)
        attempt1 = MagicMock(); attempt1.client = cb1
        attempt2 = MagicMock(); attempt2.client = cb2

        _, _, _, _, _, mock_time = self._run_normal(
            tenants=[_tenant()],
            attempts_items=[attempt1, attempt2],
        )

        mock_time.sleep.assert_called_with(0.05)
        self.assertEqual(mock_time.sleep.call_count, 2)

    def test_rate_limit_sleep_called_even_on_failed_send(self):
        """Sleep happens even when VK returns error — always throttle."""
        cb = _cb(vk_id=99999)
        attempt = MagicMock(); attempt.client = cb

        _, _, _, _, _, mock_time = self._run_normal(
            tenants=[_tenant()],
            attempts_items=[attempt],
            send_result=(False, 'error', None),
        )

        mock_time.sleep.assert_called_once_with(0.05)
