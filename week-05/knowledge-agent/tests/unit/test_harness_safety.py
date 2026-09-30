"""Offline runner safety and truthful acceptance scope regressions."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from harness import acceptance, live_embed, smoke
from knowledge_agent.config import load_settings
from knowledge_agent.sources.pdf_layout import Paragraph


def test_live_main_requires_opt_in_without_constructing_provider(monkeypatch, capsys):
    monkeypatch.delenv("RUN_EMBED_LIVE", raising=False)
    runner = Mock(side_effect=AssertionError("must not start LIVE without opt-in"))
    monkeypatch.setattr(live_embed, "run_scenario", runner)
    assert live_embed.main() == 3
    assert "BLOCKED" in capsys.readouterr().out
    runner.assert_not_called()


def test_live_main_owns_temporary_data_even_with_inherited_working_db(tmp_path, monkeypatch):
    working = tmp_path / "working.db"
    working.write_bytes(b"untouched working data")
    source = tmp_path / "approved.pdf"
    monkeypatch.setenv("RUN_EMBED_LIVE", "1")
    monkeypatch.setenv("KNOWLEDGE_DB_PATH", str(working))
    monkeypatch.setenv("KNOWLEDGE_SOURCE_PATH", str(source))
    owned = []

    def scenario(settings, root, selected):
        assert root.exists() and root != tmp_path
        assert selected == source
        owned.append(root)
        (root / "index.db").write_bytes(b"owned test artifact")
        return 1

    monkeypatch.setattr(live_embed, "run_scenario", scenario)
    assert live_embed.main() == 1
    assert working.read_bytes() == b"untouched working data"
    assert len(owned) == 1 and not owned[0].exists()


def test_live_preflight_failure_closes_store_and_never_claims_inference(tmp_path, monkeypatch, capsys):
    store = Mock()
    service = SimpleNamespace(_embedder=SimpleNamespace(preflight=lambda: {"reachable": False}))
    monkeypatch.setattr(live_embed, "build_service", lambda _: (service, store))
    assert live_embed.run_scenario(load_settings({}), tmp_path, tmp_path / "source.pdf") == 3
    output = capsys.readouterr().out
    assert "LOCAL_MODEL_INFERENCE: PASS" not in output
    assert "LOCAL_MODEL_INFERENCE: NOT_RUN" in output
    store.close.assert_called_once()


def test_indexable_pages_exclude_references_and_enforce_per_page_threshold(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"synthetic layout")
    line = SimpleNamespace(size=10)
    pages = [[line], [line], [line], [line]]
    adapter = SimpleNamespace(_extract_layout=lambda *_: (pages, [800] * 4, [600] * 4))
    service = SimpleNamespace(
        _prepare_sources=lambda _: [{"adapter": adapter, "source_ref": object()}],
        excluded_roles=("references",),
    )
    monkeypatch.setattr(live_embed, "strip_headers_footers", lambda pages, _: (pages, 0))
    monkeypatch.setattr(live_embed, "order_lines", lambda page, _: page)
    units = iter([
        [(1, Paragraph("Introduction", 1, 12, [])), (None, Paragraph("x" * 500, 1, 10, []))],
        [(None, Paragraph("x" * 499, 2, 10, []))],
        [(1, Paragraph("References", 3, 12, [])), (None, Paragraph("x" * 2000, 3, 10, []))],
        [(1, Paragraph("Appendix", 4, 12, [])), (None, Paragraph("x" * 500, 4, 10, []))],
    ])
    monkeypatch.setattr(live_embed.pdf_source, "_page_units", lambda *_: next(units))
    assert live_embed.indexable_pdf_pages(service, source, 500) == 2


def test_smoke_main_sources_are_owned_and_removed_on_failure(monkeypatch):
    owned = []

    def scenario(root):
        sources = smoke.write_sources(root)
        assert all(Path(source).parent == root and Path(source).is_file() for source in sources)
        owned.append(root)
        return 1

    monkeypatch.setattr(smoke, "run_scenario", scenario)
    assert smoke.main() == 1
    assert len(owned) == 1 and not owned[0].exists()


@pytest.mark.parametrize("live", [False, True])
def test_acceptance_is_automated_only_and_includes_enabled_live_failure(monkeypatch, capsys, live):
    monkeypatch.delenv("RUN_KNOWLEDGE_READONLY_AUDIT", raising=False)
    if live:
        monkeypatch.setenv("RUN_EMBED_LIVE", "1")
    else:
        monkeypatch.delenv("RUN_EMBED_LIVE", raising=False)
    for name in ("inspect_corpus", "inspect_new_chunks", "inspect_legacy_indexes"):
        monkeypatch.setattr(acceptance, name, Mock(side_effect=AssertionError("offline checks must not audit work data")))
    calls = []
    def run(args):
        calls.append(args)
        return 1 if args[-1] == "harness/live_embed.py" else 0
    monkeypatch.setattr(acceptance, "run", run)
    assert acceptance.main() == (1 if live else 0)
    output = capsys.readouterr().out
    assert "D21_ACCEPTANCE_STATUS: NOT_ASSESSED" in output
    assert "TEST_STATUS: PASS" not in output
    assert f"AUTOMATED_STATUS: {'FAIL' if live else 'PASS'}" in output
    assert any(args[-1] == "harness/live_embed.py" for args in calls) == live


def test_live_row_counts_closes_connection_even_when_reference_is_retained(tmp_path, monkeypatch):
    database = tmp_path / "index.db"
    connect = sqlite3.connect
    with closing(connect(database)) as connection:
        for table in ("sources", "documents", "sections", "chunks", "index_versions"):
            connection.execute(f"CREATE TABLE {table} (id TEXT)")
        connection.commit()
    retained = []
    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        retained.append(connection)
        return connection
    monkeypatch.setattr(live_embed.sqlite3, "connect", tracked_connect)
    assert live_embed.row_counts(SimpleNamespace(db_path=str(database))) == {
        "sources": 0, "documents": 0, "sections": 0, "chunks": 0, "index_versions": 0,
    }
    assert len(retained) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        retained[0].execute("SELECT 1")


def test_live_cleanup_failure_never_emits_final_pass(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RUN_EMBED_LIVE", "1")
    monkeypatch.setattr(live_embed, "run_scenario", lambda *_: 0)
    class FailingCleanup:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return str(tmp_path)
        def __exit__(self, *args):
            raise PermissionError("owned database still open")
    monkeypatch.setattr(live_embed.tempfile, "TemporaryDirectory", FailingCleanup)
    assert live_embed.main() == 1
    output = capsys.readouterr().out
    assert "LIVE_CLEANUP: FAIL" in output
    assert "EMBEDDING_LIVE_STATUS: FAIL" in output
    assert "EMBEDDING_LIVE_STATUS: PASS" not in output
    assert "LOCAL_SCENARIO_TEST: PASS" not in output
