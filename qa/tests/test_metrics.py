"""Unit tests of the metrics aggregator (``lib.metrics``).

The records are plain dictionaries, so no process or network is involved.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lib.metrics import (
    BACKEND_LLAMA_SERVER,
    BACKEND_MOCK,
    PROVIDER_LLAMA_SERVER,
    PROVIDER_MOCK,
    SOURCE_MEAN_PER_CALL_RATES,
    SOURCE_MIXED,
    SOURCE_PROXY_FIRST_CHUNK,
    SOURCE_PROXY_WALL_CLOCK,
    SOURCE_TIMINGS,
    SOURCE_USAGE,
    aggregate,
    count_failed_calls,
    load_records,
    sanitize_model,
    token_coverage,
)

EXPECTED_KEYS = {
    "provider",
    "model",
    "local_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "ttft",
    "total_seconds",
    "tokens_per_second",
    "network_api_calls",
    "backend",
    "local_model_used",
    "blocked_external_calls",
    "tokens_source",
    "ttft_source",
    "total_seconds_source",
    "tokens_per_second_source",
}


def record(**overrides):
    base = {
        "seq": 1,
        "stream": False,
        "status": 200,
        "duration_ms": 1000.0,
        "first_content_ms": None,
        "model": "local-model",
        "usage": None,
        "timings": None,
        "is_llm_call": True,
        "error": None,
    }
    base.update(overrides)
    return base


class FullRecordTest(unittest.TestCase):
    def setUp(self):
        self.records = [
            record(
                usage={
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
                timings={
                    "prompt_n": 10,
                    "prompt_ms": 20.0,
                    "predicted_n": 5,
                    "predicted_ms": 40.0,
                    "time_to_first_token_ms": 150.0,
                },
            ),
            record(
                seq=2,
                usage={
                    "prompt_tokens": 30,
                    "completion_tokens": 9,
                    "total_tokens": 39,
                },
                timings={
                    "prompt_n": 30,
                    "prompt_ms": 60.0,
                    "predicted_n": 9,
                    "predicted_ms": 90.0,
                    "time_to_first_token_ms": 250.0,
                },
            ),
        ]

    def test_totals_and_sources(self):
        block = aggregate(
            self.records,
            provider=PROVIDER_LLAMA_SERVER,
            model="local-model",
            local_model_used=True,
        )
        self.assertEqual(block["input_tokens"], 40)
        self.assertEqual(block["output_tokens"], 14)
        self.assertEqual(block["total_tokens"], 54)
        self.assertEqual(block["tokens_source"], SOURCE_USAGE)
        self.assertEqual(block["local_calls"], 2)
        self.assertEqual(block["backend"], BACKEND_LLAMA_SERVER)
        self.assertTrue(block["local_model_used"])

    def test_ttft_is_the_mean_of_the_timing_values(self):
        block = aggregate(
            self.records,
            provider=PROVIDER_LLAMA_SERVER,
            model="local-model",
            local_model_used=True,
        )
        self.assertAlmostEqual(block["ttft"]["seconds"], 0.2)
        self.assertEqual(block["ttft"]["source"], SOURCE_TIMINGS)
        self.assertEqual(block["ttft"]["calls"], 2)
        self.assertEqual(block["ttft"]["total_calls"], 2)

    def test_total_seconds_is_the_sum_of_prompt_and_predicted(self):
        block = aggregate(
            self.records,
            provider=PROVIDER_LLAMA_SERVER,
            model="local-model",
            local_model_used=True,
        )
        self.assertAlmostEqual(
            block["total_seconds"]["seconds"], (20 + 40 + 60 + 90) / 1000
        )
        self.assertEqual(block["total_seconds"]["source"], SOURCE_TIMINGS)

    def test_tokens_per_second_uses_timings_only(self):
        block = aggregate(
            self.records,
            provider=PROVIDER_LLAMA_SERVER,
            model="local-model",
            local_model_used=True,
        )
        self.assertAlmostEqual(
            block["tokens_per_second"]["value"], 14 / (130.0 / 1000)
        )
        self.assertEqual(block["tokens_per_second"]["source"], SOURCE_TIMINGS)

    def test_top_level_keys_are_strict(self):
        block = aggregate(
            self.records,
            provider=PROVIDER_LLAMA_SERVER,
            model="local-model",
            local_model_used=True,
        )
        self.assertEqual(set(block), EXPECTED_KEYS)


class PriorityTest(unittest.TestCase):
    def test_usage_wins_over_timings_without_double_counting(self):
        block = aggregate(
            [
                record(
                    usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
                    timings={"prompt_n": 999, "predicted_n": 999},
                )
            ],
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        self.assertEqual(block["input_tokens"], 10)
        self.assertEqual(block["output_tokens"], 4)
        self.assertEqual(block["total_tokens"], 14)

    def test_timings_are_used_when_usage_is_missing(self):
        block = aggregate(
            [record(timings={"prompt_n": 12, "predicted_n": 3})],
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        self.assertEqual(block["input_tokens"], 12)
        self.assertEqual(block["output_tokens"], 3)
        self.assertEqual(block["total_tokens"], 15)
        self.assertEqual(block["tokens_source"], SOURCE_TIMINGS)

    def test_missing_total_is_derived_from_input_and_output(self):
        block = aggregate(
            [
                record(
                    usage={"prompt_tokens": 7, "completion_tokens": 3},
                )
            ],
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        self.assertEqual(block["total_tokens"], 10)

    def test_partial_data_keeps_the_known_sum(self):
        records = [
            record(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
            record(seq=2),
        ]
        block = aggregate(
            records,
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        self.assertEqual(block["input_tokens"], 10)
        coverage = token_coverage(records)
        self.assertEqual(coverage["input"], {"calls": 1, "total": 2})
        self.assertEqual(coverage["total"], {"calls": 1, "total": 2})

    def test_mixed_sources_are_marked(self):
        records = [
            record(usage={"prompt_tokens": 10}),
            record(seq=2, timings={"prompt_n": 4}),
        ]
        block = aggregate(
            records,
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        self.assertEqual(block["tokens_source"], SOURCE_MIXED)


class MissingDataTest(unittest.TestCase):
    def test_no_fields_is_no_data(self):
        block = aggregate(
            [record(duration_ms=None)],
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        self.assertIsNone(block["input_tokens"])
        self.assertIsNone(block["output_tokens"])
        self.assertIsNone(block["total_tokens"])
        self.assertIsNone(block["ttft"]["seconds"])
        self.assertIsNone(block["total_seconds"]["seconds"])
        self.assertIsNone(block["tokens_per_second"]["value"])
        self.assertIsNone(block["tokens_source"])
        self.assertEqual(block["ttft"]["calls"], 0)

    def test_incomplete_call_never_fabricates_tokens(self):
        block = aggregate(
            [
                record(
                    status=None,
                    duration_ms=None,
                    usage=None,
                    timings=None,
                    error="aborted",
                    incomplete=True,
                )
            ],
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        self.assertEqual(block["local_calls"], 1)
        self.assertIsNone(block["input_tokens"])
        self.assertIsNone(block["output_tokens"])
        self.assertIsNone(block["total_tokens"])
        self.assertIsNone(block["ttft"]["seconds"])
        self.assertIsNone(block["total_seconds"]["seconds"])
        self.assertIsNone(block["tokens_per_second"]["value"])
        self.assertEqual(count_failed_calls([record(status=None, error="aborted")]), 1)

    def test_empty_log_yields_no_data(self):
        block = aggregate(
            [],
            provider=PROVIDER_MOCK,
            model="mock-local",
            local_model_used=False,
        )
        self.assertEqual(block["local_calls"], 0)
        self.assertEqual(block["backend"], BACKEND_MOCK)
        self.assertIsNone(block["input_tokens"])


class TtftFallbackTest(unittest.TestCase):
    def test_prompt_ms_is_never_used_as_ttft(self):
        block = aggregate(
            [record(timings={"prompt_ms": 500.0, "predicted_ms": 100.0})],
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        self.assertIsNone(block["ttft"]["seconds"])
        self.assertAlmostEqual(block["total_seconds"]["seconds"], 0.6)

    def test_proxy_first_chunk_is_the_streaming_fallback(self):
        block = aggregate(
            [record(stream=True, first_content_ms=42.0)],
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        self.assertAlmostEqual(block["ttft"]["seconds"], 0.042)
        self.assertEqual(block["ttft"]["source"], SOURCE_PROXY_FIRST_CHUNK)

    def test_total_seconds_falls_back_to_the_proxy_wall_clock(self):
        block = aggregate(
            [record(duration_ms=250.0)],
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        self.assertAlmostEqual(block["total_seconds"]["seconds"], 0.25)
        self.assertEqual(block["total_seconds"]["source"], SOURCE_PROXY_WALL_CLOCK)


class TokensPerSecondTest(unittest.TestCase):
    def test_wall_clock_is_never_used_for_the_rate(self):
        block = aggregate(
            [
                record(
                    usage={"completion_tokens": 100},
                    duration_ms=1000.0,
                )
            ],
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        self.assertIsNone(block["tokens_per_second"]["value"])

    def test_per_call_rates_are_averaged(self):
        records = [
            record(timings={"predicted_per_second": 10.0}),
            record(seq=2, timings={"predicted_per_second": 20.0}),
        ]
        block = aggregate(
            records,
            provider=PROVIDER_LLAMA_SERVER,
            model="m",
            local_model_used=True,
        )
        self.assertAlmostEqual(block["tokens_per_second"]["value"], 15.0)
        self.assertEqual(
            block["tokens_per_second"]["source"], SOURCE_MEAN_PER_CALL_RATES
        )


class SanitizeModelTest(unittest.TestCase):
    def test_paths_are_reduced_to_the_basename(self):
        self.assertEqual(sanitize_model(r"C:\models\qwen.gguf"), "qwen.gguf")
        self.assertEqual(sanitize_model("/opt/models/qwen.gguf"), "qwen.gguf")
        self.assertEqual(sanitize_model("plain-model"), "plain-model")
        self.assertEqual(sanitize_model(None), "")

    def test_aggregate_sanitizes_the_model(self):
        block = aggregate(
            [],
            provider=PROVIDER_LLAMA_SERVER,
            model="/models/qwen/model.gguf",
            local_model_used=True,
        )
        self.assertEqual(block["model"], "model.gguf")


class FailedCallsTest(unittest.TestCase):
    def test_error_status_and_transport_error_are_counted(self):
        records = [
            record(),
            record(seq=2, status=502, error="ConnectionRefusedError"),
            record(seq=3, status=500),
        ]
        self.assertEqual(count_failed_calls(records), 2)


class LoadRecordsTest(unittest.TestCase):
    def test_records_are_read_and_corrupt_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm_calls.jsonl"
            path.write_text(
                '{"is_llm_call": true, "usage": {"prompt_tokens": 1}}\n'
                "not json\n"
                "\n"
                '{"is_llm_call": false}\n',
                encoding="utf-8",
            )
            records = load_records(path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["usage"]["prompt_tokens"], 1)

    def test_missing_file_yields_no_records(self):
        self.assertEqual(load_records(Path("definitely-missing.jsonl")), [])


if __name__ == "__main__":
    unittest.main()
