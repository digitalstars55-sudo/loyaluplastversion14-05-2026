"""
Вердикт по жалобе (relay/verdict.py): чистая валидация, цепочка проверок ручки
(RequestFactory, запись подменена) и запись с подменённой переписки —
тенантную схему в тестах не создаём.
"""
import datetime
import json
from unittest import mock

from django.test import RequestFactory, SimpleTestCase, override_settings

from .verdict import (
    ComplaintVerdictView, VerdictError, apply_verdict, parse_complaint_id, validate_payload,
)

URL = '/api/v1/internal/complaints/verdict/'
SECRET = 'relay-test-secret'


def _good(**over):
    body = {'complaint_id': '3224-app', 'status': 'resolved', 'verdict': 'Компенсация 500 баллов',
            'manager_name': 'Алина', 'resolved_at': '2026-09-16T18:00:00+03:00', 'tenant_schema': 'dev'}
    body.update(over)
    return body


class ParseComplaintIdTest(SimpleTestCase):

    def test_shapes(self):
        self.assertEqual(parse_complaint_id('3224-app'), 3224)
        self.assertEqual(parse_complaint_id('3224'), 3224)
        self.assertEqual(parse_complaint_id(' 7-2026-09-16 '), 7)
        for bad in ('', 'abc', '-1', '12-', 'x-12', None, '1' * 13, '5-' + 'a' * 30):
            with self.assertRaises(VerdictError, msg=repr(bad)) as cm:
                parse_complaint_id(bad)
            self.assertEqual(cm.exception.status, 400)


class ValidatePayloadTest(SimpleTestCase):

    def test_ok(self):
        p = validate_payload(_good())
        self.assertEqual(p['conversation_id'], 3224)
        self.assertEqual(p['status'], 'resolved')
        self.assertEqual(p['manager'], 'Алина')
        self.assertEqual(p['tenant_schema'], 'dev')
        self.assertFalse(p['force'])
        self.assertEqual(p['resolved_at'].utcoffset(), datetime.timedelta(hours=3))

    def test_status_required(self):
        for bad in ('', 'done', 'RESOLVED!'):
            with self.assertRaises(VerdictError) as cm:
                validate_payload(_good(status=bad))
            self.assertEqual(cm.exception.code, 'invalid_status')
        self.assertEqual(validate_payload(_good(status=' Rejected '))['status'], 'rejected')

    def test_resolved_at_optional_and_naive_made_aware(self):
        self.assertIsNone(validate_payload(_good(resolved_at=None))['resolved_at'])
        aware = validate_payload(_good(resolved_at='2026-09-16T18:00:00'))['resolved_at']
        self.assertIsNotNone(aware.tzinfo)
        with self.assertRaises(VerdictError):
            validate_payload(_good(resolved_at='вчера'))

    def test_schema_shape(self):
        for bad in ('public', 'Dev!', '1x'):
            with self.assertRaises(VerdictError, msg=bad):
                validate_payload(_good(tenant_schema=bad))
        self.assertEqual(validate_payload(_good(tenant_schema=''))['tenant_schema'], '')

    def test_verdict_too_long(self):
        with self.assertRaises(VerdictError):
            validate_payload(_good(verdict='x' * 2001))

    def test_not_dict(self):
        with self.assertRaises(VerdictError):
            validate_payload('nope')


def _post(body, secret=None, remote='172.18.0.1', xff=None, raw=None):
    extra = {'REMOTE_ADDR': remote}
    if secret is not None:
        extra['HTTP_X_LOYALUP_RELAY_SECRET'] = secret
    if xff:
        extra['HTTP_X_FORWARDED_FOR'] = xff
    request = RequestFactory().post(URL, data=raw if raw is not None else json.dumps(body),
                                    content_type='application/json', **extra)
    return ComplaintVerdictView.as_view()(request)


@override_settings(LOYALUP_RELAY_SECRET=SECRET)
class VerdictViewGuardTest(SimpleTestCase):

    def test_forwarded_is_forbidden(self):
        self.assertEqual(_post(_good(), secret=SECRET, xff='1.2.3.4').status_code, 403)

    def test_public_remote_is_forbidden(self):
        self.assertEqual(_post(_good(), secret=SECRET, remote='8.8.8.8').status_code, 403)

    @override_settings(LOYALUP_RELAY_SECRET='')
    def test_unconfigured_is_500(self):
        r = _post(_good(), secret='x')
        self.assertEqual(r.status_code, 500)
        self.assertEqual(json.loads(r.content)['code'], 'not_configured')

    def test_bad_or_missing_secret_403(self):
        self.assertEqual(_post(_good(), secret='nope').status_code, 403)
        self.assertEqual(_post(_good()).status_code, 403)

    def test_bad_json_400(self):
        self.assertEqual(_post(None, secret=SECRET, raw='{').status_code, 400)

    def test_get_405(self):
        request = RequestFactory().get(URL, REMOTE_ADDR='127.0.0.1')
        self.assertEqual(ComplaintVerdictView.as_view()(request).status_code, 405)

    @mock.patch('apps.shared.relay.verdict.apply_verdict', return_value={'ok': True, 'status': 'resolved', 'previous_status': ''})
    @mock.patch('apps.shared.relay.verdict.resolve_schema', return_value='dev')
    def test_happy_path(self, resolve, apply):
        r = _post(_good(), secret=SECRET)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(json.loads(r.content)['ok'])
        resolve.assert_called_once()
        self.assertEqual(apply.call_args[0][0], 'dev')
        self.assertEqual(apply.call_args[0][1]['conversation_id'], 3224)

    @mock.patch('apps.shared.relay.verdict.resolve_schema', return_value='dev')
    @mock.patch('apps.shared.relay.verdict.apply_verdict',
                side_effect=VerdictError(409, 'already_closed', 'закрыта', current_status='rejected'))
    def test_error_passthrough(self, *_):
        r = _post(_good(), secret=SECRET)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(json.loads(r.content), {'code': 'already_closed', 'detail': 'закрыта', 'current_status': 'rejected'})

    def test_invalid_complaint_id_400(self):
        r = _post(_good(complaint_id='abc'), secret=SECRET)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(json.loads(r.content)['code'], 'invalid_complaint_id')


class _FakeConv:
    def __init__(self, status=''):
        self.checkup_status = status
        self.checkup_verdict = ''
        self.checkup_manager = ''
        self.checkup_complaint_id = ''
        self.checkup_verdict_at = None
        self.checkup_resolved_at = None
        self.saved = None

    def save(self, update_fields=None):
        self.saved = update_fields


class ApplyVerdictTest(SimpleTestCase):
    """Запись вердикта на подменённой переписке: схему не открываем, ORM не трогаем."""

    def _run(self, conv, **over):
        payload = validate_payload(_good(**over))
        fake_model = mock.MagicMock()
        fake_model.objects.filter.return_value.first.return_value = conv
        with mock.patch('apps.shared.relay.verdict.schema_context'), \
             mock.patch('apps.tenant.branch.models.TestimonialConversation', fake_model):
            return apply_verdict('dev', payload)

    def test_resolved_writes_fields(self):
        conv = _FakeConv()
        out = self._run(conv)
        self.assertEqual(out['status'], 'resolved')
        self.assertEqual(out['previous_status'], '')
        self.assertEqual(conv.checkup_status, 'resolved')
        self.assertEqual(conv.checkup_verdict, 'Компенсация 500 баллов')
        self.assertEqual(conv.checkup_manager, 'Алина')
        self.assertEqual(conv.checkup_complaint_id, '3224-app')
        self.assertIsNotNone(conv.checkup_verdict_at)
        self.assertEqual(conv.checkup_resolved_at.utcoffset(), datetime.timedelta(hours=3))
        self.assertIn('checkup_status', conv.saved)

    def test_resolved_without_resolved_at_uses_now(self):
        conv = _FakeConv()
        self._run(conv, resolved_at=None)
        self.assertIsNotNone(conv.checkup_resolved_at)

    def test_in_progress_clears_resolved_at(self):
        conv = _FakeConv(status='resolved')
        conv.checkup_resolved_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        with self.assertRaises(VerdictError) as cm:
            self._run(conv, status='in_progress')
        self.assertEqual((cm.exception.status, cm.exception.code), (409, 'already_closed'))
        self.assertEqual(cm.exception.extra['current_status'], 'resolved')
        out = self._run(conv, status='in_progress', force=True)
        self.assertEqual(out['previous_status'], 'resolved')
        self.assertIsNone(conv.checkup_resolved_at)

    def test_same_closed_status_is_idempotent(self):
        conv = _FakeConv(status='resolved')
        out = self._run(conv, verdict='уточнение')
        self.assertEqual(out['status'], 'resolved')
        self.assertEqual(conv.checkup_verdict, 'уточнение')

    def test_not_found_404(self):
        with self.assertRaises(VerdictError) as cm:
            self._run(None)
        self.assertEqual((cm.exception.status, cm.exception.code), (404, 'complaint_not_found'))
