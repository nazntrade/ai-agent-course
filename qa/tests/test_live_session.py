"""Offline unit tests of the cross-invocation live-session lifecycle.

No process is spawned, no port is inspected and no network call is made: the
launcher, the readiness probe, the listener lookup, the process start time and
the killer are all injected.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_local_e2e
from lib import live_session, modes
from lib.live_session import (
    OWNERSHIP_NONE,
    OWNERSHIP_OWN_DETACHED,
    OWNERSHIP_OWN_LIVE,
    OWNERSHIP_UNPROVEN,
    OWNER_EXTERNAL,
    OWNER_RUNNER,
    STAGE_ACTIVE,
    STAGE_KEPT,
    STAGE_ORPHAN_UNPROVEN,
    SessionState,
    begin_run,
    finish_run,
    prove_ownership,
    read_state,
    report_block,
    start_session,
    stop_session,
    write_state,
)
from lib.local_llm import KEEP_SERVER_ENV


class FakeProcess:
    def __init__(self, pid=777):
        self.pid = pid

    def poll(self):
        return None


class FakeLauncher:
    """Records spawn/wait/stop without any real process."""

    def __init__(self, *, ready=True, pid=777):
        self.ready = ready
        self.process = FakeProcess(pid=pid)
        self.start_calls = 0
        self.ready_calls = 0
        self.stop_calls = []

    def start(self):
        self.start_calls += 1
        return True

    def wait_ready(self):
        self.ready_calls += 1
        return self.ready

    def stop(self, *, keep=False, env=None):
        self.stop_calls.append({"keep": keep})
        return {"stopped": not keep, "left_running": bool(keep), "reason": "fake"}


class KillRecorder:
    def __init__(self):
        self.killed = []

    def __call__(self, pid):
        self.killed.append(pid)
        return True


def _state(**kwargs):
    defaults = dict(
        session_id="session-1",
        stage=STAGE_ACTIVE,
        started_at=1000.0,
        updated_at=1000.0,
        port=8080,
        model="local-model",
        endpoint_owner=OWNER_RUNNER,
        model_loads=1,
    )
    defaults.update(kwargs)
    return SessionState(**defaults)


class ReadWriteStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "live_session.json"

    def test_missing_state_is_none(self):
        self.assertIsNone(read_state(self.path))

    def test_broken_json_is_none(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(read_state(self.path))

    def test_unknown_schema_is_none(self):
        self.path.write_text('{"schema": 99, "session_id": "x"}', encoding="utf-8")
        self.assertIsNone(read_state(self.path))

    def test_round_trip(self):
        state = _state(pid=777, pid_start_time=1000.0, live_runs=2)
        write_state(state, self.path)
        restored = read_state(self.path)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.session_id, "session-1")
        self.assertEqual(restored.pid, 777)
        self.assertEqual(restored.live_runs, 2)
        self.assertEqual(restored.endpoint_owner, OWNER_RUNNER)


class ProveOwnershipTest(unittest.TestCase):
    def _prove(self, state, *, listeners=(), start=None):
        return prove_ownership(
            state,
            listeners_fn=lambda port: list(listeners),
            start_time_fn=(lambda pid: start) if start is not None else (lambda pid: None),
        )

    def test_live_recorded_pid_within_tolerance_is_own_live(self):
        state = _state(pid=777, pid_start_time=1000.0)
        self.assertEqual(self._prove(state, start=1000.5), OWNERSHIP_OWN_LIVE)

    def test_reused_pid_is_not_own_live(self):
        state = _state(pid=777, pid_start_time=1000.0)
        self.assertEqual(self._prove(state, start=9000.0), OWNERSHIP_NONE)

    def test_proven_listener_is_own_detached(self):
        state = _state(pid=None, pid_start_time=None, started_at=1000.0)
        self.assertEqual(
            self._prove(state, listeners=[9001], start=2000.0),
            OWNERSHIP_OWN_DETACHED,
        )

    def test_listener_without_start_time_is_unproven(self):
        state = _state(pid=None, pid_start_time=None)
        self.assertEqual(self._prove(state, listeners=[9001]), OWNERSHIP_UNPROVEN)

    def test_no_process_is_none(self):
        state = _state(pid=None, pid_start_time=None)
        self.assertEqual(self._prove(state), OWNERSHIP_NONE)


class StartSessionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "live_session.json"

    def _start(self, **kwargs):
        launcher = kwargs.pop("launcher", None) or FakeLauncher()
        result = start_session(
            path=self.path,
            base_url="http://127.0.0.1:8080/v1",
            port=8080,
            model="local-model",
            launcher=launcher,
            probe=kwargs.pop("probe", lambda: None),
            listeners_fn=kwargs.pop("listeners_fn", lambda port: []),
            start_time_fn=kwargs.pop("start_time_fn", lambda pid: None),
            now_fn=lambda: 1000.0,
            session_id_fn=lambda: "session-test",
            **kwargs,
        )
        return result, launcher

    def test_external_endpoint_is_used_without_claiming_ownership(self):
        result, launcher = self._start(probe=lambda: ["model"])
        self.assertTrue(result.external)
        self.assertFalse(result.started)
        self.assertEqual(launcher.start_calls, 0)
        stored = read_state(self.path)
        self.assertEqual(stored.endpoint_owner, OWNER_EXTERNAL)
        self.assertEqual(stored.model_loads, 0)

    def test_existing_owned_state_is_adopted_without_a_second_load(self):
        state = _state(pid=777, pid_start_time=1000.0)
        result, launcher = self._start(
            state=state,
            probe=lambda: ["model"],
            listeners_fn=lambda port: [777],
            start_time_fn=lambda pid: 1000.0,
        )
        self.assertTrue(result.adopted)
        self.assertEqual(launcher.start_calls, 0)
        self.assertEqual(result.state.model_loads, 1)
        self.assertTrue(self.path.exists())

    def test_silent_endpoint_without_state_starts_the_model(self):
        result, launcher = self._start(
            listeners_fn=lambda port: [777],
            start_time_fn=lambda pid: 1500.0,
        )
        self.assertTrue(result.started)
        self.assertEqual(launcher.start_calls, 1)
        self.assertEqual(launcher.stop_calls, [{"keep": True}])
        stored = read_state(self.path)
        self.assertEqual(stored.stage, STAGE_ACTIVE)
        self.assertEqual(stored.model_loads, 1)
        self.assertEqual(stored.pid, 777)

    def test_failed_start_is_a_prerequisite_error_and_leaves_no_state(self):
        launcher = FakeLauncher(ready=False)
        result, _ = self._start(launcher=launcher)
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.started)
        self.assertEqual(launcher.stop_calls, [{"keep": False}])
        self.assertFalse(self.path.exists())

    def test_alive_but_not_ready_never_starts_a_second_model(self):
        state = _state(pid=777, pid_start_time=1000.0)
        result, launcher = self._start(
            state=state,
            listeners_fn=lambda port: [777],
            start_time_fn=lambda pid: 1000.0,
        )
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(launcher.start_calls, 0)
        self.assertIn("SESSION_STOP", result.reason)

    def test_stale_state_is_cleared_and_restarted(self):
        state = _state(pid=777, pid_start_time=1000.0)
        result, launcher = self._start(
            state=state,
            listeners_fn=lambda port: [],
            start_time_fn=lambda pid: 5000.0,
        )
        self.assertTrue(result.started)
        self.assertEqual(launcher.start_calls, 1)
        self.assertEqual(result.state.model_loads, 1)


class StopSessionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "live_session.json"

    def test_external_endpoint_is_never_touched(self):
        state = _state(endpoint_owner=OWNER_EXTERNAL, pid=None, pid_start_time=None)
        write_state(state, self.path)
        kill = KillRecorder()
        result = stop_session(
            path=self.path,
            kill_fn=kill,
            listeners_fn=lambda port: [9001],
            start_time_fn=lambda pid: 2000.0,
            probe=lambda port: ["model"],
        )
        self.assertEqual(kill.killed, [])
        self.assertTrue(result.left_running)
        self.assertFalse(self.path.exists())

    def test_own_live_stops_exactly_the_recorded_pid_and_is_idempotent(self):
        state = _state(pid=777, pid_start_time=1000.0)
        write_state(state, self.path)
        kill = KillRecorder()
        result = stop_session(
            path=self.path,
            kill_fn=kill,
            listeners_fn=lambda port: [],
            start_time_fn=lambda pid: 1000.0,
            probe=lambda port: None,
        )
        self.assertEqual(kill.killed, [777])
        self.assertTrue(result.stopped)
        self.assertFalse(self.path.exists())

        kill_again = KillRecorder()
        second = stop_session(
            path=self.path,
            kill_fn=kill_again,
            listeners_fn=lambda port: [],
            start_time_fn=lambda pid: None,
        )
        self.assertEqual(kill_again.killed, [])
        self.assertEqual(second.exit_code, 0)
        self.assertFalse(second.stopped)

    def test_proven_detached_listener_is_killed(self):
        state = _state(pid=777, pid_start_time=1000.0, listener_pids=[9001])
        write_state(state, self.path)
        kill = KillRecorder()
        result = stop_session(
            path=self.path,
            kill_fn=kill,
            listeners_fn=lambda port: [9001],
            start_time_fn=lambda pid: 2000.0 if pid == 9001 else None,
            probe=lambda port: None,
        )
        self.assertEqual(kill.killed, [9001])
        self.assertTrue(result.stopped)

    def test_unproven_ownership_is_left_running(self):
        state = _state(pid=777, pid_start_time=1000.0, listener_pids=[9001])
        write_state(state, self.path)
        kill = KillRecorder()
        result = stop_session(
            path=self.path,
            kill_fn=kill,
            listeners_fn=lambda port: [9001],
            start_time_fn=lambda pid: 5000.0 if pid == 777 else None,
            probe=lambda port: ["model"],
        )
        self.assertEqual(kill.killed, [])
        self.assertTrue(result.left_running)
        stored = read_state(self.path)
        self.assertEqual(stored.stage, STAGE_ORPHAN_UNPROVEN)

    def test_none_ownership_removes_the_state(self):
        state = _state(pid=777, pid_start_time=1000.0)
        write_state(state, self.path)
        kill = KillRecorder()
        result = stop_session(
            path=self.path,
            kill_fn=kill,
            listeners_fn=lambda port: [],
            start_time_fn=lambda pid: None,
        )
        self.assertEqual(kill.killed, [])
        self.assertTrue(result.stopped)
        self.assertFalse(self.path.exists())

    def test_keep_leaves_the_model_running(self):
        state = _state(pid=777, pid_start_time=1000.0)
        write_state(state, self.path)
        kill = KillRecorder()
        result = stop_session(
            path=self.path,
            keep=True,
            kill_fn=kill,
            listeners_fn=lambda port: [],
            start_time_fn=lambda pid: 1000.0,
        )
        self.assertEqual(kill.killed, [])
        self.assertTrue(result.left_running)
        self.assertEqual(read_state(self.path).stage, STAGE_KEPT)

    def test_keep_can_come_from_the_environment(self):
        state = _state(pid=777, pid_start_time=1000.0)
        write_state(state, self.path)
        kill = KillRecorder()
        result = stop_session(
            path=self.path,
            env={KEEP_SERVER_ENV: "1"},
            kill_fn=kill,
            listeners_fn=lambda port: [],
            start_time_fn=lambda pid: 1000.0,
        )
        self.assertEqual(kill.killed, [])
        self.assertEqual(result.state.stage, STAGE_KEPT)

    def test_missing_state_is_idempotent(self):
        result = stop_session(path=self.path)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.stopped)
        self.assertIn("no live session state", result.reason)


class BeginRunTest(unittest.TestCase):
    def test_begin_run_increments_live_runs(self):
        state = _state(live_runs=0)
        begin_run(state, modes.MODE_LOCAL, now_fn=lambda: 1234.0)
        self.assertEqual(state.live_runs, 1)
        self.assertEqual(state.runs, [{"mode": "LOCAL", "status": "running", "at": 1234.0}])
        finish_run(state, modes.MODE_LOCAL, "PASS", now_fn=lambda: 1250.0)
        self.assertEqual(state.runs[0]["status"], "PASS")

    def test_begin_run_is_a_no_op_without_state(self):
        self.assertIsNone(begin_run(None, modes.MODE_LOCAL))


class ReportBlockTest(unittest.TestCase):
    def test_block_carries_no_path_or_base_url(self):
        state = _state(live_runs=3, model_loads=1)
        block = report_block(state, started=True, mode="session")
        self.assertEqual(block["mode"], "session")
        self.assertEqual(block["live_runs"], 3)
        self.assertEqual(block["model_loads"], 1)
        self.assertTrue(block["session_started"])
        self.assertNotIn("base_url", block)
        self.assertNotIn("command", block)


class MockSessionGuardTest(unittest.TestCase):
    def test_mock_and_network_never_read_the_session_state(self):
        calls = []

        def recorder(*args, **kwargs):
            calls.append((args, kwargs))
            return None

        with patch.object(live_session, "read_state", side_effect=recorder):
            self.assertIsNone(run_local_e2e._active_session(modes.MODE_MOCK))
            self.assertIsNone(run_local_e2e._active_session(modes.MODE_NETWORK))
        self.assertEqual(calls, [])

    def test_auto_does_read_an_active_session(self):
        state = _state(stage=STAGE_ACTIVE, endpoint_owner=OWNER_RUNNER)
        with patch.object(live_session, "read_state", return_value=state):
            self.assertIs(
                run_local_e2e._active_session(modes.MODE_AUTO), state
            )

    def test_inactive_stage_is_not_joined(self):
        state = _state(stage="orphan-unproven")
        with patch.object(live_session, "read_state", return_value=state):
            self.assertIsNone(run_local_e2e._active_session(modes.MODE_LOCAL))


if __name__ == "__main__":
    unittest.main()
