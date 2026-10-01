"""Portable selected chat test profile. Does not read configuration files."""
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit, quote

PREFIX = 'AI_TEST_MODEL_'

@dataclass(frozen=True)
class TestProfile:
    kind: str
    base_url: str
    name: str
    api_key: str = field(repr=False)
    model_id: str = ''
    lease_url: str = field(default='', repr=False)
    lease_id: str = field(default='', repr=False)


def load_test_profile(env: Mapping[str, str]) -> TestProfile | None:
    if not any(key.startswith(PREFIX) for key in env):
        return None
    def value(key):
        return str(env.get(PREFIX + key, '')).strip()
    kind, base, name, key = (value(k) for k in ('KIND', 'BASE_URL', 'NAME', 'API_KEY'))
    if kind not in ('local', 'remote') or not all((base, name, key)):
        raise ValueError('test_profile_invalid: KIND, BASE_URL, NAME and API_KEY are required')
    parsed = urlsplit(base)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('test_profile_invalid: invalid endpoint')
    loopback = parsed.hostname in ('127.0.0.1', 'localhost', '::1')
    if kind == 'local' and not loopback:
        raise ValueError('test_profile_invalid: local endpoint must be loopback')
    if kind == 'remote' and parsed.scheme != 'https' and not loopback:
        raise ValueError('test_profile_invalid: remote endpoint requires HTTPS')
    lease, model_id, inherited = (value(k) for k in ('LEASE_URL', 'ID', 'LEASE_ID'))
    if lease:
        target = urlsplit(lease)
        expected = '/api/local-models/' + quote(model_id, safe='') + '/test-leases'
        if (kind != 'local' or not model_id or target.scheme != parsed.scheme or
            target.hostname != parsed.hostname or target.port != parsed.port or
            target.path != expected or target.username or target.password or target.query or target.fragment):
            raise ValueError('test_profile_invalid: invalid lifecycle endpoint')
    if inherited and (kind != 'local' or not lease or env.get(PREFIX + 'PARENT_READY') != '1'):
        raise ValueError('test_profile_invalid: invalid inherited session')
    return TestProfile(kind, base.rstrip('/'), name, key, model_id, lease, inherited)

