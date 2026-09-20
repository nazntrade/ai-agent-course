"""Aggregator of the local-LLM call metrics.

Pure stdlib: the module only reads the metadata lines written by the recording
proxy. Token counts come from the provider's real ``usage`` first and from the
llama-server ``timings`` second; the two are never mixed inside one call.
Missing data is reported as "no data" instead of being estimated from text.

Time-to-first-token is taken from the llama-server ``timings`` when present and
otherwise from the proxy's wall clock to the first content chunk (streaming
only); ``prompt_ms`` is never substituted for it. Tokens per second is computed
from ``timings`` only and never from the proxy wall clock.
"""

from __future__ import annotations

import json
from pathlib import Path

NO_DATA = "нет данных"

PROVIDER_LLAMA_SERVER = "llama.cpp (llama-server)"
PROVIDER_MOCK = "mock (loopback stub)"
BACKEND_LLAMA_SERVER = "llama-server"
BACKEND_MOCK = "mock"

METRICS_SOURCE_LIVE = "llama-server /v1 (real usage/timings)"
METRICS_SOURCE_MOCK = "MOCK provider — synthetic values, NOT a real local model"

SOURCE_USAGE = "usage"
SOURCE_TIMINGS = "timings"
SOURCE_DERIVED = "derived"
SOURCE_MIXED = "mixed"
SOURCE_PROXY_FIRST_CHUNK = "proxy_first_chunk"
SOURCE_PROXY_WALL_CLOCK = "proxy_wall_clock"
SOURCE_MEAN_PER_CALL_RATES = "mean of per-call rates"

# Candidate fields of the time-to-first-token value inside llama-server timings.
TTFT_TIMING_FIELDS = ("time_to_first_token_ms",)
TOTAL_TIME_TIMING_FIELDS = ("prompt_ms", "predicted_ms")


def load_records(log_path) -> list:
    """Return the parsed LLM call records; a corrupt line is skipped."""
    path = Path(log_path)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("is_llm_call", True):
            records.append(entry)
    return records


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mapping(record, key) -> dict:
    value = record.get(key)
    return value if isinstance(value, dict) else {}


def _call_input(record):
    """Return ``(value, source)`` of the input tokens of one call."""
    usage = _mapping(record, "usage")
    value = _num(usage.get("prompt_tokens"))
    if value is not None:
        return value, SOURCE_USAGE
    timings = _mapping(record, "timings")
    value = _num(timings.get("prompt_n"))
    if value is not None:
        return value, SOURCE_TIMINGS
    return None, None


def _call_output(record):
    """Return ``(value, source)`` of the output tokens of one call."""
    usage = _mapping(record, "usage")
    value = _num(usage.get("completion_tokens"))
    if value is not None:
        return value, SOURCE_USAGE
    timings = _mapping(record, "timings")
    value = _num(timings.get("predicted_n"))
    if value is not None:
        return value, SOURCE_TIMINGS
    return None, None


def _call_total(record, input_value, output_value):
    """Return ``(value, source)`` of the total tokens of one call."""
    usage = _mapping(record, "usage")
    value = _num(usage.get("total_tokens"))
    if value is not None:
        return value, SOURCE_USAGE
    if input_value is not None and output_value is not None:
        return input_value + output_value, SOURCE_DERIVED
    return None, None


def _call_ttft(record):
    """Return ``(seconds, source)`` of the time to the first token."""
    timings = _mapping(record, "timings")
    for field in TTFT_TIMING_FIELDS:
        value = _num(timings.get(field))
        if value is not None:
            return value / 1000.0, SOURCE_TIMINGS
    value = _num(record.get("first_content_ms"))
    if value is not None:
        return value / 1000.0, SOURCE_PROXY_FIRST_CHUNK
    return None, None


def _call_total_seconds(record):
    """Return ``(seconds, source)`` of the full call duration."""
    timings = _mapping(record, "timings")
    parts = [_num(timings.get(field)) for field in TOTAL_TIME_TIMING_FIELDS]
    if all(part is not None for part in parts):
        return sum(parts) / 1000.0, SOURCE_TIMINGS
    value = _num(record.get("duration_ms"))
    if value is not None:
        return value / 1000.0, SOURCE_PROXY_WALL_CLOCK
    return None, None


def _combine_sources(sources) -> str | None:
    known = {source for source in sources if source is not None}
    if not known:
        return None
    if len(known) == 1:
        return known.pop()
    return SOURCE_MIXED


def _sum_known(values):
    known = [value for value in values if value is not None]
    if not known:
        return None, 0
    return sum(known), len(known)


def sanitize_model(model) -> str:
    """Return the basename of a model name (llama-server may report a path)."""
    text = str(model or "").strip()
    if not text:
        return ""
    for separator in ("\\", "/"):
        if separator in text:
            text = text.rsplit(separator, 1)[-1]
    return text.strip()


def _timings_series(records, field) -> list:
    values = []
    for record in records:
        value = _num(_mapping(record, "timings").get(field))
        if value is not None:
            values.append(value)
    return values


def _tokens_per_second(records):
    """Return ``(value, source)`` of the tokens-per-second rate."""
    predicted_n = _timings_series(records, "predicted_n")
    predicted_ms = _timings_series(records, "predicted_ms")
    if predicted_n and predicted_ms and sum(predicted_ms) > 0:
        return sum(predicted_n) / (sum(predicted_ms) / 1000.0), SOURCE_TIMINGS
    rates = _timings_series(records, "predicted_per_second")
    if rates:
        return sum(rates) / len(rates), SOURCE_MEAN_PER_CALL_RATES
    return None, None


def _rate(field, records, *, aggregate):
    """Aggregate the ttft/total-seconds series over the calls that have data.

    ``aggregate`` is ``"mean"`` for the time-to-first-token and ``"sum"`` for
    the full duration.
    """
    values = []
    sources = []
    for record in records:
        value, source = field(record)
        if value is None:
            continue
        values.append(value)
        sources.append(source)
    source = _combine_sources(sources)
    if not values:
        return {"value": None, "source": None, "calls": 0}, source
    if aggregate == "sum":
        total = sum(values)
    else:
        total = sum(values) / len(values)
    return {"value": total, "source": source, "calls": len(values)}, source


def count_failed_calls(records) -> int:
    """Number of calls that ended with an error status or transport error."""
    failed = 0
    for record in records:
        status = record.get("status")
        if record.get("error") or (isinstance(status, int) and status >= 400):
            failed += 1
    return failed


def token_coverage(records) -> dict:
    """Return the per-field call coverage used by the report lines."""
    coverage = {}
    for name, field in (
        ("input", _call_input),
        ("output", _call_output),
    ):
        calls = 0
        for record in records:
            value, _source = field(record)
            if value is not None:
                calls += 1
        coverage[name] = {"calls": calls, "total": len(records)}
    totals = 0
    for record in records:
        input_value, _ = _call_input(record)
        output_value, _ = _call_output(record)
        value, _source = _call_total(record, input_value, output_value)
        if value is not None:
            totals += 1
    coverage["total"] = {"calls": totals, "total": len(records)}
    return coverage


def aggregate(
    records,
    *,
    provider,
    model,
    local_model_used,
    blocked_external_calls=0,
    network_api_calls=0,
) -> dict:
    """Build the strict ``local_llm_metrics`` block from the call records.

    Every value comes from the provider's real fields; a field that no call
    reported stays ``None``. ``total_tokens`` may be derived from the input and
    output of the same call; ``usage`` and ``timings`` are never added together.
    """
    records = list(records or [])
    input_values = []
    output_values = []
    total_values = []
    input_sources = []
    output_sources = []
    total_sources = []
    for record in records:
        input_value, input_source = _call_input(record)
        output_value, output_source = _call_output(record)
        total_value, total_source = _call_total(record, input_value, output_value)
        input_values.append(input_value)
        output_values.append(output_value)
        total_values.append(total_value)
        input_sources.append(input_source)
        output_sources.append(output_source)
        total_sources.append(total_source)

    input_total, _ = _sum_known(input_values)
    output_total, _ = _sum_known(output_values)
    grand_total, _ = _sum_known(total_values)

    ttft, ttft_source = _rate(lambda record: _call_ttft(record), records, aggregate="mean")
    duration, duration_source = _rate(
        lambda record: _call_total_seconds(record), records, aggregate="sum"
    )
    rate, rate_source = _tokens_per_second(records)

    mapped_model = sanitize_model(model)
    if not mapped_model and records:
        for record in records:
            if record.get("model"):
                mapped_model = sanitize_model(record["model"])
                break

    return {
        "provider": str(provider),
        "model": mapped_model,
        "local_calls": len(records),
        "input_tokens": input_total,
        "output_tokens": output_total,
        "total_tokens": grand_total,
        "ttft": {
            "seconds": ttft["value"],
            "source": ttft["source"],
            "calls": ttft["calls"],
            "total_calls": len(records),
        },
        "total_seconds": {
            "seconds": duration["value"],
            "source": duration["source"],
            "calls": duration["calls"],
            "total_calls": len(records),
        },
        "tokens_per_second": {
            "value": rate,
            "source": rate_source,
            "calls": len(_timings_series(records, "predicted_ms"))
            or len(_timings_series(records, "predicted_per_second")),
            "total_calls": len(records),
        },
        "network_api_calls": int(network_api_calls),
        "backend": (
            BACKEND_LLAMA_SERVER
            if provider == PROVIDER_LLAMA_SERVER
            else BACKEND_MOCK
        ),
        "local_model_used": bool(local_model_used),
        "blocked_external_calls": int(blocked_external_calls),
        # A derived total is computed from the same call's own values, so it is
        # not a separate data source and must not turn a clean source into
        # "mixed".
        "tokens_source": _combine_sources(
            [
                source
                for source in input_sources + output_sources + total_sources
                if source != SOURCE_DERIVED
            ]
        ),
        "ttft_source": ttft_source,
        "total_seconds_source": duration_source,
        "tokens_per_second_source": rate_source,
    }
