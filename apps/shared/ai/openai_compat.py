"""OpenAI Responses transport with the Anthropic-shaped surface LoyalUP uses.

The application historically passes Anthropic message/tool blocks through
``ClaudeClient``. Rewriting every caller at once would make a provider switch
unnecessarily risky, so this module translates that established internal
contract to the OpenAI Responses API and translates the response back.
"""

from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from typing import Any, Iterable

import requests
from django.conf import settings


class OpenAIAPIError(Exception):
    """Provider error without leaking the API key or request headers."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"OpenAI API error {status_code}: {detail}")


class OpenAIBadRequestError(OpenAIAPIError):
    pass


def _value(value: Any, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    parts = []
    for block in system or []:
        if _value(block, 'type') == 'text':
            parts.append(str(_value(block, 'text', '') or ''))
    return '\n\n'.join(p for p in parts if p)


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _message_input(messages: Iterable[Any]) -> list[dict]:
    out: list[dict] = []
    for message in messages or []:
        role = _value(message, 'role', 'user')
        content = _value(message, 'content', '')
        if isinstance(content, str):
            out.append({'role': role, 'content': content})
            continue

        text_parts: list[str] = []
        rich_parts: list[dict] = []
        deferred: list[dict] = []
        for block in content or []:
            block_type = _value(block, 'type')
            if block_type == 'text':
                text = str(_value(block, 'text', '') or '')
                if text:
                    text_parts.append(text)
                    rich_parts.append({'type': 'input_text', 'text': text})
            elif block_type == 'image':
                source = _value(block, 'source', {}) or {}
                if _value(source, 'type') == 'base64':
                    media_type = _value(source, 'media_type', 'image/jpeg')
                    data = _value(source, 'data', '')
                    rich_parts.append({
                        'type': 'input_image',
                        'image_url': f'data:{media_type};base64,{data}',
                    })
            elif block_type == 'tool_use':
                deferred.append({
                    'type': 'function_call',
                    'call_id': str(_value(block, 'id', '') or ''),
                    'name': str(_value(block, 'name', '') or ''),
                    'arguments': _json_text(_value(block, 'input', {}) or {}),
                })
            elif block_type == 'tool_result':
                deferred.append({
                    'type': 'function_call_output',
                    'call_id': str(_value(block, 'tool_use_id', '') or ''),
                    'output': _json_text(_value(block, 'content', '') or ''),
                })
            elif block_type == 'openai_reasoning':
                item = _value(block, 'item', {}) or {}
                if isinstance(item, dict) and item.get('type') == 'reasoning':
                    deferred.append(item)

        if rich_parts:
            has_image = any(p['type'] == 'input_image' for p in rich_parts)
            out.append({
                'role': role,
                'content': rich_parts if has_image else '\n'.join(text_parts),
            })
        out.extend(deferred)
    return out


def _tools(tools: Iterable[Any]) -> list[dict]:
    converted = []
    for tool in tools or []:
        converted.append({
            'type': 'function',
            'name': str(_value(tool, 'name', '') or ''),
            'description': str(_value(tool, 'description', '') or ''),
            'parameters': _value(tool, 'input_schema', {}) or {},
            'strict': False,
        })
    return converted


def _tool_choice(choice: Any):
    if not choice:
        return None
    if isinstance(choice, str):
        return choice
    kind = _value(choice, 'type')
    if kind == 'tool':
        return {'type': 'function', 'name': _value(choice, 'name', '')}
    if kind == 'any':
        return 'required'
    return 'auto'


def _model(model: str) -> str:
    if model and not model.startswith('claude-'):
        return model
    if 'haiku' in (model or ''):
        return (getattr(settings, 'OPENAI_MODEL_FAST', '')
                or os.getenv('OPENAI_MODEL_FAST') or 'gpt-6-luna')
    return (getattr(settings, 'OPENAI_MODEL_SMART', '')
            or os.getenv('OPENAI_MODEL_SMART') or 'gpt-6.1-sol')


def _response_message(payload: dict):
    blocks = []
    for item in payload.get('output') or []:
        item_type = item.get('type')
        if item_type == 'reasoning':
            blocks.append(SimpleNamespace(
                type='openai_reasoning', item=dict(item)))
        elif item_type == 'message':
            for content in item.get('content') or []:
                if content.get('type') == 'output_text':
                    blocks.append(SimpleNamespace(
                        type='text', text=content.get('text', '') or ''))
                elif content.get('type') == 'refusal':
                    blocks.append(SimpleNamespace(
                        type='text', text=content.get('refusal', '') or ''))
        elif item_type == 'function_call':
            try:
                arguments = json.loads(item.get('arguments') or '{}')
            except (TypeError, ValueError):
                arguments = {}
            blocks.append(SimpleNamespace(
                type='tool_use',
                id=item.get('call_id') or item.get('id') or '',
                name=item.get('name') or '',
                input=arguments,
            ))

    usage_data = payload.get('usage') or {}
    input_details = usage_data.get('input_tokens_details') or {}
    cached = input_details.get('cached_tokens', 0) or 0
    cache_write = input_details.get('cache_write_tokens', 0) or 0
    usage = SimpleNamespace(
        input_tokens=usage_data.get('input_tokens', 0) or 0,
        output_tokens=usage_data.get('output_tokens', 0) or 0,
        cache_creation_input_tokens=cache_write,
        cache_read_input_tokens=cached,
    )
    has_tools = any(b.type == 'tool_use' for b in blocks)
    incomplete = payload.get('status') == 'incomplete'
    return SimpleNamespace(
        id=payload.get('id') or '',
        model=payload.get('model') or '',
        role='assistant',
        stop_reason='tool_use' if has_tools else ('max_tokens' if incomplete else 'end_turn'),
        usage=usage,
        content=blocks,
    )


class _Messages:
    def __init__(self, owner: 'OpenAICompatClient'):
        self.owner = owner

    def create(self, **kwargs):
        timeout = kwargs.pop('timeout', None)
        payload = self.owner._post(self.owner._body(kwargs), timeout=timeout)
        return _response_message(payload)

    def stream(self, **kwargs):
        timeout = kwargs.pop('timeout', None)
        return _ResponseStream(self.owner, self.owner._body(kwargs), timeout)


class _ResponseStream:
    def __init__(self, owner: 'OpenAICompatClient', body: dict, timeout):
        self.owner = owner
        self.body = {**body, 'stream': True}
        self.timeout = timeout
        self.response = None
        self.final_payload = None
        self.text = ''

    def __enter__(self):
        self.response = self.owner._post_stream(self.body, timeout=self.timeout)
        return self

    def __exit__(self, *_args):
        if self.response is not None:
            self.response.close()
        return False

    def __iter__(self):
        for raw_line in self.response.iter_lines(decode_unicode=True):
            if not raw_line or not raw_line.startswith('data:'):
                continue
            data = raw_line[5:].strip()
            if data == '[DONE]':
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            event_type = event.get('type')
            if event_type == 'response.output_text.delta':
                delta = event.get('delta', '') or ''
                self.text += delta
                yield SimpleNamespace(
                    type='content_block_delta',
                    delta=SimpleNamespace(type='text_delta', text=delta),
                )
            elif event_type == 'response.completed':
                self.final_payload = event.get('response') or {}
            elif event_type == 'error':
                raise OpenAIAPIError(502, _json_text(event.get('error') or event))

    def get_final_message(self):
        if self.final_payload is not None:
            return _response_message(self.final_payload)
        return _response_message({
            'id': '', 'model': self.body.get('model', ''), 'status': 'completed',
            'output': [{
                'type': 'message', 'role': 'assistant',
                'content': [{'type': 'output_text', 'text': self.text}],
            }],
            'usage': {},
        })


class OpenAICompatClient:
    """Subset of ``anthropic.Anthropic`` used by the existing application."""

    def __init__(self, *, api_key: str, base_url: str | None = None,
                 timeout: float = 60, max_retries: int = 0, **_ignored):
        self.api_key = api_key
        configured = (base_url or getattr(settings, 'OPENAI_BASE_URL', '')
                      or os.getenv('OPENAI_BASE_URL') or 'https://api.openai.com/v1')
        self.base_url = configured.rstrip('/')
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries or 0))
        self.session = requests.Session()
        proxy_url = (getattr(settings, 'OPENAI_PROXY_URL', '')
                     or os.getenv('OPENAI_PROXY_URL') or '').strip()
        if proxy_url:
            # Scope the proxy to OpenAI only. Global HTTP(S)_PROXY would also
            # reroute VK, Telegram, Senler, POS and every other integration.
            self.session.proxies.update({
                'http': proxy_url,
                'https': proxy_url,
            })
        self.messages = _Messages(self)

    def _body(self, params: dict) -> dict:
        model = _model(params.get('model', ''))
        body = {
            'model': model,
            'input': _message_input(params.get('messages') or []),
            'max_output_tokens': params.get('max_tokens') or 4096,
            'store': False,
            'prompt_cache_options': {'mode': 'implicit', 'ttl': '30m'},
        }
        instructions = _system_text(params.get('system'))
        if instructions:
            body['instructions'] = instructions
        tools = _tools(params.get('tools') or [])
        if tools:
            body['tools'] = tools
        choice = _tool_choice(params.get('tool_choice'))
        if choice is not None:
            body['tool_choice'] = choice
        if model.startswith('gpt-6'):
            default_effort = 'none' if model == 'gpt-6-luna' else 'low'
            body['reasoning'] = {
                'effort': (getattr(settings, 'OPENAI_REASONING_EFFORT_FAST', '')
                           if model == 'gpt-6-luna'
                           else getattr(settings, 'OPENAI_REASONING_EFFORT', ''))
                          or default_effort,
            }
            body['text'] = {'verbosity': 'low'}
        return body

    @property
    def _headers(self):
        return {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }

    @property
    def _url(self):
        return f'{self.base_url}/responses'

    @staticmethod
    def _raise(response):
        try:
            payload = response.json()
            detail = _json_text(payload.get('error') or payload)
        except ValueError:
            detail = (response.text or 'unknown provider error')[:1000]
        error_cls = OpenAIBadRequestError if response.status_code == 400 else OpenAIAPIError
        raise error_cls(response.status_code, detail)

    def _post(self, body: dict, *, timeout=None) -> dict:
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(
                    self._url, headers=self._headers, json=body,
                    timeout=timeout or self.timeout,
                )
                if response.ok:
                    return response.json()
                if response.status_code < 500 and response.status_code != 429:
                    self._raise(response)
                last_error = response
            except requests.RequestException:
                if attempt >= self.max_retries:
                    raise
            if attempt < self.max_retries:
                time.sleep(min(2 ** attempt, 4))
        self._raise(last_error)

    def _post_stream(self, body: dict, *, timeout=None):
        response = self.session.post(
            self._url, headers=self._headers, json=body,
            timeout=timeout or self.timeout, stream=True,
        )
        if not response.ok:
            self._raise(response)
        return response


# Runtime modules import this module under the old local SDK name. Keeping that
# narrow surface avoids a broad rewrite of proven business workflows.
Anthropic = OpenAICompatClient
APIError = OpenAIAPIError
BadRequestError = OpenAIBadRequestError
