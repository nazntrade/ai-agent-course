"""D26-07: loopback port conflict is an explicit error; foreign process untouched."""

from __future__ import annotations

from app.local_process.gemma_manager import GemmaProcessManager
from app.errors import PortInUse


def test_busy_port_refuses_start_without_foreign_stop(tmp_path):
    runtime = tmp_path / "llama-server.exe"
    model = tmp_path / "model.gguf"
    runtime.write_bytes(b"x")
    model.write_bytes(b"y")
    spawned = []

    def popen(args, **kwargs):
        spawned.append(args)
        raise AssertionError("no process may be spawned on a busy port")

    manager = GemmaProcessManager(
        runtime_path=str(runtime),
        gguf_path=str(model),
        model_id="model.gguf",
        port=8791,
        load_timeout=1.0,
        popen=popen,
    )
    manager.port_is_busy = lambda: True
    try:
        manager.ensure_started()
    except PortInUse as exc:
        assert exc.details["port"] == 8791
    else:
        raise AssertionError("a busy port must not start a process")
    assert spawned == []


def test_backend_and_gemma_use_different_ports(tmp_path):
    from app.config import load_settings

    settings = load_settings({})
    assert settings.port != settings.gemma_port
