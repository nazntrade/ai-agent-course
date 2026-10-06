"""D26-02: local process state machine, idempotent start, stop only own PID."""

from __future__ import annotations

from app.errors import PortInUse
from app.local_process.gemma_manager import (
    STATE_ERROR,
    STATE_READY,
    STATE_UNLOADED,
    GemmaProcessManager,
)


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.terminated = False
        self.killed = False
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


def make_manager(tmp_path, *, busy=False, ready=True):
    runtime = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    runtime.write_bytes(b"x")
    model.write_bytes(b"y")
    created = []

    def popen(args, **kwargs):
        process = FakeProcess()
        created.append(process)
        return process

    manager = GemmaProcessManager(
        runtime_path=str(runtime),
        gguf_path=str(model),
        model_id="model.gguf",
        port=8791,
        load_timeout=1.0,
        popen=popen,
        ready_probe=lambda: ready,
    )
    manager.port_is_busy = lambda: busy
    return manager, created


def test_initial_state_is_unloaded(tmp_path):
    manager, _ = make_manager(tmp_path)
    assert manager.state == STATE_UNLOADED


def test_start_reaches_ready_and_is_idempotent(tmp_path):
    manager, created = make_manager(tmp_path)
    snapshot = manager.ensure_started()
    assert snapshot["state"] == STATE_READY
    assert len(created) == 1
    # A second start must not spawn another process.
    again = manager.ensure_started()
    assert again["state"] == STATE_READY
    assert len(created) == 1
    assert again["pid"] == created[0].pid


def test_stop_only_stops_own_process(tmp_path):
    manager, created = make_manager(tmp_path)
    manager.ensure_started()
    manager.stop()
    assert created[0].terminated is True
    assert manager.state == STATE_UNLOADED
    # A second stop does not touch anything and does not raise.
    manager.stop()


def test_busy_port_is_refused_without_foreign_stop(tmp_path):
    manager, created = make_manager(tmp_path, busy=True)
    try:
        manager.ensure_started()
    except PortInUse as exc:
        assert exc.code == "port_in_use"
    else:
        raise AssertionError("a busy port must raise PortInUse")
    assert created == []  # no process was spawned and none was stopped
    assert manager.state == STATE_ERROR


def test_failed_readiness_stops_own_process(tmp_path):
    manager, created = make_manager(tmp_path, ready=False)
    try:
        manager.ensure_started()
    except Exception as exc:  # noqa: BLE001
        assert "not become ready" in str(exc)
    else:
        raise AssertionError("readiness failure must raise")
    assert created[0].terminated is True
    assert manager.state == STATE_ERROR
