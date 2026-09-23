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
from harness.processes import sanitized_env
from harness.run_dir import create_run_dir, relative, write_report

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
    return subprocess.run(
        [str(item) for item in command],
        cwd=str(PROJECT_DIR),
        env=sanitized_env(extra_env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


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


def run(ui: bool, live: bool) -> int:
    """Run the acceptance steps in order."""
    run_dir = create_run_dir("acceptance")
    report: dict = {"scenario": "acceptance", "steps": {}}
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

        print("=== live model E2E ===")
        command = [sys.executable, str(PROJECT_DIR / "harness" / "live_e2e.py")]
        if ui:
            command.append("--ui")
        e2e = _run(command, STEP_TIMEOUT_SECONDS, {"RUN_LIVE_LLM": "1"})
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
        search_env = {"RUN_LIVE_LLM": "1"}
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

    report["exit_code"] = exit_code
    report["status"] = {0: "pass", 1: "fail", 2: "prerequisite"}.get(exit_code, "fail")
    write_report(run_dir, report)
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
