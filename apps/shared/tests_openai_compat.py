"""OpenAI transport preserves LoyalUP's established internal AI contract."""

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.shared.ai.openai_compat import OpenAICompatClient


class _Response:
    ok = True
    status_code = 200
    text = ''

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


@override_settings(
    OPENAI_MODEL_FAST='gpt-6-luna',
    OPENAI_MODEL_SMART='gpt-6.1-sol',
    OPENAI_REASONING_EFFORT='low',
)
class OpenAICompatTests(SimpleTestCase):

    def setUp(self):
        self.client = OpenAICompatClient(api_key='test', base_url='https://example.test/v1')

    def test_text_response_maps_model_and_disables_storage(self):
        payload = {
            'id': 'resp_1', 'model': 'gpt-6-luna', 'status': 'completed',
            'output': [{
                'type': 'message', 'role': 'assistant',
                'content': [{'type': 'output_text', 'text': 'Спасибо за отзыв!'}],
            }],
            'usage': {
                'input_tokens': 20, 'output_tokens': 4,
                'input_tokens_details': {
                    'cached_tokens': 7, 'cache_write_tokens': 13,
                },
            },
        }
        with mock.patch.object(self.client.session, 'post', return_value=_Response(payload)) as post:
            result = self.client.messages.create(
                model='claude-haiku-4-5-20251001', max_tokens=100,
                system=[{'type': 'text', 'text': 'Ответь гостю.',
                         'cache_control': {'type': 'ephemeral'}}],
                messages=[{'role': 'user', 'content': 'Всё понравилось'}],
            )

        body = post.call_args.kwargs['json']
        self.assertEqual(body['model'], 'gpt-6-luna')
        self.assertFalse(body['store'])
        self.assertEqual(body['prompt_cache_options']['mode'], 'implicit')
        self.assertEqual(body['instructions'], 'Ответь гостю.')
        self.assertNotIn('cache_control', str(body))
        self.assertEqual(result.content[0].text, 'Спасибо за отзыв!')
        self.assertEqual(result.usage.cache_read_input_tokens, 7)
        self.assertEqual(result.usage.cache_creation_input_tokens, 13)

    def test_lead_tool_call_round_trip(self):
        payload = {
            'id': 'resp_tool', 'model': 'gpt-6-luna', 'status': 'completed',
            'output': [{
                'type': 'reasoning', 'id': 'rs_1',
                'encrypted_content': 'encrypted-test-reasoning', 'summary': [],
            }, {
                'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1',
                'name': 'update_lead', 'arguments': '{"cafe_count":3}',
            }],
            'usage': {'input_tokens': 30, 'output_tokens': 8},
        }
        history = [
            {'role': 'user', 'content': 'У нас три кафе'},
            {'role': 'assistant', 'content': [
                SimpleNamespace(type='tool_use', id='old_call', name='update_lead',
                                input={'cafe_name': 'Тест'}),
            ]},
            {'role': 'user', 'content': [{
                'type': 'tool_result', 'tool_use_id': 'old_call', 'content': 'OK',
            }]},
        ]
        tools = [{
            'name': 'update_lead', 'description': 'Сохранить лид',
            'input_schema': {'type': 'object', 'properties': {
                'cafe_count': {'type': 'integer'},
            }},
        }]
        with mock.patch.object(self.client.session, 'post', return_value=_Response(payload)) as post:
            result = self.client.messages.create(
                model='claude-haiku-4-5-20251001', max_tokens=100,
                messages=history, tools=tools,
            )

        body = post.call_args.kwargs['json']
        self.assertTrue(any(i.get('type') == 'function_call' for i in body['input']))
        self.assertTrue(any(i.get('type') == 'function_call_output' for i in body['input']))
        self.assertEqual(body['tools'][0]['name'], 'update_lead')
        self.assertEqual(result.stop_reason, 'tool_use')
        tool_call = next(b for b in result.content if b.type == 'tool_use')
        self.assertEqual(tool_call.input, {'cafe_count': 3})
        follow_up = self.client._body({
            'model': 'claude-haiku-4-5-20251001', 'max_tokens': 100,
            'messages': [
                {'role': 'assistant', 'content': result.content},
                {'role': 'user', 'content': [{
                    'type': 'tool_result', 'tool_use_id': 'call_1', 'content': 'OK',
                }]},
            ],
        })
        reasoning = next(i for i in follow_up['input'] if i.get('type') == 'reasoning')
        self.assertEqual(reasoning['encrypted_content'], 'encrypted-test-reasoning')
