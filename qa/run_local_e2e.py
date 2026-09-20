"""Single entry point of the isolated local-model E2E runtime.

Usage::

    python run_local_e2e.py [AUTO|LOCAL|MOCK|NETWORK|SESSION_START|SESSION_STOP]

Exit codes: ``0`` PASS, ``1`` FAIL, ``2`` prerequisite (local runtime, port or
dependency), ``3`` refused NETWORK mode.

``SESSION_START`` starts the local model once and keeps it running;
``LOCAL``/``AUTO`` then join the same process instead of reloading the model, and
``SESSION_STOP`` releases only the process tree this runner owns.

No network access: the runner makes no external LLM/API calls and no owner
``.env``/database access. All model traffic stays on loopback (the local server
or the loopback mock), so the run itself is fully isolated. The only exception
is the very first setup, where a missing dependency installation may still use
the internet through pip.

In ``AUTO`` and ``LOCAL`` the runner uses the owner's local launcher when an
explicit narrow source is available (``QA_LOCAL_LLM_LAUNCHER`` or
``QA_LOCAL_LLM_SEARCH_ROOTS``) or an untracked config exists; disks and user
folders are never scanned. It auto-creates the untracked
``qa/local.llm.local.json`` only after a model started by an explicit launcher
source is confirmed live.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from dataclasses import replace
from pathlib import Path

_QA_ROOT = Path(__file__).resolve().parent
if str(_QA_ROOT) not in sys.path:
    sys.path.insert(0, str(_QA_ROOT))

from lib import config as config_module  # noqa: E402
from lib import discovery  # noqa: E402
from lib import live_session  # noqa: E402
from lib import local_llm  # noqa: E402
from lib import metrics as metrics_module  # noqa: E402
from lib import modes  # noqa: E402
from lib.app_process import AppProcess, port_is_free  # noqa: E402
from lib.browser import open_session  # noqa: E402
from lib.isolation import IsolationGuard, RunDir, assert_run_db_path  # noqa: E402
from lib.paths import (  # noqa: E402
    APP_DB_PATH,
    DEFAULT_APP_PORT,
    LOCAL_CONFIG_PATH,
    LOCAL_CONFIG_RELATIVE,
    OWNER_ENV_PATH,
    RUNS_DIR,
    SESSION_STATE_PATH,
)
from lib.llm_recorder import RecordingProxy  # noqa: E402
from lib.mock_provider import MockProvider  # noqa: E402
from lib.report import (  # noqa: E402
    FAIL,
    PASS,
    SKIPPED,
    Check,
    build_report,
    check_llm_calls_consistency,
    check_local_metrics_recorded,
    check_network_api_calls_zero,
    write_report,
)
from integrations.memory_state_agent import recipe, scenario  # noqa: E402

EXIT_PASS = modes.EXIT_PASS
EXIT_FAIL = modes.EXIT_FAIL
EXIT_PREREQUISITE = modes.EXIT_PREREQUISITE
EXIT_NETWORK_REFUSED = modes.EXIT_NETWORK_REFUSED


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="run_local_e2e",
        description="Isolated local-LLM browser E2E of week-03/memory-state-agent.",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default=modes.MODE_AUTO,
        help=(
            "AUTO (default), LOCAL, MOCK, NETWORK, SESSION_START or SESSION_STOP"
        ),
    )
    parser.add_argument("--app-port", type=int, default=DEFAULT_APP_PORT)
    parser.add_argument("--headless", dest="headless", action="store_true", default=True)
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    parser.add_argument(
        "--no-discovery",
        dest="no_discovery",
        action="store_true",
        help="do not search for the local launcher; use the configured values only",
    )
    parser.add_argument(
        "--local-config",
        dest="local_config",
        default=None,
        help=(
            "explicit path of the local configuration; discovery may read it but "
            "never writes it"
        ),
    )
    return parser.parse_args(argv)


def _print(message) -> None:
    print(message, flush=True)


def resolve_scenario_timeout_ms(live, config) -> int:
    """Browser-recipe budget of one run.

    A live local model may spend minutes on prefill and generation, so a live run
    uses the generous budget resolved from the configuration
    (``live_timeout_seconds``, overridable through the environment). The MOCK
    provider keeps the recipe's short deterministic budget unchanged: only live
    runs get the long timeout.
    """
    if not live:
        return recipe.DEFAULT_TIMEOUT_MS
    configured = int(getattr(config, "live_timeout_seconds", 0) or 0)
    if configured <= 0:
        configured = config_module.DEFAULT_LIVE_TIMEOUT_SECONDS
    return configured * 1000


def _live_line(plan) -> str:
    if plan.live_local_llm:
        return f"LIVE_LOCAL_LLM: OK (provider={plan.kind}, model={plan.model or 'unknown'})"
    reason = plan.reason or "local runtime is not available"
    return f"LIVE_LOCAL_LLM: SKIPPED ({reason})"


def _launcher(config, run_dir):
    launcher = local_llm.LocalLlmLauncher(
        config, log_path=run_dir.logs_dir / "local_llm.log"
    )

    def launch():
        return launcher.start() and launcher.wait_ready()

    return launcher, launch


def _write_discovered_config(
    result, handoff, *, live_local_llm, spawned, base_url="", model=""
) -> str:
    """Create the untracked config once the local model is confirmed live."""
    if not (result.write_allowed and live_local_llm and result.pending_config):
        pending = bool(result.pending_config and result.write_allowed)
        return "pending" if pending else "no"
    if handoff.found and spawned:
        payload = discovery.build_launcher_payload(
            base_url,
            model,
            handoff.launch_command,
            handoff.launch_cwd,
        )
    else:
        payload = dict(result.pending_config)
    written = discovery.write_local_config(LOCAL_CONFIG_PATH, payload, tmp_dir=RUNS_DIR)
    return "yes" if written else "no"


def _screenshot_paths(run_dir) -> list:
    """List the screenshots actually written, even when the recipe failed."""
    try:
        return [
            str(path) for path in sorted(run_dir.screenshots_dir.glob("*.png"))
        ]
    except OSError:
        return []


def _active_session(mode):
    """Return the active live-session state, or ``None``.

    Only the live modes (AUTO/LOCAL) may join a session. MOCK and NETWORK never
    read the session state, so they can neither create nor adopt a live session.
    """
    if mode not in (modes.MODE_AUTO, modes.MODE_LOCAL):
        return None
    state = live_session.read_state(SESSION_STATE_PATH)
    if state is None:
        return None
    if state.stage not in (live_session.STAGE_ACTIVE, live_session.STAGE_KEPT):
        return None
    if state.endpoint_owner not in live_session.OWNERS:
        return None
    return state


def _session_launch(launch):
    """Refuse to start a second model while a session owns the endpoint."""

    def guarded():
        raise modes.PrerequisiteError(
            "the active live session owns the local model and the endpoint does "
            "not answer; run SESSION_STOP before starting a new model"
        )

    return guarded


def _endpoint_answers(port) -> bool:
    """Best-effort loopback probe used only to verify that a stop went silent."""
    if not port:
        return False
    try:
        base_url = f"http://127.0.0.1:{int(port)}/v1"
    except (TypeError, ValueError):
        return False
    return local_llm.probe(base_url) is not None


def _print_session_start(result) -> None:
    state = result.state
    if result.exit_code != 0:
        _print(f"SESSION: failed reason={result.reason}")
        return
    if result.started:
        kind = "started"
    elif result.adopted:
        kind = "adopted"
    elif result.external:
        kind = "external"
    else:
        # The endpoint answers but its ownership is not provable: it is joined
        # without claiming it.
        kind = "joined"
    _print(
        "SESSION: %s endpoint_owner=%s model_loads=%s session_id=%s"
        % (
            kind,
            state.endpoint_owner if state is not None else "unknown",
            state.model_loads if state is not None else 0,
            state.session_id if state is not None else "none",
        )
    )


def _session_block(session_state, *, live, joined):
    if session_state is not None and live:
        return live_session.report_block(
            session_state, adopted=bool(joined), mode="session"
        )
    if live:
        return live_session.report_block(None, mode="per-run")
    return live_session.report_block(None, mode="none")


def _run_session_start(args, config, handoff, discovery_result, run_dir) -> int:
    """Start the local model once and keep it for later runs."""
    launcher, _launch = _launcher(config, run_dir)
    port = live_session.port_from_base_url(config.base_url)
    try:
        result = live_session.start_session(
            path=SESSION_STATE_PATH,
            base_url=config.base_url,
            port=port,
            model=config.model or "",
            launcher=launcher,
            probe=lambda: launcher.probe(),
        )
    except Exception as exc:  # noqa: BLE001 - reported as a prerequisite failure
        launcher.stop()
        _print(f"PREREQUISITE_ERROR: {type(exc).__name__}: {exc}")
        return EXIT_PREREQUISITE

    _print_session_start(result)
    if result.exit_code != 0:
        _print(f"PREREQUISITE_ERROR: {result.reason}")
        return result.exit_code

    if discovery_result is not None:
        discovery_config_written = _write_discovered_config(
            discovery_result,
            handoff,
            live_local_llm=result.started,
            spawned=result.started,
            base_url=config.base_url,
            model=config.model or "",
        )
        _print(f"DISCOVERY_CONFIG: written={discovery_config_written}")
    return EXIT_PASS


def _run_session_stop(args, run_dir) -> int:
    """Release only the process tree this runner owns."""
    try:
        result = live_session.stop_session(
            path=SESSION_STATE_PATH,
            env=os.environ,
            listeners_fn=local_llm.listener_pids,
            start_time_fn=local_llm.process_start_time,
            probe=_endpoint_answers,
        )
    except Exception as exc:  # noqa: BLE001 - reported as a prerequisite failure
        _print(f"SESSION_STOP: failed reason={type(exc).__name__}: {exc}")
        return EXIT_PREREQUISITE
    verb = "stopped" if result.stopped else "left_running"
    _print(f"SESSION_STOP: {verb} reason={result.reason}")
    return result.exit_code


def main(argv=None) -> int:
    """Resolve the mode, run one scenario and return its exit code.

    The exit code is also written to ``exit_code.txt`` inside the run directory,
    so a run can be audited after the console output is gone.
    """
    args = parse_args(argv)

    try:
        mode = modes.normalize_mode(args.mode)
    except modes.PrerequisiteError as exc:
        _print(f"PREREQUISITE_ERROR: {exc}")
        return EXIT_PREREQUISITE

    run_dir = RunDir(mode).create()
    _print(f"RUN_DIR: {run_dir.path}")
    code = _run(args, mode, run_dir)
    try:
        (run_dir.path / "exit_code.txt").write_text(f"{code}\n", encoding="utf-8")
    except OSError:
        pass
    _print(f"EXIT_CODE: {code}")
    return code


def _run(args, mode, run_dir) -> int:
    if mode == modes.MODE_NETWORK:
        _print(
            "NETWORK_REFUSED: the explicit network mode is refused by this task; "
            "no network call was made"
        )
        return EXIT_NETWORK_REFUSED

    # SESSION_STOP works from the stored session state only: no configuration
    # parsing and no discovery are needed to release our own process tree.
    if mode == modes.MODE_SESSION_STOP:
        return _run_session_stop(args, run_dir)

    config = config_module.load_config(config_path=args.local_config)
    for warning in config.warnings:
        _print(f"CONFIG_WARNING: {warning}")

    handoff = discovery.LauncherHandoff()
    discovery_result = None
    discovery_config_written = "no"
    if (
        mode in (modes.MODE_AUTO, modes.MODE_LOCAL, modes.MODE_SESSION_START)
        and not config.has_local_config
    ):
        write_allowed = not bool(
            args.local_config or os.environ.get(config_module.LOCAL_CONFIG_ENV)
        )
        discovery_result = discovery.discover(
            config,
            env=os.environ,
            probe=lambda url: local_llm.probe(url, api_key=config.api_key),
            disabled=bool(args.no_discovery),
            write_allowed=write_allowed,
            launcher_handoff=handoff,
        )
        for warning in discovery_result.warnings:
            _print(f"DISCOVERY_WARNING: {warning}")
        config = discovery_result.config
        if handoff.found:
            config = replace(
                config,
                launch_command=handoff.launch_command,
                launch_cwd=handoff.launch_cwd,
                has_local_config=True,
            )
        pending = bool(discovery_result.pending_config and write_allowed)
        _print(
            "DISCOVERY: source=%s, launcher=%s, config_written=%s"
            % (
                discovery_result.source,
                discovery_result.launcher,
                "pending" if pending else "no",
            )
        )

    if mode == modes.MODE_SESSION_START:
        return _run_session_start(
            args, config, handoff, discovery_result, run_dir
        )

    session_state = _active_session(mode)
    launcher, launch = _launcher(config, run_dir)
    if session_state is not None:
        if launcher.probe(config.base_url) is None:
            launcher.stop()
            _print(
                "PREREQUISITE_ERROR: the active live session endpoint is not "
                "reachable; the run refuses to fall back to MOCK (check the "
                "session or run SESSION_STOP)"
            )
            return EXIT_PREREQUISITE
        launch = _session_launch(launch)
    try:
        plan = modes.resolve_provider(
            mode,
            config,
            probe=launcher.probe,
            launch=launch,
        )
    except modes.PrerequisiteError as exc:
        launcher.stop()
        _print(f"PREREQUISITE_ERROR: {exc}")
        return EXIT_PREREQUISITE
    except modes.NetworkRefused as exc:
        launcher.stop()
        _print(f"NETWORK_REFUSED: {exc}")
        return EXIT_NETWORK_REFUSED

    live = plan.kind == modes.KIND_LOCAL
    _print(_live_line(plan))
    if plan.spawned:
        _print("LOCAL_LLM: spawned and ready")

    if session_state is not None and live:
        try:
            live_session.begin_run(session_state, mode, path=SESSION_STATE_PATH)
        except OSError:
            pass
        _print(
            "SESSION: joined endpoint_owner=%s live_runs=%s model_loads=%s "
            "session_id=%s"
            % (
                session_state.endpoint_owner,
                session_state.live_runs,
                session_state.model_loads,
                session_state.session_id,
            )
        )

    discovery_block = None
    if discovery_result is not None:
        discovery_config_written = _write_discovered_config(
            discovery_result,
            handoff,
            live_local_llm=plan.live_local_llm,
            spawned=plan.spawned,
            base_url=config.base_url,
            model=plan.model or config.model or "",
        )
        _print(f"DISCOVERY_CONFIG: written={discovery_config_written}")
        discovery_block = discovery.report_block(
            discovery_result,
            config_written=discovery_config_written,
            config_path=LOCAL_CONFIG_RELATIVE,
        )

    mock = None
    proxy = None
    app = None
    session = None
    recipe_result = None
    scenario_error = None
    records = []
    checks = []
    verdict_result = None
    run_status = "error"

    try:
        upstream = plan.base_url
        if plan.kind == modes.KIND_MOCK:
            mock = MockProvider(scenario.build_mock_fixtures()).start()
            upstream = mock.base_url
            _print(f"MOCK_PROVIDER: {mock.base_url}")

        proxy = RecordingProxy(upstream, run_dir.llm_calls_jsonl).start()
        _print(f"RECORDING_PROXY: {proxy.base_url} -> {upstream}")

        isolation = IsolationGuard(APP_DB_PATH, OWNER_ENV_PATH)
        isolation.take_baseline()
        assert_run_db_path(run_dir.db_path, run_dir.path)

        app = AppProcess(
            port=args.app_port,
            db_path=run_dir.db_path,
            base_url=proxy.base_url + "/v1",
            api_key=config.api_key,
            log_path=run_dir.app_log,
        )
        if not port_is_free(app.port):
            _print(f"PREREQUISITE_ERROR: port {app.port} is already in use")
            return EXIT_PREREQUISITE
        app.start()
        if not app.wait_ready():
            _print("PREREQUISITE_ERROR: the application did not become ready")
            _print(app.tail_log())
            return EXIT_PREREQUISITE
        _print(f"APP_READY: {app.app_url}")

        scenario_timeout_ms = resolve_scenario_timeout_ms(live, config)
        _print(f"SCENARIO_TIMEOUT_MS: {scenario_timeout_ms}")
        try:
            session = open_session(
                app.app_url,
                headless=args.headless,
                timeout_ms=scenario_timeout_ms,
                wait_for="ui_mode",
            )
            _print(f"BROWSER: {session.channel} (headless={args.headless})")
        except modes.PrerequisiteError as exc:
            _print(f"PREREQUISITE_ERROR: {exc}")
            return EXIT_PREREQUISITE

        try:
            recipe_result = scenario.run_browser_scenario(
                session.page,
                db_path=run_dir.db_path,
                run_dir=run_dir,
                app=app,
                timeout_ms=scenario_timeout_ms,
            )
        except Exception as exc:  # noqa: BLE001 - reported as a scenario failure
            scenario_error = f"{type(exc).__name__}: {exc}"
            _print(f"SCENARIO_ERROR: {scenario_error}")
            _print(traceback.format_exc())

        _print(f"TASK_ID: {recipe_result.task_id if recipe_result else 'none'}")

        verdict_result = scenario.evaluate(
            run_dir.db_path, live=live
        )
        aborted_calls = proxy.flush_in_flight()
        if aborted_calls:
            _print(
                f"INCOMPLETE_LLM_CALLS: {aborted_calls} in-flight call(s) were "
                "aborted and recorded as incomplete; their tokens stay unknown"
            )
        records = metrics_module.load_records(run_dir.llm_calls_jsonl)

        metadata = scenario.provider_metadata(live)
        metrics_block = metrics_module.aggregate(
            records,
            provider=metadata["provider"],
            model=plan.model,
            local_model_used=metadata["local_model_used"],
            blocked_external_calls=proxy.blocked_external_calls,
            network_api_calls=0,
        )
        failed_calls = metrics_module.count_failed_calls(records)

        checks.extend(_build_checks(
            plan=plan,
            recipe_result=recipe_result,
            scenario_error=scenario_error,
            verdict_result=verdict_result,
            metrics_block=metrics_block,
            records=records,
            live=live,
            task_attempts=scenario.sum_task_attempts(run_dir.db_path),
            isolation=isolation,
        ))

        report = build_report(
            mode=mode,
            provider_plan=plan,
            metrics=metrics_block,
            records=records,
            checks=checks,
            scenario=verdict_result.as_report() if verdict_result else {
                "error": scenario_error or "scenario did not run"
            },
            isolation=isolation.check().__dict__,
            run_dir=run_dir.path,
            failed_calls=failed_calls,
            discovery=discovery_block,
        )
        if recipe_result is not None:
            report["scenario"]["probe_checks"] = list(recipe_result.probe_checks)
            report["scenario"]["plan_attempts_observed"] = recipe_result.plan_attempts
            report["scenario"]["restart_ok"] = recipe_result.restart_ok
            report["scenario"]["resume_verified"] = recipe_result.resume_verified
            report["scenario"]["step_total"] = recipe_result.step_total
        # The screenshot list is built from the disk, so a partial or failed run
        # still reports the screenshots it managed to take.
        screenshots = _screenshot_paths(run_dir)
        report["scenario"]["screenshots"] = screenshots
        artifacts = run_dir.artifact_paths()
        artifacts["screenshots"] = screenshots
        report["artifacts"] = artifacts
        report["session"] = _session_block(
            session_state, live=live, joined=session_state is not None
        )
        write_report(run_dir.path, report)

        _print("METRICS:")
        for line in report["metric_lines"]:
            _print(f"  {line}")
        _print("CHECKS:")
        for check in report["checks"]:
            _print(f"  {check['name']}: {check['status']} - {check['detail']}")
        _print(f"RESULT: {report['status']}")
        _print(f"REPORT: {run_dir.report_md}")
        run_status = report["status"]
        return EXIT_PASS if report["status"] == PASS else EXIT_FAIL
    finally:
        if session is not None:
            session.close()
        if app is not None:
            app.stop()
        if proxy is not None:
            proxy.stop()
        if mock is not None:
            mock.stop()
        if session_state is not None and live:
            # A live session owns the model: the per-run proxy/app/browser are
            # closed, but the session model is never stopped here. Its run is
            # marked finished best-effort.
            try:
                live_session.finish_run(
                    session_state, mode, run_status, path=SESSION_STATE_PATH
                )
            except OSError:
                pass
        else:
            launcher.stop()


def _build_checks(
    *,
    plan,
    recipe_result,
    scenario_error,
    verdict_result,
    metrics_block,
    records,
    live,
    task_attempts,
    isolation,
):
    checks = []
    if plan.live_local_llm:
        checks.append(
            Check("live_local_llm", PASS, f"live local model {plan.model or 'unknown'}")
        )
    else:
        checks.append(
            Check(
                "live_local_llm",
                SKIPPED,
                plan.reason or "no local runtime; the loopback mock was used",
            )
        )

    if recipe_result is not None and scenario_error is None:
        checks.append(Check("browser_e2e", PASS, "the recipe completed"))
    else:
        checks.append(
            Check(
                "browser_e2e",
                FAIL,
                scenario_error or "the recipe did not run",
            )
        )

    if verdict_result is not None and verdict_result.ok:
        checks.append(
            Check("scenario_verdict", PASS, "the stored scenario matches the contract")
        )
    else:
        problems = (
            "; ".join(verdict_result.problems) if verdict_result else "no verdict"
        )
        checks.append(Check("scenario_verdict", FAIL, problems))

    if recipe_result is not None:
        if recipe_result.probe_checks:
            checks.append(
                Check(
                    "transition_probe",
                    FAIL,
                    "; ".join(recipe_result.probe_checks),
                )
            )
        else:
            checks.append(
                Check(
                    "transition_probe",
                    PASS,
                    f"one refusal row #{recipe_result.probe_audit_id} and no state change",
                )
            )

    checks.append(check_local_metrics_recorded(metrics_block, records, live=live))
    checks.append(check_network_api_calls_zero(metrics_block["network_api_calls"]))
    checks.append(
        check_llm_calls_consistency(
            metrics_block["local_calls"], task_attempts, live=live
        )
    )

    result = isolation.check()
    checks.append(Check("owner_isolation", result.status, result.reason))
    return checks


if __name__ == "__main__":
    raise SystemExit(main())
