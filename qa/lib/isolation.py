"""Isolation of one E2E run from the owner's working state.

Every run gets its own ``qa/.runs/<stamp>-<mode>/`` directory with a private
database, logs and screenshots. Before and after the run the guard fingerprints
the owner's working database (without opening it for writes) and records only
the ``stat`` of the owner's ``.env`` — its content is never read.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from lib.paths import APP_DB_PATH, OWNER_ENV_PATH, RUNS_DIR

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_SKIPPED = "SKIPPED"

_DB_SUFFIXES = ("", "-wal", "-shm")


def sha256_file(path) -> str | None:
    """Return the hex digest of a file, or ``None`` when it does not exist."""
    target = Path(path)
    if not target.exists() or not target.is_file():
        return None
    digest = hashlib.sha256()
    with open(target, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(path) -> dict:
    """Return a content fingerprint of one file (or of its absence)."""
    target = Path(path)
    if not target.exists():
        return {"path": str(target), "exists": False, "sha256": None, "size": None}
    stat = target.stat()
    return {
        "path": str(target),
        "exists": True,
        "sha256": sha256_file(target),
        "size": stat.st_size,
    }


def fingerprint_db(db_path) -> dict:
    """Fingerprint a SQLite database together with its WAL sidecar files."""
    base = str(db_path)
    return {f"db{suffix}": fingerprint(base + suffix) for suffix in _DB_SUFFIXES}


def stat_only(path) -> dict:
    """Record size and mtime of a file without reading its content."""
    target = Path(path)
    if not target.exists():
        return {"path": str(target), "exists": False, "size": None, "mtime": None}
    stat = target.stat()
    return {
        "path": str(target),
        "exists": True,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


@dataclass
class IsolationResult:
    """Outcome of the isolation check of one run."""

    status: str = STATUS_SKIPPED
    reason: str = ""
    db_changed: bool = False
    env_changed: bool = False
    baseline: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)


class IsolationGuard:
    """Fingerprint the owner's state around a run and compare afterwards."""

    def __init__(self, db_path=APP_DB_PATH, env_path=OWNER_ENV_PATH):
        self._db_path = Path(db_path)
        self._env_path = Path(env_path)
        self._baseline = None
        self._baseline_after = None
        self._after = None

    @property
    def baseline_stable(self) -> bool:
        """Whether the two pre-run fingerprints agree."""
        return self._baseline is not None and self._baseline == self._baseline_after

    def take_baseline(self) -> dict:
        """Take a double fingerprint of the owner's database and env stat."""
        self._baseline = {
            "db": fingerprint_db(self._db_path),
            "env": stat_only(self._env_path),
        }
        self._baseline_after = {
            "db": fingerprint_db(self._db_path),
            "env": stat_only(self._env_path),
        }
        return self._baseline

    def check(self) -> IsolationResult:
        """Compare the current state with the baseline.

        An unstable baseline (the database changed between the two pre-run
        fingerprints) yields ``SKIPPED``: the run cannot attribute a later
        change to itself. Otherwise a changed database or env stat is ``FAIL``.
        """
        if self._baseline is None:
            return IsolationResult(
                status=STATUS_SKIPPED, reason="no baseline was taken"
            )
        if not self.baseline_stable:
            return IsolationResult(
                status=STATUS_SKIPPED,
                reason="the owner database was not stable before the run",
                baseline=self._baseline,
            )
        self._after = {
            "db": fingerprint_db(self._db_path),
            "env": stat_only(self._env_path),
        }
        db_changed = self._baseline["db"] != self._after["db"]
        env_changed = self._baseline["env"] != self._after["env"]
        if db_changed or env_changed:
            return IsolationResult(
                status=STATUS_FAIL,
                reason=(
                    "the owner working database changed"
                    if db_changed
                    else "the owner .env stat changed"
                ),
                db_changed=db_changed,
                env_changed=env_changed,
                baseline=self._baseline,
                after=self._after,
            )
        return IsolationResult(
            status=STATUS_PASS,
            reason="the owner database and .env are unchanged",
            baseline=self._baseline,
            after=self._after,
        )


def assert_run_db_path(db_path, run_dir) -> None:
    """Fail when the app database is not inside the run directory."""
    resolved = Path(db_path).resolve()
    root = Path(run_dir).resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(
            f"the app database {resolved} is outside the run directory {root}"
        )


class RunDir:
    """The private directory of one run and every artifact it contains."""

    def __init__(self, mode, *, root=None, stamp=None):
        self.mode = str(mode).upper()
        self._root = Path(root) if root is not None else RUNS_DIR
        self.stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = self._reserve_path()

    def _reserve_path(self) -> Path:
        """Create and return a unique run directory.

        The directory is created here so two runs of the same second can never
        resolve to the same path, even before their sub-directories exist.
        """
        base = self._root / f"{self.stamp}-{self.mode.lower()}"
        candidate = base
        counter = 1
        while True:
            try:
                candidate.mkdir(parents=True, exist_ok=False)
                return candidate
            except FileExistsError:
                counter += 1
                candidate = Path(f"{base}-{counter}")

    @property
    def db_dir(self) -> Path:
        return self.path / "db"

    @property
    def db_path(self) -> Path:
        return self.db_dir / "app.db"

    @property
    def logs_dir(self) -> Path:
        return self.path / "logs"

    @property
    def app_log(self) -> Path:
        return self.logs_dir / "app.log"

    @property
    def screenshots_dir(self) -> Path:
        return self.path / "screenshots"

    @property
    def llm_calls_jsonl(self) -> Path:
        return self.path / "llm_calls.jsonl"

    @property
    def report_md(self) -> Path:
        return self.path / "report.md"

    @property
    def report_json(self) -> Path:
        return self.path / "report.json"

    def create(self) -> "RunDir":
        """Create every sub-directory; safe to call more than once."""
        for directory in (self.db_dir, self.logs_dir, self.screenshots_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def screenshot(self, name) -> Path:
        """Return a screenshot path inside the run directory."""
        return self.screenshots_dir / str(name)

    def artifact_paths(self) -> dict:
        return {
            "report_md": str(self.report_md),
            "report_json": str(self.report_json),
            "llm_calls_jsonl": str(self.llm_calls_jsonl),
            "database": str(self.db_path),
            "app_log": str(self.app_log),
            "screenshots_dir": str(self.screenshots_dir),
        }
