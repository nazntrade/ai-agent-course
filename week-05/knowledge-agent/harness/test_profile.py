"""Own a test-model lease around one command; never stop model processes."""
from __future__ import annotations
import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from knowledge_agent.chat.test_profile import load_test_profile

class SessionError(RuntimeError):
    pass

class TestSession:
    __test__ = False
    def __init__(self, env=None, opener=None):
        self.env = dict(os.environ if env is None else env)
        self.profile = load_test_profile(self.env)
        self.opener = opener or urllib.request.urlopen
        self.owned_id = None
        self.stop = threading.Event()
        self.thread = None
        self.renew_failed = False

    def _call(self, method, suffix='', payload=None):
        request = urllib.request.Request(self.profile.lease_url + suffix, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Authorization': 'Bearer ' + self.profile.api_key, 'Content-Type': 'application/json'})
        try:
            with self.opener(request, timeout=270 if method == 'POST' else 15) as response:
                body = response.read()
            return json.loads(body) if body else {}
        except Exception:
            raise SessionError('test_model_session_failed: lifecycle request failed') from None

    def _heartbeat(self, interval):
        while not self.stop.wait(interval):
            try:
                self._call('PATCH', '/' + self.owned_id)
            except SessionError:
                self.renew_failed = True
                return

    def __enter__(self):
        profile = self.profile
        if profile is None or profile.kind == 'remote' or profile.lease_id:
            return self.env
        if not profile.lease_url:
            raise SessionError('test_model_session_failed: local LIVE requires a lifecycle endpoint')
        result = self._call('POST', payload={})
        lease_id = result.get('lease_id')
        if not isinstance(lease_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', lease_id):
            raise SessionError('test_model_session_failed: invalid lifecycle response')
        self.owned_id = lease_id
        self.env['AI_TEST_MODEL_LEASE_ID'] = lease_id
        self.env['AI_TEST_MODEL_PARENT_READY'] = '1'
        ttl = result.get('expires_in_seconds')
        if isinstance(ttl, (int, float)) and ttl > 0:
            self.thread = threading.Thread(target=self._heartbeat, args=(max(1, min(45, ttl / 3)),), daemon=True)
            self.thread.start()
        print('TEST_MODEL_SESSION: acquired')
        return self.env

    def __exit__(self, exc_type, exc, tb):
        if self.owned_id:
            self.stop.set()
            if self.thread:
                self.thread.join(timeout=20)
            try:
                self._call('DELETE', '/' + self.owned_id)
                print('TEST_MODEL_SESSION: released')
            except SessionError:
                print('TEST_MODEL_SESSION: release failed')
                if exc_type is None:
                    raise
            if self.renew_failed:
                print('TEST_MODEL_SESSION: renewal failed')
                if exc_type is None:
                    raise SessionError('test_model_session_failed: lease renewal failed')
        return False


def main(argv=None):
    command = list(sys.argv[1:] if argv is None else argv)
    if command and command[0] == '--':
        command.pop(0)
    if not command:
        print('TEST_MODEL_SESSION: command required')
        return 2
    child = None
    try:
        with TestSession() as env:
            child = subprocess.Popen(command, env=env)
            try:
                return child.wait()
            except KeyboardInterrupt:
                # Only stop the child we own, then let finally release the lease.
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                raise
    except KeyboardInterrupt:
        print('TEST_MODEL_SESSION: interrupted')
        return 130
    except (SessionError, ValueError):
        print('TEST_MODEL_SESSION: FAIL (invalid profile or lifecycle unavailable)')
        return 2

if __name__ == '__main__':
    sys.exit(main())

