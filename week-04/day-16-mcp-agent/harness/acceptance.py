"""One-shot acceptance run: unit → MCP-unavailable browser E2E → live MCP → live model.

The four steps keep their own run directories; this module only writes the
aggregated verdict into a dedicated ``.runs/<timestamp>-acceptance`` directory
and prints the summary lines the operator looks for.

Exit codes: 0 everything passed, 1 a step failed, 2 a prerequisite was missing
(a busy port, no system browser, an unreachable local model).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# The .bat entry points run this file directly (``python harness\acceptance.py``),
# which puts ``harness\`` on sys.path instead of the project root. Add the root
# explicitly so the ``harness.*`` imports resolve.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from harness.qa_bridge import PROJECT_DIR
from harness.processes import PrerequisiteError, sanitized_env
from harness.run_dir import create_run_dir, relative, write_report
from harness.test_profile import (
    load_model_profile,
    probe_model,
    redact_report,
    tavily_opt_in,
)

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_PREREQUISITE = 2

UNIT_TIMEOUT_SECONDS = 600
STEP_TIMEOUT_SECONDS = 3600

# Playwright locates a system Edge/Chrome through these Windows install-path
# variables (``sanitized_env`` keeps only an allow-list, so they are dropped).
# The browser step below needs them back; no secret is among them.
BROWSER_ENV_KEYS = ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "HOMEDRIVE")


def _run(command: list, timeout: float, extra_env: dict | None = None):
    completed = subprocess.run(
        [str(item) for item in command],
        cwd=str(PROJECT_DIR),
        env=sanitized_env(extra_env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    for key, value in (extra_env or {}).items():
        if key.endswith("API_KEY") and value:
            completed.stdout = completed.stdout.replace(str(value), "[redacted]")
            completed.stderr = completed.stderr.replace(str(value), "[redacted]")
    return completed


def _status_from_output(text: str, prefix: str) -> str:
    """Return the last ``<prefix>: <STATUS>`` token of a harness output.

    The harness prints one-line status records (``LIVE_LLM_STATUS: PASS``); this
    reads the first token after the colon so an appended explanation does not
    hide the verdict.
    """
    value = "UNKNOWN"
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix + ":"):
            remainder = stripped.split(":", 1)[1].strip()
            value = remainder.split()[0] if remainder else "UNKNOWN"
    return value


def _prepare_live_model(profile, run_dir, report: dict, owner: dict) -> dict:
    """Select a panel-owned endpoint or start one legacy local launcher."""
    if profile is not None:
        report["test_model"] = profile.public_details()
        if not probe_model(profile):
            raise PrerequisiteError(
                "selected test model is unavailable or does not match its GGUF"
            )
        print(
            "TEST_MODEL_STATUS: READY "
            f"({profile.kind}, {profile.name}, {profile.base_url})"
        )
        child_env = profile.child_environment()
        if profile.kind == "remote":
            child_env["AI_TEST_MODEL_PARENT_READY"] = "1"
        return child_env

    from harness.live_e2e import ensure_local_model

    config, model, launcher = ensure_local_model(run_dir, report)
    owner["launcher"] = launcher
    if not model:
        raise PrerequisiteError("the default local test model is unavailable")
    report["test_model"] = {
        "kind": "local",
        "name": model,
        "base_url": config.base_url,
    }
    print(f"TEST_MODEL_STATUS: READY (local, {model}, {config.base_url})")
    return {}


def _run_steps(
    ui: bool,
    live: bool,
    report: dict,
    profile,
    tavily_enabled: bool,
    run_dir,
    owner: dict,
) -> int:
    """Run unit/MCP checks before preparing the selected live model."""
    exit_code = EXIT_OK

    print("=== unit tests ===")
    unit = _run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        UNIT_TIMEOUT_SECONDS,
    )
    print(unit.stdout or "")
    print(unit.stderr or "")
    report["steps"]["unit"] = {"exit_code": unit.returncode}
    print(f"UNIT_STATUS: {'PASS' if unit.returncode == 0 else 'FAIL'}")
    if unit.returncode != 0:
        exit_code = EXIT_FAIL

    if live:
        # The MCP-unavailable browser scenario runs first, before the live MCP
        # smoke and the live model E2E, so the controlled error path is checked
        # even when no local model is reachable. It boots only the backend with
        # an MCP URL that nothing listens on.
        print("=== MCP-unavailable browser E2E ===")
        unavailable = _run(
            [sys.executable, str(PROJECT_DIR / "harness" / "mcp_unavailable_e2e.py")],
            STEP_TIMEOUT_SECONDS,
            {key: os.environ.get(key) for key in BROWSER_ENV_KEYS},
        )
        print(unavailable.stdout or "")
        print(unavailable.stderr or "")
        report["steps"]["mcp_unavailable_ui"] = {"exit_code": unavailable.returncode}
        if unavailable.returncode == 0:
            unavailable_status = "PASS"
        elif unavailable.returncode == 2:
            unavailable_status = "PREREQUISITE"
            if exit_code == EXIT_OK:
                exit_code = EXIT_PREREQUISITE
        else:
            unavailable_status = "FAIL"
            exit_code = EXIT_FAIL
        print(f"MCP_UNAVAILABLE_UI_STATUS: {unavailable_status}")

        print("=== live MCP smoke ===")
        smoke = _run(
            [sys.executable, str(PROJECT_DIR / "harness" / "live_mcp.py")],
            STEP_TIMEOUT_SECONDS,
        )
        print(smoke.stdout or "")
        print(smoke.stderr or "")
        report["steps"]["live_mcp"] = {"exit_code": smoke.returncode}
        if smoke.returncode not in (0, 2):
            exit_code = EXIT_FAIL
        elif smoke.returncode == 2:
            exit_code = EXIT_PREREQUISITE

        model_env = _prepare_live_model(profile, run_dir, report, owner)

        print("=== live model E2E ===")
        command = [sys.executable, str(PROJECT_DIR / "harness" / "live_e2e.py")]
        if ui:
            command.append("--ui")
        e2e = _run(
            command, STEP_TIMEOUT_SECONDS, {"RUN_LIVE_LLM": "1", **model_env}
        )
        print(e2e.stdout or "")
        print(e2e.stderr or "")
        report["steps"]["live_e2e"] = {"exit_code": e2e.returncode}
        if e2e.returncode == 2 and exit_code == EXIT_OK:
            exit_code = EXIT_PREREQUISITE
        elif e2e.returncode not in (0, 2):
            exit_code = EXIT_FAIL

        # The web-search scenario runs through the same trusted entry point:
        # the real model has to select ``search_web`` and the answer has to
        # cite the deterministic fake-search links. The UI part is optional: a
        # missing system browser yields ``UI_E2E_STATUS: BLOCKED`` and does not
        # fail acceptance, because the live_e2e exit code is the LLM verdict.
        print("=== live search E2E (real model + fake search API) ===")
        search_env = {"RUN_LIVE_LLM": "1", **model_env}
        search_env.update({key: os.environ.get(key) for key in BROWSER_ENV_KEYS})
        search = _run(
            [
                sys.executable,
                str(PROJECT_DIR / "harness" / "live_e2e.py"),
                "--scenario",
                "search",
                "--ui",
            ],
            STEP_TIMEOUT_SECONDS,
            search_env,
        )
        print(search.stdout or "")
        print(search.stderr or "")
        search_live_status = _status_from_output(search.stdout, "LIVE_LLM_STATUS")
        search_ui_status = _status_from_output(search.stdout, "UI_E2E_STATUS")
        if search.returncode == 2:
            search_live_status = "BLOCKED"
        elif search.returncode != 0:
            search_live_status = "FAIL"
        report["steps"]["search_live_e2e"] = {
            "exit_code": search.returncode,
            "live_status": search_live_status,
            "ui_status": search_ui_status,
        }
        print(f"SEARCH_LIVE_STATUS: {search_live_status}")
        print(f"SEARCH_UI_STATUS: {search_ui_status}")
        if search.returncode == 2:
            if exit_code == EXIT_OK:
                exit_code = EXIT_PREREQUISITE
        elif search.returncode != 0 or search_ui_status == "FAIL":
            exit_code = EXIT_FAIL

        # The tasks scenario schedules a repeating search, then reports its
        # summary and drives the chats and tasks panels. Its UI statuses are
        # still produced when no local model is reachable, because the tasks
        # panel is seeded through the real MCP tool and the message history
        # through the storage layer.
        print("=== live tasks E2E (real model + fake search + UI) ===")
        tasks_env = {"RUN_LIVE_LLM": "1", **model_env}
        tasks_env.update({key: os.environ.get(key) for key in BROWSER_ENV_KEYS})
        tasks = _run(
            [
                sys.executable,
                str(PROJECT_DIR / "harness" / "live_e2e.py"),
                "--scenario",
                "tasks",
                "--ui",
            ],
            STEP_TIMEOUT_SECONDS,
            tasks_env,
        )
        print(tasks.stdout or "")
        print(tasks.stderr or "")
        tasks_live_status = _status_from_output(tasks.stdout, "TASKS_LIVE_STATUS")
        chats_ui_status = _status_from_output(tasks.stdout, "CHATS_UI_STATUS")
        tasks_ui_status = _status_from_output(tasks.stdout, "TASKS_UI_STATUS")
        if tasks.returncode == 2:
            tasks_live_status = "BLOCKED"
        elif tasks.returncode != 0:
            tasks_live_status = "FAIL"
        report["steps"]["tasks_live_e2e"] = {
            "exit_code": tasks.returncode,
            "live_status": tasks_live_status,
            "chats_ui_status": chats_ui_status,
            "tasks_ui_status": tasks_ui_status,
        }
        print(f"TASKS_LIVE_STATUS: {tasks_live_status}")
        print(f"CHATS_UI_STATUS: {chats_ui_status}")
        print(f"TASKS_UI_STATUS: {tasks_ui_status}")
        if tasks.returncode == 2:
            if exit_code == EXIT_OK:
                exit_code = EXIT_PREREQUISITE
        elif tasks.returncode != 0 or "FAIL" in (chats_ui_status, tasks_ui_status):
            exit_code = EXIT_FAIL

        # The composition scenario drives three dependent tools from one message
        # (search_web → digest_search_results → save_report) and checks that a
        # plain search does not create a report, plus the Saved reports UI.
        print("=== live composition E2E (real model + fake search + UI) ===")
        composition_env = {"RUN_LIVE_LLM": "1", **model_env}
        composition_env.update({key: os.environ.get(key) for key in BROWSER_ENV_KEYS})
        composition = _run(
            [
                sys.executable,
                str(PROJECT_DIR / "harness" / "live_e2e.py"),
                "--scenario",
                "composition",
                "--ui",
            ],
            STEP_TIMEOUT_SECONDS,
            composition_env,
        )
        print(composition.stdout or "")
        print(composition.stderr or "")
        composition_live_status = _status_from_output(
            composition.stdout, "COMPOSITION_LIVE_STATUS"
        )
        composition_no_save_status = _status_from_output(
            composition.stdout, "COMPOSITION_NO_SAVE_LIVE_STATUS"
        )
        reports_ui_status = _status_from_output(
            composition.stdout, "REPORTS_UI_STATUS"
        )
        if composition.returncode == 2:
            composition_live_status = "BLOCKED"
        elif composition.returncode != 0:
            composition_live_status = "FAIL"
        report["steps"]["composition_live_e2e"] = {
            "exit_code": composition.returncode,
            "live_status": composition_live_status,
            "no_save_status": composition_no_save_status,
            "reports_ui_status": reports_ui_status,
        }
        print(f"COMPOSITION_LIVE_STATUS: {composition_live_status}")
        print(f"COMPOSITION_NO_SAVE_LIVE_STATUS: {composition_no_save_status}")
        print(f"REPORTS_UI_STATUS: {reports_ui_status}")
        if composition.returncode == 2:
            if exit_code == EXIT_OK:
                exit_code = EXIT_PREREQUISITE
        elif composition.returncode != 0 or "FAIL" in (
            composition_no_save_status,
            reports_ui_status,
        ):
            exit_code = EXIT_FAIL

        # The day-20 notification scenario drives the real model across two real
        # MCP servers (A and B). Server B's Telegram and server A's paid search
        # API are replaced by loopback fakes; the UI shows both servers and the
        # watches panel.
        print("=== live notifications E2E (real model + A+B + fakes + UI) ===")
        notifications_env = {"RUN_LIVE_LLM": "1", **model_env}
        notifications_env.update(
            {key: os.environ.get(key) for key in BROWSER_ENV_KEYS}
        )
        notifications = _run(
            [
                sys.executable,
                str(PROJECT_DIR / "harness" / "live_e2e.py"),
                "--scenario",
                "notifications",
                "--ui",
            ],
            STEP_TIMEOUT_SECONDS,
            notifications_env,
        )
        print(notifications.stdout or "")
        print(notifications.stderr or "")
        notifications_live = _status_from_output(
            notifications.stdout, "NOTIFICATIONS_LIVE_STATUS"
        )
        notifier_servers_ui = _status_from_output(
            notifications.stdout, "NOTIFIER_SERVERS_UI_STATUS"
        )
        notification_ui = _status_from_output(
            notifications.stdout, "NOTIFICATION_UI_STATUS"
        )
        if notifications.returncode == 2:
            notifications_live = "BLOCKED"
        elif notifications.returncode != 0 and notifications_live == "UNKNOWN":
            notifications_live = "FAIL"
        report["steps"]["notifications_live_e2e"] = {
            "exit_code": notifications.returncode,
            "notifications_live": notifications_live,
            "servers_ui": notifier_servers_ui,
            "notification_ui": notification_ui,
        }
        print(f"NOTIFICATIONS_LIVE_STATUS: {notifications_live}")
        print(f"NOTIFIER_SERVERS_UI_STATUS: {notifier_servers_ui}")
        print(f"NOTIFICATION_UI_STATUS: {notification_ui}")
        if notifications.returncode == 2:
            if exit_code == EXIT_OK:
                exit_code = EXIT_PREREQUISITE
        elif notifications.returncode != 0 or "FAIL" in (
            notifier_servers_ui,
            notification_ui,
        ):
            exit_code = EXIT_FAIL

        # The monitor channel is its own LIVE run: it needs the backend monitor
        # enabled with a short tick, so it cannot share the chat scenario's
        # backend window.
        print("=== monitor LIVE E2E (real model + A+B + fakes) ===")
        monitor = _run(
            [
                sys.executable,
                str(PROJECT_DIR / "harness" / "live_e2e.py"),
                "--scenario",
                "notifications-monitor",
            ],
            STEP_TIMEOUT_SECONDS,
            {"RUN_LIVE_LLM": "1", **model_env},
        )
        print(monitor.stdout or "")
        print(monitor.stderr or "")
        monitor_live = _status_from_output(
            monitor.stdout, "NOTIFICATIONS_MONITOR_LIVE_STATUS"
        )
        if monitor.returncode == 2:
            monitor_live = "BLOCKED"
        elif monitor.returncode != 0 and monitor_live == "UNKNOWN":
            monitor_live = "FAIL"
        report["steps"]["notifications_monitor_live_e2e"] = {
            "exit_code": monitor.returncode,
            "monitor_live": monitor_live,
        }
        print(f"NOTIFICATIONS_MONITOR_LIVE_STATUS: {monitor_live}")
        if monitor.returncode == 2:
            if exit_code == EXIT_OK:
                exit_code = EXIT_PREREQUISITE
        elif monitor.returncode != 0:
            exit_code = EXIT_FAIL

        if tavily_enabled:
            print("=== real Tavily E2E (opt-in model + MCP + search) ===")
            tavily = _run(
                [
                    sys.executable,
                    str(PROJECT_DIR / "harness" / "live_e2e.py"),
                    "--scenario",
                    "tavily",
                ],
                STEP_TIMEOUT_SECONDS,
                {
                    "RUN_LIVE_LLM": "1",
                    **model_env,
                    "AI_TEST_TAVILY_ENABLED": "1",
                    "AI_TEST_TAVILY_API_KEY": os.environ["AI_TEST_TAVILY_API_KEY"],
                },
            )
            print(tavily.stdout or "")
            print(tavily.stderr or "")
            tavily_status = _status_from_output(tavily.stdout, "TAVILY_STATUS")
            if tavily.returncode == EXIT_PREREQUISITE:
                tavily_status = "BLOCKED"
                if exit_code == EXIT_OK:
                    exit_code = EXIT_PREREQUISITE
            elif tavily.returncode != EXIT_OK:
                tavily_status = "FAIL"
                exit_code = EXIT_FAIL
            report["steps"]["tavily_live_e2e"] = {
                "exit_code": tavily.returncode,
                "tavily_status": tavily_status,
            }
            print(f"TAVILY_STATUS: {tavily_status}")
        else:
            report["tavily_status"] = "NOT_REQUESTED"

        # The only real external delivery is opt-in: without
        # NOTIFIER_REAL_ALLOW=1 the harness reports BLOCKED and acceptance stays
        # green. An explicit operator opt-in is forwarded to allow the one call.
        print("=== real Telegram delivery (opt-in) ===")
        real = _run(
            [sys.executable, str(PROJECT_DIR / "harness" / "notifier_real_live.py")],
            STEP_TIMEOUT_SECONDS,
            {"NOTIFIER_REAL_ALLOW": os.environ.get("NOTIFIER_REAL_ALLOW")},
        )
        print(real.stdout or "")
        print(real.stderr or "")
        notifier_real = _status_from_output(real.stdout, "NOTIFIER_REAL_STATUS")
        if real.returncode == 2:
            notifier_real = "BLOCKED"
        elif real.returncode != 0:
            notifier_real = "FAIL"
            exit_code = EXIT_FAIL
        report["steps"]["notifier_real_live"] = {
            "exit_code": real.returncode,
            "notifier_real": notifier_real,
        }
        print(f"NOTIFIER_REAL_STATUS: {notifier_real}")

    return exit_code


def run(ui: bool, live: bool) -> int:
    """Validate one local test profile and own one launcher for the live run."""
    run_dir = create_run_dir("acceptance")
    report: dict = {"scenario": "acceptance", "steps": {}}
    owner: dict = {"launcher": None}
    exit_code = EXIT_FAIL
    try:
        profile = load_model_profile(os.environ)
        tavily_enabled = tavily_opt_in(os.environ)
        exit_code = _run_steps(
            ui, live, report, profile, tavily_enabled, run_dir, owner
        )
    except PrerequisiteError as exc:
        print(f"TEST_MODEL_STATUS: BLOCKED - {exc}")
        report["reason"] = str(exc)
        exit_code = EXIT_PREREQUISITE
    finally:
        if owner["launcher"] is not None:
            report["model_launcher_stop"] = owner["launcher"].stop()
        report["exit_code"] = exit_code
        report["status"] = {
            EXIT_OK: "pass",
            EXIT_FAIL: "fail",
            EXIT_PREREQUISITE: "blocked",
        }[exit_code]
        write_report(
            run_dir,
            redact_report(
                report,
                (
                    os.environ.get("AI_TEST_MODEL_API_KEY"),
                    os.environ.get("AI_TEST_TAVILY_API_KEY"),
                    os.environ.get("AI_TEST_MODEL_PATH"),
                ),
            ),
        )
        print(f"RUN_DIR: {relative(run_dir)}")
    return exit_code


def main(argv=None) -> int:
    """Entry point of ``harness/acceptance.py``."""
    parser = argparse.ArgumentParser(prog="acceptance")
    parser.add_argument(
        "--no-live",
        action="store_true",
        help="run only the unit suite (no live MCP and no model)",
    )
    parser.add_argument("--ui", action="store_true", help="also run the UI E2E")
    args = parser.parse_args(argv)
    ui = bool(args.ui or str(os.environ.get("RUN_UI_E2E") or "").strip() == "1")
    return run(ui=ui, live=not args.no_live)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
