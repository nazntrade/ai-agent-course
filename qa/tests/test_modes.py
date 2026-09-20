"""Unit tests of the provider-mode decision table (``lib.modes``).

The probe and the launch are fakes, so no port is opened and no process starts.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from lib.modes import (
    KIND_LOCAL,
    KIND_MOCK,
    MODE_AUTO,
    MODE_LOCAL,
    MODE_MOCK,
    MODE_NETWORK,
    NetworkRefused,
    PrerequisiteError,
    normalize_mode,
    resolve_provider,
)


@dataclass
class FakeConfig:
    base_url: str = "http://127.0.0.1:8080/v1"
    model: str = ""
    launch_command: str = ""
    has_local_config: bool = False
    can_launch: bool = False


class ProbeRecorder:
    def __init__(self, models=("probed-model",)):
        self.models = models
        self.calls = []

    def __call__(self, base_url, timeout=2.0):
        self.calls.append(base_url)
        return self.models


class LaunchRecorder:
    def __init__(self, result=True):
        self.result = result
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.result


class NormalizeModeTest(unittest.TestCase):
    def test_default_is_auto(self):
        self.assertEqual(normalize_mode(None), MODE_AUTO)

    def test_case_insensitive(self):
        self.assertEqual(normalize_mode("mock"), MODE_MOCK)

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(PrerequisiteError):
            normalize_mode("TURBO")


class NetworkModeTest(unittest.TestCase):
    def test_network_is_refused_before_any_call(self):
        probe = ProbeRecorder()
        launch = LaunchRecorder()
        with self.assertRaises(NetworkRefused):
            resolve_provider(MODE_NETWORK, FakeConfig(), probe=probe, launch=launch)
        self.assertEqual(probe.calls, [])
        self.assertEqual(launch.calls, 0)


class MockModeTest(unittest.TestCase):
    def test_mock_never_probes_or_launches(self):
        probe = ProbeRecorder()
        launch = LaunchRecorder()
        plan = resolve_provider(MODE_MOCK, FakeConfig(), probe=probe, launch=launch)
        self.assertEqual(plan.kind, KIND_MOCK)
        self.assertFalse(plan.live_local_llm)
        self.assertEqual(probe.calls, [])
        self.assertEqual(launch.calls, 0)


class AutoModeTest(unittest.TestCase):
    def test_reachable_local_model_is_used(self):
        probe = ProbeRecorder(models=("first-model", "second-model"))
        launch = LaunchRecorder()
        plan = resolve_provider(
            MODE_AUTO,
            FakeConfig(model="configured-model"),
            probe=probe,
            launch=launch,
        )
        self.assertEqual(plan.kind, KIND_LOCAL)
        self.assertTrue(plan.live_local_llm)
        self.assertEqual(plan.model, "configured-model")
        self.assertEqual(launch.calls, 0)

    def test_probed_model_is_used_when_none_is_configured(self):
        plan = resolve_provider(
            MODE_AUTO,
            FakeConfig(),
            probe=ProbeRecorder(models=("discovered",)),
            launch=LaunchRecorder(),
        )
        self.assertEqual(plan.model, "discovered")

    def test_unreachable_without_configuration_falls_back_to_mock(self):
        launch = LaunchRecorder()
        plan = resolve_provider(
            MODE_AUTO,
            FakeConfig(),
            probe=ProbeRecorder(models=None),
            launch=launch,
        )
        self.assertEqual(plan.kind, KIND_MOCK)
        self.assertFalse(plan.live_local_llm)
        self.assertIn("no local configuration", plan.reason)
        self.assertEqual(launch.calls, 0)

    def test_launch_is_attempted_when_configured(self):
        launch = LaunchRecorder(result=True)
        plan = resolve_provider(
            MODE_AUTO,
            FakeConfig(
                model="configured", launch_command="start", has_local_config=True,
                can_launch=True,
            ),
            probe=ProbeRecorder(models=None),
            launch=launch,
        )
        self.assertEqual(plan.kind, KIND_LOCAL)
        self.assertTrue(plan.spawned)
        self.assertEqual(launch.calls, 1)

    def test_failed_launch_falls_back_to_mock(self):
        plan = resolve_provider(
            MODE_AUTO,
            FakeConfig(has_local_config=True, can_launch=True),
            probe=ProbeRecorder(models=None),
            launch=LaunchRecorder(result=False),
        )
        self.assertEqual(plan.kind, KIND_MOCK)
        self.assertIn("not reachable", plan.reason)

    def test_non_loopback_endpoint_is_rejected(self):
        with self.assertRaises(PrerequisiteError):
            resolve_provider(
                MODE_AUTO,
                FakeConfig(base_url="https://api.deepseek.com"),
                probe=ProbeRecorder(),
                launch=LaunchRecorder(),
            )


class LocalModeTest(unittest.TestCase):
    def test_without_configuration_it_is_a_prerequisite_error(self):
        probe = ProbeRecorder()
        with self.assertRaises(PrerequisiteError):
            resolve_provider(
                MODE_LOCAL,
                FakeConfig(has_local_config=False),
                probe=probe,
                launch=LaunchRecorder(),
            )
        self.assertEqual(probe.calls, [])

    def test_reachable_server_never_falls_back_to_mock(self):
        plan = resolve_provider(
            MODE_LOCAL,
            FakeConfig(has_local_config=True),
            probe=ProbeRecorder(models=("real",)),
            launch=LaunchRecorder(),
        )
        self.assertEqual(plan.kind, KIND_LOCAL)
        self.assertTrue(plan.live_local_llm)

    def test_unreachable_without_launch_command_is_a_prerequisite_error(self):
        with self.assertRaises(PrerequisiteError):
            resolve_provider(
                MODE_LOCAL,
                FakeConfig(has_local_config=True, can_launch=False),
                probe=ProbeRecorder(models=None),
                launch=LaunchRecorder(),
            )

    def test_unreachable_with_a_failed_launch_is_a_prerequisite_error(self):
        with self.assertRaises(PrerequisiteError):
            resolve_provider(
                MODE_LOCAL,
                FakeConfig(has_local_config=True, can_launch=True),
                probe=ProbeRecorder(models=None),
                launch=LaunchRecorder(result=False),
            )

    def test_configured_launch_yields_a_spawned_local_plan(self):
        plan = resolve_provider(
            MODE_LOCAL,
            FakeConfig(
                model="real",
                launch_command="start",
                has_local_config=True,
                can_launch=True,
            ),
            probe=ProbeRecorder(models=None),
            launch=LaunchRecorder(result=True),
        )
        self.assertEqual(plan.kind, KIND_LOCAL)
        self.assertTrue(plan.spawned)

    def test_non_loopback_endpoint_is_rejected(self):
        with self.assertRaises(PrerequisiteError):
            resolve_provider(
                MODE_LOCAL,
                FakeConfig(base_url="http://10.0.0.5:8080/v1", has_local_config=True),
                probe=ProbeRecorder(),
                launch=LaunchRecorder(),
            )


if __name__ == "__main__":
    unittest.main()
