"""Regression tests for D28 artifact model field correctness.

These tests import and call the ACTUAL artifact-building functions from
d28-acceptance.py, ensuring a revert of stale-record or hardcoded-gemma
bugs will break these tests.

Does NOT exercise LIVE — uses the extracted builder functions directly.
"""
import unittest
import importlib.util
from pathlib import Path

# Import the scenario module via importlib to handle the hyphen in the filename
SCENARIO_PATH = Path(__file__).resolve().parent.parent / "scenarios" / "d28-acceptance.py"
spec = importlib.util.spec_from_file_location("d28_acceptance", SCENARIO_PATH)
scenario = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scenario)

build_preflight_seed = scenario.build_preflight_seed
build_q_error_artifact = scenario.build_q_error_artifact
build_stability_error_artifact = scenario.build_stability_error_artifact


class TestQErrorArtifactUsesActiveModel(unittest.TestCase):
    """Verify q-*.json error artifact uses active_model, not stale record."""

    def test_error_artifact_model_is_active_model(self):
        """When Q2 fails (Q1 succeeded), error model = active_model."""
        exc = Exception("ProviderInvalidResponse")
        artifact = build_q_error_artifact("What is agent X?", "remote-model-v1", exc)
        self.assertEqual(artifact["model"], "remote-model-v1")
        self.assertEqual(artifact["question"], "What is agent X?")
        self.assertEqual(artifact["type"], "Exception")

    def test_error_artifact_has_no_stale_record(self):
        """Error artifact must NOT contain data from a previous successful Q1."""
        # Simulate: Q1 had record with model "remote-model-from-q1"
        # Q2 fails. The error artifact for Q2 should NOT have Q1's model.
        exc = Exception("InvalidRequest")
        artifact = build_q_error_artifact("Question 2", "remote-model-v1", exc)
        # The artifact model should be the active_model we passed in
        self.assertEqual(artifact["model"], "remote-model-v1")
        # If stale record leak existed, artifact would contain "remote-model-from-q1"
        self.assertNotIn("remote-model-from-q1", artifact.get("model", ""))

    def test_error_artifact_error_fields_preserved(self):
        """Error info fields are preserved in artifact."""
        exc = ValueError("test error")
        artifact = build_q_error_artifact("Q3", "test-model", exc)
        self.assertEqual(artifact["type"], "ValueError")
        self.assertEqual(artifact["message"], "test error")
        self.assertIsNone(artifact["status_code"])  # ValueError has no status_code


class TestStabilityErrorArtifactUsesActiveModel(unittest.TestCase):
    """Verify stability.json error artifact uses active_model fallback."""

    def test_with_run1_models(self):
        exc = Exception("timeout")
        artifact = build_stability_error_artifact(["run1-model"], "fallback", exc)
        self.assertEqual(artifact["model"], "run1-model")

    def test_without_run1_models(self):
        exc = Exception("timeout")
        artifact = build_stability_error_artifact([], "fallback-model", exc)
        self.assertEqual(artifact["model"], "fallback-model")

    def test_model_not_hardcoded_gemma(self):
        """When run1_models empty, model = active_model, NOT hardcoded gemma."""
        exc = Exception("config error")
        artifact = build_stability_error_artifact([], "active-remote", exc)
        self.assertEqual(artifact["model"], "active-remote")
        self.assertNotEqual(artifact["model"], "llama-3.1-8b")


class TestPreflightSeedUsesActiveModel(unittest.TestCase):
    """Verify preflight artifact seed uses active_model."""

    def test_preflight_model_is_active_model(self):
        seed = build_preflight_seed("remote-model")
        self.assertEqual(seed["model"], "remote-model")
        self.assertEqual(seed["provider"], "local")
        self.assertEqual(seed["steps"], {})


if __name__ == "__main__":
    unittest.main()
