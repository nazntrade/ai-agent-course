"""Report building and rendering of one E2E run.

The report is written both as ``report.json`` (machine-readable) and
``report.md`` (human-readable). The metric lines of the Markdown report follow
the exact contract of the task: a missing field is rendered as "no data" and is
never estimated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from lib.metrics import (
    METRICS_SOURCE_LIVE,
    METRICS_SOURCE_MOCK,
    NO_DATA,
    SOURCE_DERIVED,
    SOURCE_MEAN_PER_CALL_RATES,
    SOURCE_MIXED,
    SOURCE_PROXY_FIRST_CHUNK,
    SOURCE_PROXY_WALL_CLOCK,
    SOURCE_TIMINGS,
    SOURCE_USAGE,
    token_coverage,
)

PASS = "PASS"
FAIL = "FAIL"
SKIPPED = "SKIPPED"


@dataclass
class Check:
    """One named pass/fail/skip check of the run."""

    name: str
    status: str
    detail: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def _fmt_tokens(value) -> str:
    if value is None:
        return NO_DATA
    return str(int(value))


def _fmt_number(value, digits=3) -> str:
    if value is None:
        return NO_DATA
    return f"{float(value):.{digits}f}"


def _token_source_label(source, *, live) -> str:
    if not live:
        return "synthetic mock usage"
    return {
        SOURCE_USAGE: "llama-server usage",
        SOURCE_TIMINGS: "llama-server timings",
        SOURCE_DERIVED: "derived from prompt+completion (llama-server)",
        SOURCE_MIXED: "mixed usage/timings",
    }.get(source, NO_DATA)


def _ttft_source_label(source, *, live) -> str:
    if not live and source == SOURCE_TIMINGS:
        return "synthetic mock timings"
    if source == SOURCE_TIMINGS:
        return "llama-server timings"
    if source == SOURCE_PROXY_FIRST_CHUNK:
        return "proxy wall-clock first chunk"
    if source == SOURCE_MIXED:
        return "mixed sources"
    return NO_DATA


def _seconds_source_label(source, *, live) -> str:
    if not live and source == SOURCE_TIMINGS:
        return "synthetic mock timings"
    if source == SOURCE_TIMINGS:
        return "llama-server timings"
    if source == SOURCE_PROXY_WALL_CLOCK:
        return "proxy wall-clock"
    if source == SOURCE_MIXED:
        return "mixed sources"
    return NO_DATA


def _rate_source_label(source, *, live) -> str:
    if not live and source == SOURCE_TIMINGS:
        return "synthetic mock timings"
    if source == SOURCE_TIMINGS:
        return "llama-server timings"
    if source == SOURCE_MEAN_PER_CALL_RATES:
        return "mean of per-call rates"
    if source == SOURCE_MIXED:
        return "mixed sources"
    return NO_DATA


def metric_lines(metrics, coverage, *, live, failed_calls=0, incomplete_calls=0) -> list:
    """Render the metric lines of the report in the required order."""
    lines = [f"Provider: {metrics['provider']}"]
    lines.append(f"Model: {metrics['model'] or NO_DATA}")
    if live:
        lines.append(
            f"Local LLM calls: {metrics['local_calls']} (failed: {failed_calls})"
        )
    else:
        lines.append("Local model used: NO")
        lines.append(f"Mock LLM calls: {metrics['local_calls']} (synthetic)")
    if incomplete_calls:
        lines.append(
            f"Incomplete LLM calls: {incomplete_calls} "
            "(aborted before the response completed; tokens stay unknown)"
        )

    for name, label in (
        ("input", "Input tokens"),
        ("output", "Output tokens"),
        ("total", "Total tokens"),
    ):
        value = metrics[f"{name}_tokens"]
        if value is None:
            lines.append(f"{label}: {NO_DATA}")
            continue
        cover = coverage.get(name) or {"calls": 0, "total": metrics["local_calls"]}
        lines.append(
            f"{label}: {_fmt_tokens(value)} "
            f"({_token_source_label(metrics.get('tokens_source'), live=live)}; "
            f"{cover['calls']}/{cover['total']} calls)"
        )

    ttft = metrics["ttft"]
    if ttft.get("seconds") is None:
        lines.append(f"TTFT: {NO_DATA}")
    else:
        lines.append(
            f"TTFT: {_fmt_number(ttft['seconds'])} s "
            f"(avg; {_ttft_source_label(ttft.get('source'), live=live)}; "
            f"{ttft.get('calls', 0)}/{ttft.get('total_calls', 0)} calls)"
        )

    duration = metrics["total_seconds"]
    if duration.get("seconds") is None:
        lines.append(f"Total time: {NO_DATA}")
    else:
        lines.append(
            f"Total time: {_fmt_number(duration['seconds'])} s "
            f"(sum; {_seconds_source_label(duration.get('source'), live=live)}; "
            f"{duration.get('calls', 0)}/{duration.get('total_calls', 0)} calls)"
        )

    rate = metrics["tokens_per_second"]
    if rate.get("value") is None:
        lines.append(f"Tokens/s: {NO_DATA}")
    else:
        lines.append(
            f"Tokens/s: {_fmt_number(rate['value'], 2)} "
            f"({_rate_source_label(rate.get('source'), live=live)}; "
            f"{rate.get('calls', 0)}/{rate.get('total_calls', 0)} calls)"
        )

    lines.append(f"Network API calls: {metrics['network_api_calls']}")
    lines.append(
        f"Metrics source: {METRICS_SOURCE_LIVE if live else METRICS_SOURCE_MOCK}"
    )
    return lines


def check_local_metrics_recorded(metrics, records, *, live=False) -> Check:
    """The metrics block exists, its counters match the log and sources exist.

    A live run with an empty call log never passes: zero recorded calls means the
    local model was never observed, even though the run claims to be live. An
    incomplete (aborted) call is counted and reported, but it does not let the
    check pass, so an interrupted run cannot be mistaken for a complete one.
    """
    if not isinstance(metrics, dict) or not metrics:
        return Check("local_metrics_recorded", FAIL, "no local_llm_metrics block")
    records = list(records or [])
    incomplete = [record for record in records if record.get("incomplete")]
    if live and not records:
        return Check(
            "local_metrics_recorded",
            FAIL,
            "live run recorded 0 local LLM calls: no llm_calls.jsonl entry "
            "exists, so no real model call was observed",
        )
    if incomplete:
        return Check(
            "local_metrics_recorded",
            FAIL,
            f"{len(records)} local calls recorded, {len(incomplete)} incomplete "
            "(aborted before the response completed); their tokens stay unknown",
        )
    problems = []
    if int(metrics.get("local_calls", -1)) != len(records):
        problems.append(
            f"local_calls={metrics.get('local_calls')} but the log has {len(records)}"
        )
    if metrics.get("input_tokens") is not None and metrics.get("tokens_source") is None:
        problems.append("input tokens have no source")
    if metrics.get("ttft", {}).get("seconds") is not None and not metrics["ttft"].get(
        "source"
    ):
        problems.append("ttft has no source")
    if metrics.get("total_seconds", {}).get("seconds") is not None and not metrics[
        "total_seconds"
    ].get("source"):
        problems.append("total time has no source")
    if metrics.get("tokens_per_second", {}).get("value") is not None and not metrics[
        "tokens_per_second"
    ].get("source"):
        problems.append("tokens/s has no source")
    if problems:
        return Check("local_metrics_recorded", FAIL, "; ".join(problems))
    return Check(
        "local_metrics_recorded",
        PASS,
        f"{metrics['local_calls']} local calls recorded with sources",
    )


def check_network_api_calls_zero(network_api_calls) -> Check:
    """A hard pass only when the run made zero network API calls."""
    count = int(network_api_calls or 0)
    if count == 0:
        return Check("network_api_calls_zero", PASS, "Network API calls: 0")
    return Check(
        "network_api_calls_zero", FAIL, f"Network API calls: {count}"
    )


def check_llm_calls_consistency(local_calls, task_attempts, *, live) -> Check:
    """In MOCK the recorded calls must equal the task attempts; live is skipped."""
    if live:
        return Check(
            "llm_calls_consistency",
            SKIPPED,
            "a real local model owns its own call count",
        )
    if int(local_calls) == int(task_attempts):
        return Check(
            "llm_calls_consistency",
            PASS,
            f"local calls {local_calls} == task attempts {task_attempts}",
        )
    return Check(
        "llm_calls_consistency",
        FAIL,
        f"local calls {local_calls} != task attempts {task_attempts}",
    )


def overall_status(checks) -> str:
    """The run passes only when no check failed."""
    if any(check.status == FAIL for check in checks):
        return FAIL
    return PASS


def build_report(
    *,
    mode,
    provider_plan,
    metrics,
    records,
    checks,
    scenario,
    isolation,
    run_dir,
    failed_calls=0,
    discovery=None,
    extra=None,
) -> dict:
    """Assemble the report dictionary."""
    checks = list(checks or [])
    coverage = token_coverage(records)
    incomplete_calls = sum(1 for record in records if record.get("incomplete"))
    live = str(provider_plan.kind) == "local"
    report = {
        "mode": str(mode).upper(),
        "kind": provider_plan.kind,
        "status": overall_status(checks),
        "live_local_llm": {
            "status": "OK" if provider_plan.live_local_llm else "SKIPPED",
            "reason": provider_plan.reason,
        },
        "provider": {
            "kind": provider_plan.kind,
            "base_url": provider_plan.base_url,
            "model": provider_plan.model,
            "spawned": bool(provider_plan.spawned),
            "reason": provider_plan.reason,
        },
        "local_llm_metrics": metrics,
        "metric_lines": metric_lines(
            metrics,
            coverage,
            live=live,
            failed_calls=failed_calls,
            incomplete_calls=incomplete_calls,
        ),
        "checks": [check.as_dict() for check in checks],
        "scenario": scenario,
        "isolation": isolation,
        "run_dir": str(run_dir),
        "artifacts": {},
    }
    if discovery:
        report["discovery"] = discovery
    if extra:
        report.update(extra)
    return report


def render_markdown(report) -> str:
    """Render the human-readable report."""
    lines = ["# Local LLM E2E report", ""]
    lines.append(f"- Mode: {report.get('mode')}")
    lines.append(f"- Result: {report.get('status')}")
    provider = report.get("live_local_llm", {})
    live_status = provider.get("status", "SKIPPED")
    if provider.get("reason"):
        lines.append(f"- LIVE_LOCAL_LLM: {live_status} ({provider['reason']})")
    else:
        lines.append(f"- LIVE_LOCAL_LLM: {live_status}")
    lines.append(f"- Run directory: `{report.get('run_dir')}`")
    lines.append("")

    discovery = report.get("discovery")
    if discovery:
        lines.append("## Discovery")
        lines.append("")
        lines.append(f"- Source: {discovery.get('source')}")
        lines.append(f"- Launcher: {discovery.get('launcher')}")
        lines.append(f"- Config written: {discovery.get('config_written')}")
        lines.append(f"- Config path: `{discovery.get('config_path')}`")
        lines.append(
            "- Endpoint running before the run: "
            f"{discovery.get('endpoint_running_before_run')}"
        )
        lines.append(f"- Model source: {discovery.get('model_source')}")
        lines.append(
            f"- Candidates considered: {discovery.get('candidates_considered')}"
        )
        lines.append(f"- Search elapsed: {discovery.get('search_elapsed_ms')} ms")
        for warning in discovery.get("warnings", []) or []:
            lines.append(f"- Warning: {warning}")
        lines.append("")

    session = report.get("session")
    if session:
        lines.append("## Session")
        lines.append("")
        lines.append(f"- Mode: {session.get('mode')}")
        lines.append(f"- Session id: {session.get('session_id')}")
        lines.append(f"- Session started: {session.get('session_started')}")
        lines.append(f"- Session stopped: {session.get('session_stopped')}")
        lines.append(f"- Adopted: {session.get('adopted')}")
        lines.append(f"- Endpoint owner: {session.get('endpoint_owner')}")
        lines.append(f"- Model loads: {session.get('model_loads')}")
        lines.append(f"- Live runs: {session.get('live_runs')}")
        lines.append(f"- Left running: {session.get('left_running')}")
        lines.append(f"- Stage: {session.get('stage')}")
        lines.append(f"- Started at: {session.get('started_at')}")
        lines.append("")

    lines.append("## Local model metrics")
    lines.append("")
    for line in report.get("metric_lines", []):
        lines.append(f"- {line}")
    lines.append("")

    lines.append("## Checks")
    lines.append("")
    lines.append("| Check | Status | Detail |")
    lines.append("| --- | --- | --- |")
    for check in report.get("checks", []):
        lines.append(
            f"| {check.get('name')} | {check.get('status')} | {check.get('detail', '')} |"
        )
    lines.append("")

    scenario = report.get("scenario") or {}
    lines.append("## Scenario")
    lines.append("")
    lines.append(f"- Task id: {scenario.get('task_id')}")
    lines.append(f"- Events: {', '.join(scenario.get('events', []) or [])}")
    lines.append(
        f"- Step attempts: {scenario.get('step_attempts', [])}"
    )
    lines.append(f"- Plan attempts: {scenario.get('plan_attempts')}")
    lines.append(
        f"- Transition attempts: {scenario.get('transition_attempt_count')}"
    )
    for note in scenario.get("notes", []) or []:
        lines.append(f"- {note}")
    lines.append("")

    isolation = report.get("isolation") or {}
    lines.append("## Isolation")
    lines.append("")
    lines.append(f"- Status: {isolation.get('status')}")
    lines.append(f"- Detail: {isolation.get('reason', '')}")
    lines.append("")

    artifacts = report.get("artifacts") or {}
    if artifacts:
        lines.append("## Artifacts")
        lines.append("")
        for name, path in artifacts.items():
            lines.append(f"- {name}: `{path}`")
        lines.append("")
    return "\n".join(lines)


def write_report(run_dir, report) -> dict:
    """Write ``report.json`` and ``report.md`` into the run directory."""
    json_path = Path(run_dir) / "report.json"
    md_path = Path(run_dir) / "report.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return {"report_json": str(json_path), "report_md": str(md_path)}
