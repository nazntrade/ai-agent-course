"""LIVE authorization is independent of runner selection and model configuration."""
from __future__ import annotations
import os
import sys
from typing import Mapping

POLICY_KEY = 'AI_TEST_LIVE_POLICY'

class LivePolicyBlocked(RuntimeError):
    """Real model tests are forbidden or have an invalid authorization flag."""


def live_policy(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    raw = source.get(POLICY_KEY)
    if raw is None:
        return 'legacy'
    return raw if raw in ('allowed', 'forbidden') else 'invalid'


def require_live(env: Mapping[str, str] | None = None) -> None:
    policy = live_policy(env)
    if policy in ('forbidden', 'invalid'):
        raise LivePolicyBlocked('LIVE tests blocked by ' + policy + ' policy')


def report_live_blocked(status: str, env: Mapping[str, str] | None = None) -> bool:
    try:
        require_live(env)
    except LivePolicyBlocked:
        print(f'{status}: BLOCKED ({live_policy(env)} policy; real calls NOT_RUN)')
        return True
    return False


def main() -> int:
    return 3 if report_live_blocked('LIVE_POLICY_STATUS') else 0

if __name__ == '__main__':
    sys.exit(main())
