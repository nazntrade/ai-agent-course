"""Real-browser recipe of the Day 15 memory-state-agent scenario.

The recipe drives the actual Streamlit UI in a real browser: it creates a chat,
creates the demo task, runs planning (which the mock answers with a bad plan and
then a good one), reviews and accepts the plan, exercises the production
transition guard through ``Test Finish execution``, runs every step of the
accepted plan (read back from SQLite, so a live 4- or 6-step plan is driven
fully), finishes execution, pauses the task, fully restarts the application
process on the same port and database and resumes the task.

Every decision is read back from SQLite; the browser only clicks the widgets and
takes screenshots. A live long-running model needs the caller's generous timeout
budget, so the timeout is threaded through every UI interaction instead of
falling back to the browser default.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from lib import browser as ui

_APP_DIR = Path(__file__).resolve().parents[3] / "week-03" / "memory-state-agent"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

from tasks import plan_steps  # noqa: E402  (path is set above)

MODE_GROUP_KEY = "ui_mode"
CHAT_MODE = "Chat"
DIAGNOSTICS_MODE = "Diagnostics / Task"

EVENT_TASK_CREATED = "TASK_CREATED"
EVENT_PLAN_CREATED = "PLAN_CREATED"
EVENT_PLAN_ACCEPTED = "PLAN_ACCEPTED"
EVENT_STEP_COMPLETED = "STEP_COMPLETED"
EVENT_EXECUTION_FINISHED = "EXECUTION_FINISHED"
EVENT_PAUSE = "PAUSE"
EVENT_RESUME = "RESUME"

DEFAULT_TIMEOUT_MS = 90000


class RecipeError(RuntimeError):
    """Raised when a browser step or a database assertion fails."""


@dataclass
class RecipeResult:
    """What the recipe observed while driving the UI."""

    task_id: int = 0
    screenshots: list = field(default_factory=list)
    step_total: int = 0
    plan_attempts: int | None = None
    step_attempts: list = field(default_factory=list)
    probe_audit_id: int | None = None
    probe_checks: list = field(default_factory=list)
    restart_ok: bool = False
    resume_verified: bool = False
    notes: list = field(default_factory=list)


def _query(db_path, sql, params=()):
    connection = sqlite3.connect(str(db_path), timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def _scalar(db_path, sql, params=(), default=None):
    rows = _query(db_path, sql, params)
    if not rows:
        return default
    return list(rows[0].values())[0]


def wait_until(predicate, *, timeout, interval=0.25, message="condition"):
    """Poll ``predicate`` until it is truthy, or raise ``RecipeError``."""
    deadline = time.monotonic() + max(timeout, 1) / 1000.0
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - retried until the deadline
            last_error = exc
        time.sleep(interval)
    detail = f" ({last_error})" if last_error is not None else ""
    raise RecipeError(f"timed out waiting for {message}{detail}")


def _event_rows(db_path, task_id):
    return _query(
        db_path,
        "SELECT id, event_type, payload_json FROM task_events "
        "WHERE task_id = ? ORDER BY id",
        (task_id,),
    )


def _event_count(db_path, task_id, event_type=None):
    if event_type is None:
        return int(
            _scalar(
                db_path, "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,), 0
            )
        )
    return int(
        _scalar(
            db_path,
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND event_type = ?",
            (task_id, event_type),
            0,
        )
    )


def _attempts(db_path, task_id):
    return _query(
        db_path,
        "SELECT id, action, reason FROM task_transition_attempts "
        "WHERE task_id = ? ORDER BY id",
        (task_id,),
    )


def _task_fields(db_path, task_id):
    rows = _query(
        db_path,
        "SELECT stage, status, version, current_step, current_step_index, "
        "expected_action_type FROM tasks WHERE id = ?",
        (task_id,),
    )
    return rows[0] if rows else {}


def _snapshot(db_path, task_id):
    return {
        "task": _task_fields(db_path, task_id),
        "events": _query(
            db_path,
            "SELECT id, event_type FROM task_events WHERE task_id = ? ORDER BY id",
            (task_id,),
        ),
        "artifacts": _query(
            db_path,
            "SELECT id, kind, revision, content FROM task_artifacts "
            "WHERE task_id = ? ORDER BY id",
            (task_id,),
        ),
        "attempts": _attempts(db_path, task_id),
    }


def _event_attempts(db_path, task_id, event_type):
    rows = _query(
        db_path,
        "SELECT payload_json FROM task_events WHERE task_id = ? AND event_type = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, event_type),
    )
    if not rows:
        return None
    try:
        payload = json.loads(rows[0]["payload_json"] or "{}")
    except ValueError:
        return None
    value = payload.get("attempts")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _plan_steps(db_path, task_id) -> list:
    """Return the valid steps of the newest stored plan, or an empty list.

    The plan is read back from SQLite after ``PLAN_ACCEPTED``; a missing, broken
    or step-less plan yields no steps so the recipe can fail loudly instead of
    driving a hard-coded number of steps.
    """
    rows = _query(
        db_path,
        "SELECT content FROM task_artifacts WHERE task_id = ? AND kind = 'plan' "
        "ORDER BY revision DESC, id DESC LIMIT 1",
        (task_id,),
    )
    if not rows:
        return []
    try:
        content = json.loads(rows[0]["content"] or "{}")
    except ValueError:
        return []
    return plan_steps(content)


def run_recipe(
    page,
    *,
    db_path,
    run_dir,
    app,
    title,
    goal,
    brief,
    timeout_ms=DEFAULT_TIMEOUT_MS,
):
    """Drive the full scenario and return what was observed."""
    result = RecipeResult()
    db_path = str(db_path)

    def shoot(name):
        result.screenshots.append(
            str(ui.screenshot(page, run_dir.screenshot(name)))
        )

    # 1) A chat is required before a task can exist.
    ui.click_button(page, "Create chat", timeout=timeout_ms)
    wait_until(
        lambda: _scalar(db_path, "SELECT COUNT(*) FROM chats", (), 0) >= 1,
        timeout=timeout_ms,
        message="the chat row",
    )
    shoot("01_chat_created.png")

    # 2) Create the demo task from the Diagnostics / Task form.
    ui.select_radio(page, MODE_GROUP_KEY, DIAGNOSTICS_MODE, timeout=timeout_ms)
    ui.open_expander(page, "Create task", timeout=timeout_ms)
    ui.fill_key(page, "task_diag_create_title_0", title, timeout=timeout_ms)
    ui.fill_key(page, "task_diag_create_goal_0", goal, timeout=timeout_ms)
    ui.fill_key(
        page, "task_diag_create_brief_0", brief, area=True, timeout=timeout_ms
    )
    ui.click_key(page, "task_diag_create_submit_0", timeout=timeout_ms)
    wait_until(
        lambda: _scalar(db_path, "SELECT COUNT(*) FROM tasks", (), 0) >= 1,
        timeout=timeout_ms,
        message="the task row",
    )
    result.task_id = int(_scalar(db_path, "SELECT MAX(id) FROM tasks", (), 0))
    task_id = result.task_id

    # 3) Planning: the mock answers the first call with the incompatible plan
    #    and the corrective retry with the good one (defect a).
    ui.select_radio(page, MODE_GROUP_KEY, CHAT_MODE, timeout=timeout_ms)
    ui.wait_for_key(page, "task_action_run_planning", timeout=timeout_ms)
    ui.click_key(page, "task_action_run_planning", timeout=timeout_ms)
    wait_until(
        lambda: _event_count(db_path, task_id, EVENT_PLAN_CREATED) >= 1,
        timeout=timeout_ms,
        message="PLAN_CREATED",
    )
    result.plan_attempts = _event_attempts(db_path, task_id, EVENT_PLAN_CREATED)
    ui.wait_for_key(page, "task_review_plan", timeout=timeout_ms)
    shoot("02_plan_ready.png")

    # 4) Review and accept the plan.
    ui.click_key(page, "task_review_plan", timeout=timeout_ms)
    ui.wait_for_key(page, "task_plan_dialog_accept", timeout=timeout_ms)
    shoot("03_plan_dialog.png")
    ui.click_key(page, "task_plan_dialog_accept", timeout=timeout_ms)
    wait_until(
        lambda: _event_count(db_path, task_id, EVENT_PLAN_ACCEPTED) >= 1,
        timeout=timeout_ms,
        message="PLAN_ACCEPTED",
    )

    # 5) The production transition guard: the explicit probe must append exactly
    #    one refusal-audit row and change nothing else.
    ui.select_radio(page, MODE_GROUP_KEY, DIAGNOSTICS_MODE, timeout=timeout_ms)
    snapshot_before = _snapshot(db_path, task_id)
    ui.open_expander(page, "Transition guard", timeout=timeout_ms)
    probe_key = f"task_guard_probe_finish_execution_{task_id}"
    ui.wait_for_key(page, probe_key, timeout=timeout_ms)
    shoot("04_transition_guard.png")
    ui.click_key(page, probe_key, timeout=timeout_ms)
    wait_until(
        lambda: ui.text_visible(page, "Refusal audit #"),
        timeout=timeout_ms,
        message="the refusal-audit caption",
    )
    wait_until(
        lambda: len(_attempts(db_path, task_id)) >= 1,
        timeout=timeout_ms,
        message="the refusal-audit row",
    )
    snapshot_after = _snapshot(db_path, task_id)
    result.probe_checks, result.probe_audit_id = _probe_checks(
        snapshot_before, snapshot_after, page
    )
    shoot("05_refusal_audit.png")

    # 6) Back to the chat card: run exactly the accepted plan's steps. The plan
    #    is read back from SQLite so a live 4- or 6-step plan is driven fully and
    #    a broken plan fails loudly instead of running a hard-coded count.
    steps = _plan_steps(db_path, task_id)
    if not steps:
        raise RecipeError(
            "the accepted plan stores no valid steps; nothing can be executed"
        )
    result.step_total = len(steps)
    ui.select_radio(page, MODE_GROUP_KEY, CHAT_MODE, timeout=timeout_ms)
    for _step in range(len(steps)):
        ui.wait_for_key(page, "task_action_run_step", timeout=timeout_ms)
        wait_until(
            lambda: ui.button_enabled(page, "task_action_run_step"),
            timeout=timeout_ms,
            message="an enabled Run step button",
        )
        before = _event_count(db_path, task_id, EVENT_STEP_COMPLETED)
        ui.click_key(page, "task_action_run_step", timeout=timeout_ms)
        wait_until(
            lambda: _event_count(db_path, task_id, EVENT_STEP_COMPLETED) > before,
            timeout=timeout_ms,
            message="the next STEP_COMPLETED",
        )
    result.step_attempts = [
        _payload_attempts(event)
        for event in _event_rows(db_path, task_id)
        if event["event_type"] == EVENT_STEP_COMPLETED
    ]
    shoot("06_steps_completed.png")

    # 7) Finish execution and pause the task. The button may take a live model a
    #    long time to enable, so the wait uses the run budget, not the 30 s
    #    browser default.
    ui.wait_for_key(page, "task_action_finish_execution", timeout=timeout_ms)
    ui.click_key(page, "task_action_finish_execution", timeout=timeout_ms)
    wait_until(
        lambda: _event_count(db_path, task_id, EVENT_EXECUTION_FINISHED) >= 1,
        timeout=timeout_ms,
        message="EXECUTION_FINISHED",
    )
    ui.wait_for_key(page, "task_action_pause", timeout=timeout_ms)
    ui.click_key(page, "task_action_pause", timeout=timeout_ms)
    wait_until(
        lambda: _task_fields(db_path, task_id).get("status") == "paused",
        timeout=timeout_ms,
        message="the paused status",
    )
    shoot("07_paused.png")

    # 8) A real full process restart on the same port and database, then Resume.
    result.restart_ok = bool(app.restart())
    if not result.restart_ok:
        raise RecipeError("the application process did not restart")
    page.goto(app.app_url, wait_until="domcontentloaded", timeout=timeout_ms)
    ui.wait_for_key(page, MODE_GROUP_KEY, timeout=timeout_ms)
    ui.wait_for_key(page, "task_action_resume", timeout=timeout_ms)
    result.resume_verified = True
    shoot("08_after_restart.png")
    ui.click_key(page, "task_action_resume", timeout=timeout_ms)
    wait_until(
        lambda: _event_count(db_path, task_id, EVENT_RESUME) >= 1,
        timeout=timeout_ms,
        message="RESUME",
    )
    wait_until(
        lambda: _task_fields(db_path, task_id).get("status") == "active",
        timeout=timeout_ms,
        message="the resumed status",
    )
    shoot("09_resumed.png")
    return result


def _payload_attempts(row):
    try:
        payload = json.loads(row.get("payload_json") or "{}")
    except ValueError:
        return None
    value = payload.get("attempts")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _probe_checks(snapshot_before, snapshot_after, page):
    """Compare the task state around the guard probe and parse the shown id."""
    problems = []
    if snapshot_before["task"] != snapshot_after["task"]:
        problems.append("the probe changed the task state")
    if snapshot_before["events"] != snapshot_after["events"]:
        problems.append("the probe changed the task journal")
    if snapshot_before["artifacts"] != snapshot_after["artifacts"]:
        problems.append("the probe changed the task artifacts")
    before_attempts = snapshot_before["attempts"]
    after_attempts = snapshot_after["attempts"]
    if before_attempts:
        problems.append("the audit was not empty before the probe")
    if len(after_attempts) != 1:
        problems.append(
            f"expected exactly one refusal-audit row, got {len(after_attempts)}"
        )
    if after_attempts and after_attempts[0].get("action") != "finish_execution":
        problems.append(
            f"the audit action is {after_attempts[0].get('action')!r}, "
            "expected 'finish_execution'"
        )
    audit_id = after_attempts[0]["id"] if after_attempts else None
    try:
        text = page.get_by_text(re.compile("Refusal audit #")).first.inner_text()
        match = re.search(r"Refusal audit #(\d+)", text)
    except Exception:
        match = None
    if match is None:
        problems.append("the UI did not show a 'Refusal audit #<id>' line")
    elif audit_id is not None and int(match.group(1)) != int(audit_id):
        problems.append(
            f"the UI shows audit #{match.group(1)} but the stored row is #{audit_id}"
        )
    return problems, audit_id
