import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from apps.shared.ai.openai_compat import _response_message
from apps.tenant.marketer.generator import generate_post

@override_settings(OPENAI_API_KEY='synthetic-test-key')
class MarketerResponseTest(SimpleTestCase):
    def call_with(self, output):
        message = _response_message({'output': output, 'usage': {}})
        fake = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: message))
        with patch('apps.shared.ai.openai_compat.Anthropic', return_value=fake), patch('apps.shared.ai.usage.log_usage'):
            return generate_post({'brand_name': 'Public Brand'})[0]

    def test_reasoning_before_split_text_is_not_rendered_or_assumed_to_be_text(self):
        output = [
            {'type': 'reasoning', 'id': 'r1', 'encrypted_content': 'private-internal'},
            {'type': 'message', 'content': [
                {'type': 'output_text', 'text': '{"text": "Hello '},
                {'type': 'output_text', 'text': 'guests!"}'},
            ]},
        ]
        self.assertEqual(self.call_with(output), 'Hello guests!')

    def test_no_text_reports_unreadable_response(self):
        with self.assertRaises(RuntimeError):
            self.call_with([{'type': 'reasoning', 'id': 'r1'}])
