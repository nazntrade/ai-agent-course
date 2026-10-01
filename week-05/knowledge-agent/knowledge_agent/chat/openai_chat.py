"""Selected-profile OpenAI-compatible chat boundary; secrets stay in headers."""
import json
import math
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from .test_profile import TestProfile
from ..domain.contracts import ChatModel, ChatModelIdentity, ChatResult, ChatUsage
from ..domain.errors import ChatUnavailable, ChatTimeout, ChatInvalidResponse, ChatLengthError, ChatModelMissing

class OpenAIChatModel(ChatModel):
    provider = 'openai-compatible'

    def __init__(self, profile: TestProfile, *, timeout=120., max_output_tokens=1024,
                 context_tokens=8192, temperature=0., seed=0, opener=None, preflight_ttl=5.):
        self.profile = profile
        self.model = profile.name
        self.base_url = profile.base_url
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.context_tokens = context_tokens
        self.temperature = temperature
        self.seed = seed
        self._open = opener or urllib.request.urlopen
        self._preflight = None
        self._preflight_at = 0.
        self._preflight_ttl = preflight_ttl

    @property
    def default_options(self):
        capabilities = (self._preflight or {}).get('capabilities') or {}
        local = self.profile.kind == 'local'
        reasoning = 'none' if local and capabilities.get('reasoning_effort') is True else None
        template_kwargs = {'enable_thinking': False} if local and capabilities.get('chat_template_kwargs') is True else None
        return dict(reasoning_effort=reasoning, chat_template_kwargs=template_kwargs,
                    temperature=self.temperature, seed=None, seed_supported=False, num_predict=self.max_output_tokens,
                    num_ctx=self.context_tokens, model_check_kind=self.profile.kind,
                    local_model_id=self.profile.model_id if self.profile.kind == "local" else None)

    def _request(self, path, payload=None):
        headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.profile.api_key}
        if self.profile.lease_id:
            headers['X-AI-Test-Model-Lease-Id'] = self.profile.lease_id
        request = urllib.request.Request(self.base_url + path,
            data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        try:
            return self._open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            # Error text can contain echoed credentials/input: never propagate it.
            if exc.code in (408, 504):
                raise ChatTimeout('The selected chat provider timed out.') from None
            if exc.code == 404:
                raise ChatModelMissing('The selected chat model is unavailable.') from None
            error_text = exc.read(65536).decode('utf-8', 'replace').lower()
            length_markers = ('context length', 'context_length_exceeded', 'maximum context',
                              'context window', 'too many tokens', 'input length', 'input is too long')
            if exc.code == 413 or any(marker in error_text for marker in length_markers):
                raise ChatLengthError('The selected provider rejected the context as too long.') from None
            raise ChatUnavailable('The selected chat provider rejected the request.') from None
        except (TimeoutError, socket.timeout):
            raise ChatTimeout('The selected chat provider timed out.') from None
        except (OSError, urllib.error.URLError):
            raise ChatUnavailable('The selected chat provider is unreachable.') from None

    @staticmethod
    def _read_json(response):
        try:
            return json.load(response)
        except (TimeoutError, socket.timeout):
            raise ChatTimeout('The selected chat response timed out.') from None
        except (OSError, urllib.error.URLError):
            raise ChatUnavailable('The selected chat connection was interrupted.') from None

    @staticmethod
    def _lines(response):
        try:
            yield from response
        except (TimeoutError, socket.timeout):
            raise ChatTimeout('The selected chat stream timed out.') from None
        except (OSError, urllib.error.URLError):
            raise ChatUnavailable('The selected chat stream was interrupted.') from None

    def preflight(self, infer=False, force=False):
        if self._preflight is not None and not force and time.monotonic() - self._preflight_at < self._preflight_ttl:
            return dict(self._preflight)
        info = dict(reachable=False, model_present=False, context_length=None, digest=None,
                    api='/chat/completions', model_check_kind=self.profile.kind)
        try:
            with self._request('/models') as response:
                data = self._read_json(response)
            if not isinstance(data, dict) or not isinstance(data.get('data'), list):
                raise ChatInvalidResponse('Invalid model listing.')
            info['reachable'] = True
            for item in data['data']:
                if isinstance(item, dict) and item.get('id') == self.model:
                    info['model_present'] = True
                    capabilities = item.get('capabilities')
                    if isinstance(capabilities, dict):
                        info['capabilities'] = {name: capabilities.get(name) is True for name in
                                                ('reasoning_effort', 'chat_template_kwargs')}
                    length = item.get('context_length')
                    info['context_length'] = length if isinstance(length, int) and length > 0 else None
                    break
        except (ChatUnavailable, ChatTimeout, ChatModelMissing, ChatInvalidResponse, ValueError):
            pass
        self._preflight = info
        self._preflight_at = time.monotonic()
        return dict(info)

    def identity(self):
        info = self.preflight()
        return ChatModelIdentity(provider=self.provider, base_url=self.base_url, model=self.model,
            context_length=info.get('context_length'), default_options=self.default_options)

    def _payload(self, messages, options, stream):
        opts = dict(self.default_options)
        opts.update(options or {})
        payload = dict(model=self.model, messages=[m.to_dict() for m in messages], stream=stream,
                       temperature=opts['temperature'], max_tokens=opts['num_predict'])
        # Only negotiated selected-local request controls are sent. Other providers
        # remain portable and receive no runtime-specific reasoning extensions.
        for name in ('reasoning_effort', 'chat_template_kwargs'):
            if opts.get(name) is not None:
                payload[name] = opts[name]
        # Many compatible providers reject seed; deterministic temperature is shared.
        if stream:
            payload['stream_options'] = {'include_usage': True}
        return payload

    def _result(self, text, reason, usage, model, started, timings=None):
        parsed_usage = None
        if isinstance(usage, dict):
            def integer(name):
                value = usage.get(name)
                return value if type(value) is int and value >= 0 else None
            values = [integer(k) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')]
            if any(v is not None for v in values):
                parsed_usage = ChatUsage(*values)
        elapsed = (time.perf_counter() - started) * 1000
        rate = timings.get('predicted_per_second') if isinstance(timings, dict) else None
        rate = float(rate) if type(rate) in (int, float) and math.isfinite(rate) and rate > 0 else None
        # Only provider-reported generation timing qualifies as token throughput.
        return ChatResult(text=text, finish_reason=reason if isinstance(reason, str) else None,
            usage=parsed_usage, model=model or self.model, created_at=datetime.now(timezone.utc).isoformat(),
            latency_ms=round(elapsed, 3), output_tokens_per_second=rate)

    def chat(self, messages, options=None):
        started = time.perf_counter()
        with self._request('/chat/completions', self._payload(messages, options, False)) as response:
            try:
                data = self._read_json(response)
            except ValueError:
                raise ChatInvalidResponse('Malformed selected-provider response.') from None
        try:
            choice = data['choices'][0]
            text = choice['message']['content']
            reason = choice.get('finish_reason')
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ChatInvalidResponse('The selected provider returned no answer.') from None
        # An empty answer is never a success, even when the provider reports a
        # length finish reason; details stay limited to safe provider metadata.
        if not isinstance(text, str) or not text.strip():
            usage = data.get('usage') if isinstance(data, dict) else None
            output_tokens = usage.get('completion_tokens') if isinstance(usage, dict) else None
            raise ChatInvalidResponse('The selected provider returned an empty answer.',
                details={'finish_reason': reason if isinstance(reason, str) else None,
                         'output_tokens': output_tokens if type(output_tokens) is int and output_tokens >= 0 else None})
        return self._result(text, reason, data.get('usage'), data.get('model'), started, data.get('timings'))

    def stream_chat(self, messages, options=None):
        started = time.perf_counter()
        pieces, reason, usage, model = [], None, None, self.model
        ended = False
        timings = None
        with self._request('/chat/completions', self._payload(messages, options, True)) as response:
            for raw in self._lines(response):
                try:
                    line = raw.decode('utf-8', 'strict').strip()
                except UnicodeError:
                    raise ChatInvalidResponse('Malformed selected-provider stream encoding.') from None
                if not line.startswith('data:'):
                    continue
                text = line[5:].strip()
                if text == '[DONE]':
                    ended = True
                    break
                try:
                    data = json.loads(text)
                    if not isinstance(data, dict) or data.get('error'):
                        raise ValueError()
                    model = data.get('model') or model
                    if data.get('timings') is not None:
                        timings = data['timings']
                    if data.get('usage') is not None:
                        usage = data['usage']
                    for choice in data.get('choices', []):
                        delta = (choice.get('delta') or {}).get('content')
                        if isinstance(delta, str) and delta:
                            pieces.append(delta)
                            yield {'type': 'token', 'text': delta}
                        if choice.get('finish_reason') is not None:
                            reason = choice['finish_reason']
                except (ValueError, TypeError, AttributeError):
                    raise ChatInvalidResponse('Malformed selected-provider stream.') from None
        answer = ''.join(pieces)
        if not ended or not answer.strip():
            raise ChatInvalidResponse('The selected-provider stream ended unexpectedly.')
        yield {'type': 'done', 'result': self._result(answer, reason, usage, model, started, timings)}

