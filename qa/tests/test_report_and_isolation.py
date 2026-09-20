"""Unit tests of the report renderer and the isolation guard."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from lib.config import LocalLlmConfig
from lib.discovery import SEARCH_ROOTS_ENV, discover
from lib.isolation import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIPPED,
    IsolationGuard,
    RunDir,
    assert_run_db_path,
    fingerprint,
    fingerprint_db,
    sha256_file,
    stat_only,
)
from lib.metrics import (
    PROVIDER_LLAMA_SERVER,
    PROVIDER_MOCK,
    aggregate,
)
from lib.modes import ProviderPlan
from lib.report import (
    FAIL,
    PASS,
    Check,
    build_report,
    check_llm_calls_consistency,
    check_local_metrics_recorded,
    check_network_api_calls_zero,
    metric_lines,
    render_markdown,
    write_report,
)


def _record(seq, prompt, completion, total, prompt_ms, predicted_ms, ttft_ms):
    return {
        "seq": seq,
        "stream": False,
        "status": 200,
        "duration_ms": 500.0,
        "first_content_ms": None,
        "model": "my-model",
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        },
        "timings": {
            "prompt_n": prompt,
            "prompt_ms": prompt_ms,
            "predicted_n": completion,
            "predicted_ms": predicted_ms,
            "time_to_first_token_ms": ttft_ms,
        },
        "is_llm_call": True,
        "error": None,
    }


LIVE_RECORDS = [
    _record(1, 10, 5, 15, 20.0, 40.0, 150.0),
    _record(2, 30, 9, 39, 60.0, 90.0, 250.0),
]


class LiveMetricLinesTest(unittest.TestCase):
    def setUp(self):
        self.metrics = aggregate(
            LIVE_RECORDS,
            provider=PROVIDER_LLAMA_SERVER,
            model="my-model",
            local_model_used=True,
        )
        from lib.metrics import token_coverage

        self.lines = metric_lines(
            self.metrics, token_coverage(LIVE_RECORDS), live=True, failed_calls=0
        )

    def test_required_lines_are_exact(self):
        self.assertIn("Provider: llama.cpp (llama-server)", self.lines)
        self.assertIn("Model: my-model", self.lines)
        self.assertIn("Local LLM calls: 2 (failed: 0)", self.lines)
        self.assertIn("Input tokens: 40 (llama-server usage; 2/2 calls)", self.lines)
        self.assertIn("Output tokens: 14 (llama-server usage; 2/2 calls)", self.lines)
        self.assertIn("Total tokens: 54 (llama-server usage; 2/2 calls)", self.lines)
        self.assertIn("TTFT: 0.200 s (avg; llama-server timings; 2/2 calls)", self.lines)
        self.assertIn(
            "Total time: 0.210 s (sum; llama-server timings; 2/2 calls)", self.lines
        )
        self.assertIn("Tokens/s: 107.69 (llama-server timings; 2/2 calls)", self.lines)
        self.assertIn("Network API calls: 0", self.lines)
        self.assertIn("Metrics source: llama-server /v1 (real usage/timings)", self.lines)

    def test_incomplete_calls_are_reported_explicitly(self):
        from lib.metrics import token_coverage

        records = LIVE_RECORDS + [
            {
                "seq": 3,
                "stream": False,
                "status": None,
                "usage": None,
                "timings": None,
                "is_llm_call": True,
                "error": "aborted",
                "incomplete": True,
            }
        ]
        metrics = aggregate(
            records,
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        lines = metric_lines(
            metrics,
            token_coverage(records),
            live=True,
            failed_calls=1,
            incomplete_calls=1,
        )
        self.assertTrue(
            any(line.startswith("Incomplete LLM calls: 1") for line in lines)
        )


class MockMetricLinesTest(unittest.TestCase):
    def test_mock_lines_are_marked_synthetic(self):
        records = [
            _record(1, 10, 5, 15, 20.0, 40.0, 150.0),
        ]
        metrics = aggregate(
            records,
            provider=PROVIDER_MOCK,
            model="mock-local",
            local_model_used=False,
        )
        from lib.metrics import token_coverage

        lines = metric_lines(
            metrics, token_coverage(records), live=False, failed_calls=0
        )
        self.assertIn("Provider: mock (loopback stub)", lines)
        self.assertIn("Local model used: NO", lines)
        self.assertIn("Mock LLM calls: 1 (synthetic)", lines)
        self.assertIn("Input tokens: 10 (synthetic mock usage; 1/1 calls)", lines)
        self.assertIn("TTFT: 0.150 s (avg; synthetic mock timings; 1/1 calls)", lines)
        self.assertIn(
            "Total time: 0.060 s (sum; synthetic mock timings; 1/1 calls)", lines
        )
        self.assertIn("Tokens/s: 125.00 (synthetic mock timings; 1/1 calls)", lines)
        self.assertIn(
            "Metrics source: MOCK provider — synthetic values, NOT a real local model",
            lines,
        )
        self.assertFalse(any(line.startswith("Local LLM calls") for line in lines))

    def test_missing_values_are_rendered_as_no_data(self):
        metrics = aggregate(
            [{"is_llm_call": True, "status": 200, "usage": None, "timings": None}],
            provider=PROVIDER_LLAMA_SERVER,
            model="",
            local_model_used=True,
        )
        lines = metric_lines(metrics, None, live=True, failed_calls=0)
        self.assertIn("Model: нет данных", lines)
        self.assertIn("Input tokens: нет данных", lines)
        self.assertIn("Output tokens: нет данных", lines)
        self.assertIn("Total tokens: нет данных", lines)
        self.assertIn("TTFT: нет данных", lines)
        self.assertIn("Total time: нет данных", lines)
        self.assertIn("Tokens/s: нет данных", lines)


class CheckTest(unittest.TestCase):
    def test_metrics_recorded_passes_with_sources(self):
        metrics = aggregate(
            LIVE_RECORDS,
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        check = check_local_metrics_recorded(metrics, LIVE_RECORDS)
        self.assertEqual(check.status, PASS)

    def test_metrics_recorded_fails_on_a_call_count_mismatch(self):
        metrics = aggregate(
            LIVE_RECORDS,
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        check = check_local_metrics_recorded(metrics, LIVE_RECORDS[:1])
        self.assertEqual(check.status, FAIL)

    def test_live_zero_calls_are_a_failure_not_a_pass(self):
        metrics = aggregate(
            [], provider=PROVIDER_LLAMA_SERVER, model="m", local_model_used=True
        )
        check = check_local_metrics_recorded(metrics, [], live=True)
        self.assertEqual(check.status, FAIL)
        self.assertIn("0", check.detail)

    def test_mock_zero_calls_keep_the_previous_behaviour(self):
        metrics = aggregate(
            [], provider=PROVIDER_MOCK, model="mock-local", local_model_used=False
        )
        self.assertEqual(
            check_local_metrics_recorded(metrics, [], live=False).status, PASS
        )

    def test_only_incomplete_calls_never_pass(self):
        incomplete = {
            "seq": 1,
            "stream": False,
            "status": None,
            "usage": None,
            "timings": None,
            "is_llm_call": True,
            "error": "aborted",
            "incomplete": True,
        }
        metrics = aggregate(
            [incomplete],
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        # No tokens may be fabricated for an aborted call.
        self.assertIsNone(metrics["input_tokens"])
        self.assertIsNone(metrics["output_tokens"])
        self.assertIsNone(metrics["total_tokens"])
        self.assertIsNone(metrics["tokens_per_second"]["value"])
        check = check_local_metrics_recorded(metrics, [incomplete], live=True)
        self.assertEqual(check.status, FAIL)
        self.assertIn("incomplete", check.detail)

    def test_completed_calls_plus_one_incomplete_never_pass(self):
        incomplete = {
            "seq": 9,
            "stream": True,
            "status": None,
            "usage": None,
            "timings": None,
            "is_llm_call": True,
            "error": "aborted",
            "incomplete": True,
        }
        records = LIVE_RECORDS + [incomplete]
        metrics = aggregate(
            records,
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        check = check_local_metrics_recorded(metrics, records, live=True)
        self.assertEqual(check.status, FAIL)
        self.assertIn("1 incomplete", check.detail)

    def test_network_zero_is_hard(self):
        self.assertEqual(check_network_api_calls_zero(0).status, PASS)
        self.assertEqual(check_network_api_calls_zero(3).status, FAIL)

    def test_consistency_is_checked_in_mock_and_skipped_in_live(self):
        self.assertEqual(check_llm_calls_consistency(8, 8, live=False).status, PASS)
        self.assertEqual(check_llm_calls_consistency(7, 8, live=False).status, FAIL)
        self.assertEqual(
            check_llm_calls_consistency(7, 8, live=True).status, "SKIPPED"
        )


class ReportTest(unittest.TestCase):
    def _plan(self, kind, live):
        return ProviderPlan(
            mode="MOCK" if kind == "mock" else "AUTO",
            kind=kind,
            base_url="" if kind == "mock" else "http://127.0.0.1:8080/v1",
            live_local_llm=live,
            reason="" if live else "no local runtime",
            model="mock-local" if kind == "mock" else "local-model",
        )

    def test_report_contains_metrics_and_checks(self):
        plan = self._plan("mock", False)
        metrics = aggregate(
            [], provider=PROVIDER_MOCK, model="mock-local", local_model_used=False
        )
        report = build_report(
            mode="MOCK",
            provider_plan=plan,
            metrics=metrics,
            records=[],
            checks=[Check("network_api_calls_zero", PASS, "ok")],
            scenario={"task_id": 1},
            isolation={"status": STATUS_PASS, "reason": "unchanged"},
            run_dir="C:/run",
        )
        self.assertEqual(report["status"], PASS)
        self.assertEqual(report["live_local_llm"]["status"], "SKIPPED")
        self.assertIn("local_llm_metrics", report)

    def test_failed_check_makes_the_run_fail(self):
        plan = self._plan("mock", False)
        metrics = aggregate(
            [], provider=PROVIDER_MOCK, model="mock-local", local_model_used=False
        )
        report = build_report(
            mode="MOCK",
            provider_plan=plan,
            metrics=metrics,
            records=[],
            checks=[Check("scenario_verdict", FAIL, "missing event")],
            scenario={},
            isolation={},
            run_dir="C:/run",
        )
        self.assertEqual(report["status"], FAIL)

    def test_write_report_writes_both_files(self):
        plan = self._plan("mock", False)
        metrics = aggregate(
            [], provider=PROVIDER_MOCK, model="mock-local", local_model_used=False
        )
        report = build_report(
            mode="MOCK",
            provider_plan=plan,
            metrics=metrics,
            records=[],
            checks=[],
            scenario={},
            isolation={},
            run_dir="run",
        )
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_report(tmp, report)
            self.assertTrue(Path(paths["report_json"]).exists())
            self.assertTrue(Path(paths["report_md"]).exists())
            parsed = json.loads(Path(paths["report_json"]).read_text(encoding="utf-8"))
            self.assertEqual(parsed["mode"], "MOCK")
            markdown = Path(paths["report_md"]).read_text(encoding="utf-8")
            self.assertIn("LIVE_LOCAL_LLM: SKIPPED", markdown)

    def test_render_markdown_lists_the_metric_lines(self):
        plan = self._plan("mock", False)
        metrics = aggregate(
            [], provider=PROVIDER_MOCK, model="mock-local", local_model_used=False
        )
        report = build_report(
            mode="MOCK",
            provider_plan=plan,
            metrics=metrics,
            records=[],
            checks=[Check("network_api_calls_zero", PASS, "0")],
            scenario={"events": ["TASK_CREATED"]},
            isolation={"status": STATUS_PASS},
            run_dir="run",
        )
        markdown = render_markdown(report)
        self.assertIn("Network API calls: 0", markdown)
        self.assertIn("network_api_calls_zero", markdown)


class DiscoveryReportTest(unittest.TestCase):
    def _report(self, discovery):
        plan = ProviderPlan(
            mode="AUTO",
            kind="mock",
            base_url="",
            live_local_llm=False,
            reason="no local runtime",
            model="mock-local",
        )
        metrics = aggregate(
            [], provider=PROVIDER_MOCK, model="mock-local", local_model_used=False
        )
        return build_report(
            mode="AUTO",
            provider_plan=plan,
            metrics=metrics,
            records=[],
            checks=[],
            scenario={},
            isolation={},
            run_dir="run",
            discovery=discovery,
        )

    def test_discovery_section_is_rendered(self):
        block = {
            "source": "launcher-search",
            "launcher": "auto-discovered",
            "config_written": "yes",
            "config_path": "qa/local.llm.local.json",
            "endpoint_running_before_run": False,
            "model_source": "default",
            "candidates_considered": 2,
            "search_elapsed_ms": 12,
            "warnings": ["a safe warning"],
        }
        report = self._report(block)
        markdown = render_markdown(report)
        self.assertIn("## Discovery", markdown)
        self.assertIn("launcher-search", markdown)
        self.assertIn("qa/local.llm.local.json", markdown)
        self.assertEqual(report["discovery"]["config_written"], "yes")

    def test_discovery_section_is_absent_without_a_block(self):
        report = self._report(None)
        self.assertNotIn("discovery", report)
        self.assertNotIn("## Discovery", render_markdown(report))

    def test_discovery_never_leaks_the_launcher_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "run_qwen_SECRET_LAUNCHER.bat").write_text(
                "@echo off\n", encoding="utf-8"
            )
            result = discover(
                LocalLlmConfig(),
                env={SEARCH_ROOTS_ENV: tmp},
                probe=lambda url: None,
                port_in_use=lambda host, port: False,
            )
            self.assertNotIn("SECRET_LAUNCHER", repr(result))
            self.assertNotIn(
                "SECRET_LAUNCHER", json.dumps(asdict(result), default=str)
            )
            block = result.as_report_block(
                config_written="no", config_path="qa/local.llm.local.json"
            )
            report = self._report(block)
            with tempfile.TemporaryDirectory() as out:
                paths = write_report(out, report)
                markdown = Path(paths["report_md"]).read_text(encoding="utf-8")
                payload = Path(paths["report_json"]).read_text(encoding="utf-8")
            self.assertNotIn("SECRET_LAUNCHER", markdown)
            self.assertNotIn("SECRET_LAUNCHER", payload)


class IsolationArtifactsTest(unittest.TestCase):
    def test_run_dir_is_unique_and_has_every_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = RunDir("MOCK", root=tmp, stamp="20260101-000000")
            second = RunDir("MOCK", root=tmp, stamp="20260101-000000")
            first.create()
            second.create()
            self.assertNotEqual(first.path, second.path)
            self.assertTrue(first.db_dir.exists())
            self.assertTrue(first.screenshots_dir.exists())
            self.assertEqual(first.llm_calls_jsonl.name, "llm_calls.jsonl")
            self.assertIn("report.md", first.artifact_paths()["report_md"])

    def test_assert_run_db_path_requires_the_run_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = RunDir("MOCK", root=tmp, stamp="stamp")
            run_dir.create()
            assert_run_db_path(run_dir.db_path, run_dir.path)
            with self.assertRaises(ValueError):
                assert_run_db_path(Path(tmp) / "outside.db", run_dir.path)

    def test_fingerprint_detects_a_content_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.db"
            path.write_bytes(b"before")
            before = fingerprint(path)
            path.write_bytes(b"after")
            after = fingerprint(path)
            self.assertNotEqual(before["sha256"], after["sha256"])
            self.assertTrue(before["exists"])
            self.assertEqual(sha256_file(Path(tmp) / "missing.db"), None)

    def test_db_fingerprint_covers_the_wal_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "app.db"
            db.write_bytes(b"x")
            (Path(tmp) / "app.db-wal").write_bytes(b"y")
            result = fingerprint_db(db)
            self.assertEqual(set(result), {"db", "db-wal", "db-shm"})
            self.assertTrue(result["db-wal"]["exists"])

    def test_stat_only_never_returns_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("SECRET=1", encoding="utf-8")
            result = stat_only(env)
            self.assertEqual(set(result), {"path", "exists", "size", "mtime"})
            self.assertNotIn("SECRET", json.dumps(result))


class IsolationGuardTest(unittest.TestCase):
    def _guard(self, tmp):
        db = Path(tmp) / "app.db"
        db.write_bytes(b"owner data")
        env = Path(tmp) / ".env"
        env.write_text("SECRET=1", encoding="utf-8")
        return IsolationGuard(db, env), db

    def test_unchanged_state_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard, _db = self._guard(tmp)
            guard.take_baseline()
            self.assertTrue(guard.baseline_stable)
            result = guard.check()
            self.assertEqual(result.status, STATUS_PASS)

    def test_changed_database_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard, db = self._guard(tmp)
            guard.take_baseline()
            db.write_bytes(b"owner data changed")
            result = guard.check()
            self.assertEqual(result.status, STATUS_FAIL)
            self.assertTrue(result.db_changed)
            self.assertFalse(result.env_changed)

    def test_unstable_baseline_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard, _db = self._guard(tmp)
            with patch(
                "lib.isolation.fingerprint_db",
                side_effect=[{"db": "one"}, {"db": "two"}],
            ):
                guard.take_baseline()
            result = guard.check()
            self.assertEqual(result.status, STATUS_SKIPPED)
            self.assertIn("not stable", result.reason)

    def test_check_without_baseline_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard, _db = self._guard(tmp)
            self.assertEqual(guard.check().status, STATUS_SKIPPED)


if __name__ == "__main__":
    unittest.main()
