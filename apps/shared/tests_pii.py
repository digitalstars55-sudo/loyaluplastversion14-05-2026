"""Обезличивание перед OpenAI (решение владельца 15.10.2026): личные данные гостей
наружу не уходят, а сотрудник видит ответ с настоящими значениями."""

import json
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.shared.ai.openai_compat import OpenAICompatClient
from apps.shared.pii import Обезличка

ЖАЛОБА = ('Меня зовут Анна Петрова, звоните +7 (912) 345-67-89 или 89001234567, '
          'почта anna.p@mail.ru. Доставка на ул. Ленина 15, кв. 42. Карта 4276 3800 1234 5679 '
          'списана дважды. Паспорт 45 12 123456. Спасибо Маше и Ивану Сергеевичу! '
          'Заказ №1234567 на 2 350 руб., официант Дмитрий Сидоров нагрубил.')


class _Response:
    ok = True
    status_code = 200
    text = ''

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def _answer(text, tool_args=None):
    output = [{'type': 'message', 'role': 'assistant',
               'content': [{'type': 'output_text', 'text': text}]}]
    if tool_args is not None:
        output.append({'type': 'function_call', 'call_id': 'c1', 'name': 'find_call',
                       'arguments': json.dumps(tool_args, ensure_ascii=False)})
    return {'id': 'r', 'model': 'gpt-6.1-sol', 'status': 'completed', 'output': output, 'usage': {}}


class ОбезличкаTests(SimpleTestCase):

    def test_structured_data_replaced_and_restored(self):
        o = Обезличка()
        скрыто = o.структурные(ЖАЛОБА)
        for секрет in ('912) 345-67-89', '89001234567', 'anna.p@mail.ru', 'Ленина 15',
                       'кв. 42', '4276 3800 1234 5679', '45 12 123456'):
            self.assertNotIn(секрет, скрыто, секрет)
        # Номер заказа и сумма — не личные данные, остаются.
        self.assertIn('№1234567', скрыто)
        self.assertIn('2 350 руб', скрыто)
        self.assertEqual(o.вернуть(скрыто), ЖАЛОБА)

    def test_card_needs_luhn(self):
        o = Обезличка()
        self.assertIn('4276 3800 1234 5678', o.структурные('счёт 4276 3800 1234 5678'))

    def test_names_with_cases_and_known_author(self):
        o = Обезличка()
        скрыто = o.имена(ЖАЛОБА, известные=['Петрова Анна'])
        for имя in ('Анна', 'Петрова', 'Маше', 'Ивану Сергеевичу', 'Дмитрий Сидоров'):
            self.assertNotIn(имя, скрыто, имя)
        self.assertIn('Спасибо [ИМЯ-', скрыто)
        self.assertIn('официант [ИМЯ-', скрыто)
        self.assertEqual(o.вернуть(скрыто), ЖАЛОБА)

    def test_same_value_same_label(self):
        o = Обезличка()
        скрыто = o.структурные('89001234567, повторю: 89001234567')
        self.assertEqual(скрыто.count('[ТЕЛЕФОН-1]'), 2)

    def test_restore_tolerates_missing_brackets_and_nested(self):
        o = Обезличка()
        o.структурные('+79123456789')
        self.assertEqual(o.вернуть({'a': ['звонить ТЕЛЕФОН-1']}), {'a': ['звонить +79123456789']})


@override_settings(OPENAI_MODEL_SMART='gpt-6.1-sol', OPENAI_REASONING_EFFORT='low')
class TransportTests(SimpleTestCase):

    def setUp(self):
        self.client = OpenAICompatClient(api_key='test', base_url='https://example.test/v1')

    def _send(self, payload, **kwargs):
        with mock.patch.object(self.client.session, 'post', return_value=_Response(payload)) as post:
            result = self.client.messages.create(model='claude-sonnet-4-6', max_tokens=50, **kwargs)
        return post.call_args.kwargs['json'], result

    def test_nothing_personal_leaves_and_answer_is_restored(self):
        body, result = self._send(
            _answer('Перезвоните гостю на [ТЕЛЕФОН-1].', tool_args={'phone': '[ТЕЛЕФОН-1]'}),
            system='Разбери.',
            messages=[
                {'role': 'user', 'content': 'Найди звонок 89001234567'},
                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't0', 'name': 'x',
                                                   'input': {'q': 'anna.p@mail.ru'}}]},
                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't0',
                                              'content': 'гость +7 912 345 67 89, ул. Ленина 15'}]},
            ])
        sent = json.dumps(body, ensure_ascii=False)
        for секрет in ('89001234567', 'anna.p@mail.ru', '345 67 89', 'Ленина 15'):
            self.assertNotIn(секрет, sent, секрет)
        self.assertIn('[ТЕЛЕФОН-1]', sent)
        self.assertIn('скрытые личные данные', body['instructions'])
        self.assertEqual(result.content[0].text, 'Перезвоните гостю на 89001234567.')
        self.assertEqual(result.content[1].input, {'phone': '89001234567'})

    def test_no_personal_data_no_extra_instruction(self):
        body, result = self._send(_answer('Ок'), system='Кратко.',
                                  messages=[{'role': 'user', 'content': 'Сколько смен?'}])
        self.assertEqual(body['instructions'], 'Кратко.')
        self.assertEqual(result.content[0].text, 'Ок')

    @override_settings(AI_PII_REDACT='off')
    def test_switch_off(self):
        body, _ = self._send(_answer('Ок'), messages=[{'role': 'user', 'content': '89001234567'}])
        self.assertIn('89001234567', json.dumps(body, ensure_ascii=False))

    def test_stream_restores_label_split_between_chunks(self):
        events = [
            {'type': 'response.output_text.delta', 'delta': 'Номер [ТЕЛ'},
            {'type': 'response.output_text.delta', 'delta': 'ЕФОН-1], всё.'},
            {'type': 'response.completed', 'response': _answer('Номер [ТЕЛЕФОН-1], всё.')},
        ]
        lines = ['data: ' + json.dumps(e, ensure_ascii=False) for e in events]
        response = SimpleNamespace(ok=True, iter_lines=lambda decode_unicode=True: iter(lines),
                                   close=lambda: None)
        with mock.patch.object(self.client, '_post_stream', return_value=response) as post:
            with self.client.messages.stream(model='claude-sonnet-4-6', max_tokens=50,
                                             messages=[{'role': 'user', 'content': '89001234567'}]) as s:
                text = ''.join(e.delta.text for e in s)
                final = s.get_final_message()
        self.assertNotIn('89001234567', json.dumps(post.call_args.args[0], ensure_ascii=False))
        self.assertEqual(text, 'Номер 89001234567, всё.')
        self.assertEqual(final.content[0].text, 'Номер 89001234567, всё.')
