"""Unit tests of the loopback mock provider (``lib.mock_provider``).

The tests talk to the mock over loopback HTTP only; no external network is used.
"""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request

from lib.mock_provider import MockFixtures, MockProvider

PLAN_RETRY = "Предыдущий план отклонён"
STEP_RETRY = "Предыдущий результат шага отклонён"


def build_fixtures() -> MockFixtures:
    return MockFixtures(
        bad_plan={
            "summary": "bad",
            "acceptance_criteria": ["criterion"],
            "steps": [{"index": 1, "title": "bad step", "description": "d"}],
        },
        good_plan={
            "summary": "good",
            "acceptance_criteria": ["criterion"],
            "steps": [{"index": 1, "title": "good step", "description": "d"}],
        },
        bad_step_text="bad step text",
        good_step_text="good step text",
        validation_payload={"passed": True, "defects": [], "notes": "ok"},
        planning_markers=("PLAN_SYSTEM",),
        execution_markers=("EXEC_SYSTEM",),
        validation_markers=("VALID_SYSTEM",),
    )


def post_json(base_url, payload):
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def post_stream(base_url, payload):
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8")


def messages(system, retry=None, stream=False):
    payload = {
        "model": "mock-local",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "action"},
        ],
        "stream": stream,
    }
    if retry:
        payload["messages"].append({"role": "user", "content": retry})
    return payload


class MockProviderTest(unittest.TestCase):
    def setUp(self):
        self.provider = MockProvider(build_fixtures()).start()
        self.addCleanup(self.provider.stop)
        self.base_url = self.provider.base_url

    def test_models_endpoint_lists_the_mock_model(self):
        with urllib.request.urlopen(self.base_url + "/models", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["data"][0]["id"], "mock-local")

    def test_models_endpoint_is_not_counted_as_a_call(self):
        urllib.request.urlopen(self.base_url + "/models", timeout=5).read()
        self.assertEqual(self.provider.calls, 0)

    def test_doubled_v1_prefix_is_rejected(self):
        request = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
            data=json.dumps(messages("PLAN_SYSTEM")).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(self.provider.calls, 0)

    def test_first_planning_reply_is_the_bad_fixture(self):
        response = post_json(self.base_url, messages("PLAN_SYSTEM"))
        content = response["choices"][0]["message"]["content"]
        self.assertEqual(json.loads(content)["summary"], "bad")

    def test_planning_retry_returns_the_good_fixture(self):
        response = post_json(
            self.base_url, messages("PLAN_SYSTEM", retry=PLAN_RETRY)
        )
        content = response["choices"][0]["message"]["content"]
        self.assertEqual(json.loads(content)["summary"], "good")

    def test_first_execution_reply_is_the_bad_fixture(self):
        response = post_json(self.base_url, messages("EXEC_SYSTEM"))
        content = response["choices"][0]["message"]["content"]
        self.assertEqual(content, "bad step text")

    def test_execution_retry_returns_the_good_fixture(self):
        response = post_json(
            self.base_url, messages("EXEC_SYSTEM", retry=STEP_RETRY)
        )
        content = response["choices"][0]["message"]["content"]
        self.assertEqual(content, "good step text")

    def test_validation_returns_the_verdict_json(self):
        response = post_json(self.base_url, messages("VALID_SYSTEM"))
        content = response["choices"][0]["message"]["content"]
        self.assertTrue(json.loads(content)["passed"])

    def test_non_stream_reply_carries_usage_and_timings(self):
        response = post_json(self.base_url, messages("PLAN_SYSTEM"))
        self.assertIn("usage", response)
        self.assertIn("timings", response)
        self.assertEqual(response["model"], "mock-local")
        self.assertIn("time_to_first_token_ms", response["timings"])
        self.assertEqual(
            response["usage"]["total_tokens"],
            response["usage"]["prompt_tokens"] + response["usage"]["completion_tokens"],
        )

    def test_usage_is_deterministic(self):
        first = post_json(self.base_url, messages("PLAN_SYSTEM"))
        second = post_json(self.base_url, messages("PLAN_SYSTEM"))
        self.assertEqual(first["usage"], second["usage"])
        self.assertEqual(first["timings"], second["timings"])

    def test_stream_reply_is_sse_with_usage_and_timings(self):
        text = post_stream(self.base_url, messages("EXEC_SYSTEM", stream=True))
        self.assertIn("data: [DONE]", text)
        events = [
            json.loads(line[len("data:") :].strip())
            for line in text.splitlines()
            if line.startswith("data:") and "[DONE]" not in line
        ]
        self.assertTrue(events)
        content = "".join(
            choice["delta"].get("content", "")
            for event in events
            for choice in event.get("choices", [])
            if choice.get("delta")
        )
        self.assertEqual(content, "bad step text")
        usage_events = [event for event in events if event.get("usage")]
        self.assertTrue(usage_events)
        self.assertIn("timings", usage_events[-1])
        self.assertEqual(usage_events[0]["choices"], [])

    def test_calls_counter_counts_chat_completions_only(self):
        post_json(self.base_url, messages("PLAN_SYSTEM"))
        post_json(self.base_url, messages("EXEC_SYSTEM"))
        self.assertEqual(self.provider.calls, 2)


if __name__ == "__main__":
    unittest.main()
