"""Offline unit tests of the narrow local-launcher discovery.

No real launcher, no real endpoint and no machine-specific path is touched: the
search roots are temporary directories and the probe is injected. Discovery
never scans anything unless ``QA_LOCAL_LLM_LAUNCHER`` or
``QA_LOCAL_LLM_SEARCH_ROOTS`` is set explicitly.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from lib.config import LocalLlmConfig
from lib.discovery import (
    DISCOVERY_ENV,
    LAUNCHER_AUTO,
    LAUNCHER_CONFIGURED,
    LAUNCHER_ENV,
    LAUNCHER_NONE,
    MODEL_SOURCE_ENDPOINT,
    SEARCH_ROOTS_ENV,
    SOURCE_DISABLED,
    SOURCE_ENV,
    SOURCE_LAUNCHER_ENV,
    SOURCE_LAUNCHER_SEARCH,
    SOURCE_LOCAL_CONFIG,
    SOURCE_NONE,
    SOURCE_RUNNING_ENDPOINT,
    LauncherHandoff,
    SearchOutcome,
    build_endpoint_payload,
    build_launcher_payload,
    discover,
    explicit_launcher,
    launcher_tier,
    search_launchers,
    search_roots,
    write_local_config,
)


def _config(**kwargs) -> LocalLlmConfig:
    return LocalLlmConfig(**kwargs)


def _never_probe(*args, **kwargs):
    raise AssertionError("the probe must not run in this scenario")


def _never_search(**kwargs):
    raise AssertionError("the launcher must not be searched in this scenario")


def _never_scandir(*args, **kwargs):
    raise AssertionError("no directory must be scanned in this scenario")


def _touch(directory, name, body="@echo off\n"):
    path = Path(directory) / name
    path.write_text(body, encoding="utf-8")
    return path


class FakeClock:
    """Return a scripted sequence of timestamps (the last value repeats)."""

    def __init__(self, values):
        self._values = list(values)
        self._index = 0

    def __call__(self):
        value = self._values[min(self._index, len(self._values) - 1)]
        self._index += 1
        return value


class SearchSpy:
    """Record search calls and return an empty outcome."""

    def __init__(self, outcome=None):
        self.outcome = outcome or SearchOutcome()
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.outcome


class SearchRootsTest(unittest.TestCase):
    def test_only_the_explicit_roots_are_returned_and_deduplicated(self):
        env = {SEARCH_ROOTS_ENV: os.pathsep.join([r"C:\AI\Scripts", r"C:\AI\Scripts"])}
        roots = search_roots(env=env)
        normalized = [os.path.normcase(os.path.normpath(root)) for root in roots]
        self.assertEqual(normalized, [os.path.normcase(r"C:\AI\Scripts")])

    def test_no_root_without_the_opt_in_variable(self):
        self.assertEqual(search_roots(env={}), [])
        self.assertEqual(search_roots(env={"USERPROFILE": r"C:\Users\tester"}), [])

    def test_explicit_launcher_strips_optional_quotes(self):
        env = {LAUNCHER_ENV: '  "C:\\tools\\start qwen.bat"  '}
        self.assertEqual(explicit_launcher(env), r"C:\tools\start qwen.bat")
        self.assertEqual(explicit_launcher({}), "")


class LauncherTierTest(unittest.TestCase):
    def test_tiers_and_plain_start_is_rejected(self):
        self.assertEqual(launcher_tier("start_qwen.bat"), 0)
        self.assertEqual(launcher_tier("qwen_start.cmd"), 0)
        self.assertEqual(launcher_tier("run_qwen.bat"), 0)
        self.assertEqual(launcher_tier("start_llama_server.bat"), 1)
        self.assertEqual(launcher_tier("start_llm.cmd"), 1)
        self.assertIsNone(launcher_tier("start.bat"))
        self.assertIsNone(launcher_tier("notes.txt"))


class SearchLaunchersTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def test_matching_and_ranking_are_deterministic(self):
        _touch(self.root, "start_llama_server.bat")
        _touch(self.root, "run_qwen.cmd")
        _touch(self.root, "start_qwen.bat")
        _touch(self.root, "start.bat")
        _touch(self.root, "notes.txt")
        nested = Path(self.root) / "nested"
        nested.mkdir()
        _touch(nested, "start_qwen.bat")

        outcome = search_launchers(roots=[self.root])
        names = [os.path.basename(path) for path in outcome.candidates]
        self.assertEqual(
            names, ["start_qwen.bat", "run_qwen.cmd", "start_llama_server.bat"]
        )
        self.assertEqual(outcome.considered, 3)

    def test_empty_roots_scan_nothing(self):
        outcome = search_launchers(roots=[], scandir=_never_scandir)
        self.assertEqual(outcome.candidates, ())
        self.assertEqual(outcome.considered, 0)

    def test_scan_has_no_recursion(self):
        nested = Path(self.root) / "nested"
        nested.mkdir()
        _touch(nested, "start_qwen.bat")
        outcome = search_launchers(roots=[self.root])
        self.assertEqual(outcome.candidates, ())

    def test_deadline_limit_is_reported(self):
        _touch(self.root, "start_qwen.bat")
        outcome = search_launchers(
            roots=[self.root],
            clock=FakeClock([0.0, 100.0]),
            deadline_seconds=5.0,
        )
        self.assertEqual(outcome.candidates, ())
        self.assertTrue(any("time limit" in warning for warning in outcome.warnings))

    def test_file_limit_is_reported(self):
        for index in range(5):
            _touch(self.root, f"notes_{index}.bat")
        _touch(self.root, "start_qwen.bat")
        outcome = search_launchers(roots=[self.root], max_files=3)
        self.assertTrue(any("file limit" in warning for warning in outcome.warnings))

    def test_candidate_limit_is_reported(self):
        _touch(self.root, "start_qwen.bat")
        _touch(self.root, "run_qwen.bat")
        _touch(self.root, "qwen_start.cmd")
        outcome = search_launchers(roots=[self.root], max_candidates=1)
        self.assertEqual(len(outcome.candidates), 1)
        self.assertTrue(
            any("candidate limit" in warning for warning in outcome.warnings)
        )


class WriteLocalConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name) / "local.llm.local.json"
        self.runs = Path(self._tmp.name) / "runs"

    def test_creates_once_and_never_overwrites(self):
        payload = {"base_url": "http://127.0.0.1:8080/v1"}
        self.assertTrue(write_local_config(self.target, payload, tmp_dir=self.runs))
        self.assertEqual(
            json.loads(self.target.read_text(encoding="utf-8")), payload
        )
        self.assertFalse(
            write_local_config(
                self.target, {"base_url": "http://127.0.0.1:9999/v1"}, tmp_dir=self.runs
            )
        )
        self.assertEqual(
            json.loads(self.target.read_text(encoding="utf-8")), payload
        )

    def test_broken_existing_file_is_not_overwritten(self):
        self.target.write_text("{ not json", encoding="utf-8")
        self.assertFalse(
            write_local_config(
                self.target, {"base_url": "http://127.0.0.1:8080/v1"}, tmp_dir=self.runs
            )
        )
        self.assertEqual(self.target.read_text(encoding="utf-8"), "{ not json")

    def test_empty_payload_writes_nothing(self):
        self.assertFalse(write_local_config(self.target, None, tmp_dir=self.runs))
        self.assertFalse(write_local_config(self.target, {}, tmp_dir=self.runs))
        self.assertFalse(self.target.exists())

    def test_temporary_file_is_not_left_behind(self):
        write_local_config(
            self.target, {"base_url": "http://127.0.0.1:8080/v1"}, tmp_dir=self.runs
        )
        leftovers = [
            entry
            for entry in self.runs.iterdir()
            if entry.suffix == ".tmp"
        ]
        self.assertEqual(leftovers, [])


class PayloadTest(unittest.TestCase):
    def test_endpoint_payload_has_no_launch_command(self):
        payload = build_endpoint_payload("http://127.0.0.1:8080/v1", "my-model")
        self.assertEqual(payload["base_url"], "http://127.0.0.1:8080/v1")
        self.assertEqual(payload["model"], "my-model")
        self.assertNotIn("launch_command", payload)
        self.assertNotIn("launch_cwd", payload)

    def test_launcher_payload_quotes_the_command_and_keeps_the_cwd(self):
        payload = build_launcher_payload(
            "http://127.0.0.1:8080/v1",
            "",
            r"C:\tools\qwen\start_qwen.bat",
            r"C:\tools\qwen",
        )
        self.assertEqual(payload["launch_command"], '"C:\\tools\\qwen\\start_qwen.bat"')
        self.assertEqual(payload["launch_cwd"], r"C:\tools\qwen")

    def test_an_already_quoted_command_is_not_quoted_twice(self):
        payload = build_launcher_payload("u", "", '"C:\\x y\\go.bat"')
        self.assertEqual(payload["launch_command"], '"C:\\x y\\go.bat"')


class DiscoverTest(unittest.TestCase):
    def test_disabled_never_probes(self):
        result = discover(_config(), env={DISCOVERY_ENV: "off"}, probe=_never_probe)
        self.assertEqual(result.source, SOURCE_DISABLED)
        self.assertEqual(result.launcher, LAUNCHER_NONE)
        self.assertFalse(result.write_allowed)
        self.assertIsNone(result.pending_config)

    def test_existing_env_configuration_is_not_searched(self):
        config = replace(
            _config(),
            has_local_config=True,
            source="env",
            launch_command="machine-specific",
        )
        result = discover(config, env={}, probe=_never_probe)
        self.assertEqual(result.source, SOURCE_ENV)
        self.assertEqual(result.launcher, LAUNCHER_CONFIGURED)
        self.assertEqual(result.config.launch_command, "")

    def test_existing_file_configuration_is_reported(self):
        config = replace(
            _config(), has_local_config=True, source="file", launch_command="machine"
        )
        result = discover(config, env={}, probe=_never_probe)
        self.assertEqual(result.source, SOURCE_LOCAL_CONFIG)
        self.assertEqual(result.launcher, LAUNCHER_CONFIGURED)

    def test_running_endpoint_is_used_without_a_launcher(self):
        result = discover(_config(), env={}, probe=lambda url: ["endpoint-model"])
        self.assertEqual(result.source, SOURCE_RUNNING_ENDPOINT)
        self.assertTrue(result.endpoint_running)
        self.assertTrue(result.config.has_local_config)
        self.assertEqual(result.model_source, MODEL_SOURCE_ENDPOINT)
        # A running endpoint is never a launcher source and creates no config.
        self.assertIsNone(result.pending_config)

    def test_running_endpoint_never_writes_a_config_even_with_write_allowed(self):
        result = discover(
            _config(), env={}, probe=lambda url: ["endpoint-model"], write_allowed=True
        )
        self.assertTrue(result.write_allowed)
        self.assertIsNone(result.pending_config)

    def test_running_endpoint_has_no_pending_config_without_write_allowed(self):
        result = discover(
            _config(), env={}, probe=lambda url: [], write_allowed=False
        )
        self.assertIsNone(result.pending_config)
        self.assertFalse(result.write_allowed)

    def test_no_explicit_source_never_searches(self):
        spy = SearchSpy()
        result = discover(
            _config(),
            env={},
            probe=lambda url: None,
            search=spy,
            port_in_use=lambda host, port: False,
        )
        self.assertEqual(result.source, SOURCE_NONE)
        self.assertEqual(result.launcher, LAUNCHER_NONE)
        self.assertEqual(spy.calls, [])
        self.assertIsNone(result.pending_config)

    def test_discovery_is_off_when_there_is_no_explicit_source(self):
        result = discover(
            _config(),
            env={},
            probe=lambda url: None,
            search=_never_search,
            port_in_use=lambda host, port: False,
        )
        self.assertFalse(result.config.has_local_config)
        self.assertTrue(result.warnings)
        self.assertTrue(
            any("not searched" in warning for warning in result.warnings)
        )

    def test_explicit_launcher_env_path_fills_the_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _touch(tmp, "start_qwen.bat")
            handoff = LauncherHandoff()
            result = discover(
                _config(),
                env={LAUNCHER_ENV: str(path)},
                probe=lambda url: None,
                port_in_use=lambda host, port: False,
                launcher_handoff=handoff,
                search=_never_search,
            )
            self.assertEqual(result.source, SOURCE_LAUNCHER_ENV)
            self.assertEqual(result.launcher, LAUNCHER_CONFIGURED)
            self.assertTrue(handoff.found)
            self.assertTrue(handoff.launch_command.startswith('"'))
            self.assertTrue(handoff.launch_command.endswith('"'))
            self.assertEqual(
                os.path.normcase(handoff.launch_cwd), os.path.normcase(tmp)
            )
            self.assertTrue(result.config.has_local_config)
            self.assertEqual(result.candidates_considered, 1)

    def test_missing_explicit_launcher_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "missing_launcher.bat")
            result = discover(
                _config(),
                env={LAUNCHER_ENV: missing},
                probe=lambda url: None,
                port_in_use=lambda host, port: False,
                search=_never_search,
            )
            self.assertEqual(result.source, SOURCE_NONE)
            self.assertEqual(result.launcher, LAUNCHER_NONE)
            self.assertIsNone(result.pending_config)
            self.assertTrue(
                any("QA_LOCAL_LLM_LAUNCHER" in warning for warning in result.warnings)
            )

    def test_search_roots_scope_is_passed_to_the_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            spy = SearchSpy()
            discover(
                _config(),
                env={SEARCH_ROOTS_ENV: tmp},
                probe=lambda url: None,
                search=spy,
                port_in_use=lambda host, port: False,
            )
            self.assertEqual(len(spy.calls), 1)
            self.assertEqual(
                [os.path.normcase(root) for root in spy.calls[0]["roots"]],
                [os.path.normcase(tmp)],
            )

    def test_launcher_search_fills_the_handoff_but_not_the_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            _touch(tmp, "start_qwen.bat")
            handoff = LauncherHandoff()
            result = discover(
                _config(),
                env={SEARCH_ROOTS_ENV: tmp},
                probe=lambda url: None,
                launcher_handoff=handoff,
                port_in_use=lambda host, port: False,
            )
            self.assertEqual(result.source, SOURCE_LAUNCHER_SEARCH)
            self.assertEqual(result.launcher, LAUNCHER_AUTO)
            self.assertTrue(handoff.found)
            self.assertTrue(handoff.launch_command.startswith('"'))
            self.assertTrue(handoff.launch_command.endswith('"'))
            self.assertTrue(result.config.has_local_config)
            self.assertGreaterEqual(result.candidates_considered, 1)

    def test_nothing_found_is_a_plain_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = discover(
                _config(),
                env={SEARCH_ROOTS_ENV: tmp},
                probe=lambda url: None,
                port_in_use=lambda host, port: False,
            )
            self.assertEqual(result.source, SOURCE_NONE)
            self.assertIsNone(result.pending_config)
            self.assertFalse(result.config.has_local_config)

    def test_occupied_port_without_models_never_starts_a_launcher(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            result = discover(
                _config(base_url=f"http://127.0.0.1:{port}/v1"),
                env={},
                probe=lambda url: None,
                search=_never_search,
            )
        self.assertEqual(result.source, SOURCE_NONE)
        self.assertEqual(result.launcher, LAUNCHER_NONE)
        self.assertTrue(
            any("already in use" in warning for warning in result.warnings)
        )

    def test_result_never_carries_the_launcher_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            sentinel = _touch(tmp, "run_qwen_SECRET_LAUNCHER.bat")
            result = discover(
                _config(),
                env={SEARCH_ROOTS_ENV: tmp},
                probe=lambda url: None,
                port_in_use=lambda host, port: False,
            )
            text = repr(result) + json.dumps(asdict(result), default=str)
            self.assertNotIn("SECRET_LAUNCHER", text)
            self.assertNotIn(str(tmp), text)
            self.assertTrue(sentinel.exists())


if __name__ == "__main__":
    unittest.main()
