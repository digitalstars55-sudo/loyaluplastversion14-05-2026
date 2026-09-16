"""Тесты вебхука доставки: подпись и наблюдение за кандидатом секрета (волна 0, 16.09.2026)."""
import os
from unittest.mock import patch

from django.test import SimpleTestCase

from .api.services import observe_webhook_secret_candidate, verify_webhook_signature


class _Req:
    """Минимальный request: заголовки + META, как у DRF Request."""

    def __init__(self, header: str | None = None, ip: str = '10.0.0.1'):
        self.headers = {} if header is None else {'X-Webhook-Secret': header}
        self.META = {'REMOTE_ADDR': ip}


class VerifyWebhookSignatureTest(SimpleTestCase):
    """Решение о допуске — ровно как раньше: пустой секрет пускает всех, заданный сверяется."""

    def test_no_secret_allows_everything(self):
        with patch.dict(os.environ, {'DELIVERY_WEBHOOK_SECRET': '', 'DELIVERY_WEBHOOK_SECRET_CANDIDATE': ''}):
            self.assertTrue(verify_webhook_signature(_Req()))
            self.assertTrue(verify_webhook_signature(_Req('whatever')))

    def test_secret_set_requires_exact_match(self):
        with patch.dict(os.environ, {'DELIVERY_WEBHOOK_SECRET': 's3cret'}):
            self.assertTrue(verify_webhook_signature(_Req('s3cret')))
            self.assertFalse(verify_webhook_signature(_Req('S3CRET')))
            self.assertFalse(verify_webhook_signature(_Req('')))
            self.assertFalse(verify_webhook_signature(_Req()))

    def test_candidate_never_changes_decision(self):
        # секрет пуст, кандидат задан и НЕ совпадает — всё равно пускаем
        with patch.dict(os.environ, {'DELIVERY_WEBHOOK_SECRET': '', 'DELIVERY_WEBHOOK_SECRET_CANDIDATE': 'future'}):
            self.assertTrue(verify_webhook_signature(_Req('wrong')))
            self.assertTrue(verify_webhook_signature(_Req()))
        # секрет задан — кандидат вообще не участвует
        with patch.dict(os.environ, {'DELIVERY_WEBHOOK_SECRET': 's3cret', 'DELIVERY_WEBHOOK_SECRET_CANDIDATE': 'wrong'}):
            self.assertTrue(verify_webhook_signature(_Req('s3cret')))
            self.assertFalse(verify_webhook_signature(_Req('wrong')))

    def test_observation_failure_does_not_block_request(self):
        with patch.dict(os.environ, {'DELIVERY_WEBHOOK_SECRET': ''}), \
             patch('apps.tenant.delivery.api.services.observe_webhook_secret_candidate',
                   side_effect=RuntimeError('boom')):
            self.assertTrue(verify_webhook_signature(_Req()))


class ObserveCandidateTest(SimpleTestCase):
    """Кандидат только классифицирует присланный заголовок и пишет в лог."""

    def test_off_without_candidate(self):
        self.assertEqual(observe_webhook_secret_candidate(_Req('x'), candidate=''), 'off')

    def test_statuses(self):
        self.assertEqual(observe_webhook_secret_candidate(_Req(), candidate='future'), 'missing')
        self.assertEqual(observe_webhook_secret_candidate(_Req('nope'), candidate='future'), 'mismatch')
        self.assertEqual(observe_webhook_secret_candidate(_Req('future'), candidate='future'), 'ok')

    def test_reads_candidate_from_env(self):
        with patch.dict(os.environ, {'DELIVERY_WEBHOOK_SECRET_CANDIDATE': 'future'}):
            self.assertEqual(observe_webhook_secret_candidate(_Req('future')), 'ok')

    def test_logs_status_and_source_ip(self):
        with self.assertLogs('apps.tenant.delivery.api.services', level='WARNING') as cm:
            observe_webhook_secret_candidate(_Req(header=None, ip='95.80.120.85'), candidate='future')
        self.assertTrue(any('missing' in line and '95.80.120.85' in line for line in cm.output))
