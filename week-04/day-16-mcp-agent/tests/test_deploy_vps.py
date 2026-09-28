"""Deploy-contour tests for the Day 20 VPS script.

The byte-level and static tests always run. The behavioural scenarios execute
the real ``deploy_vps.sh`` under Git Bash with deterministic shims for the
privileged VPS commands; they are skipped with an explicit reason when Git Bash
or PowerShell is unavailable. Nothing here touches the network, real services,
SSH or the real ``.env``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]
DEPLOY_SH = ROOT / "deploy_vps.sh"
DEPLOY_BAT = ROOT / "deploy_vps.bat"
NORMALIZE_LF = ROOT / "harness" / "normalize_lf.ps1"
SHIM_LIB = ROOT / "tests" / "support" / "deploy_shims.sh"
GITATTRIBUTES = REPO_ROOT / ".gitattributes"

SHA_OLD = "a" * 40
SHA_NEW = "b" * 40

SHIM_NAMES = ("id", "stat", "runuser", "git", "systemctl", "curl", "python3", "sleep", "timeout")


def _find_powershell():
    for name in ("powershell", "pwsh"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _usable_bash(candidate) -> bool:
    """Reject the Windows WSL launcher and any bash that cannot execute a script."""
    text = str(candidate).lower()
    if "system32" in text and "git" not in text:
        # C:\\Windows\\System32\\bash.exe is the WSL relay; it has no distro here.
        return False
    try:
        result = subprocess.run(
            [str(candidate), "-c", "echo BASH_OK"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "BASH_OK" in result.stdout


def _git_bash_candidates():
    git = shutil.which("git")
    if git:
        try:
            result = subprocess.run(
                [git, "--exec-path"], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            pass
        else:
            if result.returncode == 0:
                for parent in Path(result.stdout.strip()).parents:
                    yield parent / "bin" / "bash.exe"
    for candidate in (
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
        Path(r"C:\Program Files (x86)\Git\bin\bash.exe"),
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "bash.exe",
    ):
        yield candidate


def _find_bash():
    found = shutil.which("bash")
    candidates = list(_git_bash_candidates())
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.exists() and _usable_bash(candidate):
            return str(candidate)
    return None


def _to_posix(path) -> str:
    """Convert a Windows path to the form MSYS bash exposes (C:\\x -> /c/x)."""
    text = str(path).replace("\\", "/")
    if len(text) >= 2 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:]
    return text


class NormalizeLfScriptTests(unittest.TestCase):
    def test_deploy_script_is_stored_with_lf(self):
        self.assertNotIn(
            b"\r",
            DEPLOY_SH.read_bytes(),
            "deploy_vps.sh must be stored with LF line endings",
        )

    def test_normalization_removes_cr_and_keeps_content(self):
        powershell = _find_powershell()
        if powershell is None:
            self.skipTest("PowerShell was not found; line-ending normalization is not run")
        original = DEPLOY_SH.read_bytes()
        expected = original.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        crlf = original.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", b"\r\n")
        self.assertIn(b"\r", crlf, "the CRLF fixture must actually contain CR bytes")

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "crlf.sh"
            destination = tmp / "normalized.sh"
            source.write_bytes(crlf)
            result = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(NORMALIZE_LF),
                    "-Source",
                    str(source),
                    "-Destination",
                    str(destination),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = destination.read_bytes()
            self.assertNotIn(b"\r", data, "normalized script must not contain CR")
            self.assertTrue(data.startswith(b"#!/usr/bin/env bash"))
            self.assertEqual(data, expected)


class GitAttributesTests(unittest.TestCase):
    def test_root_gitattributes_pins_shell_to_lf(self):
        self.assertTrue(GITATTRIBUTES.is_file(), f"{GITATTRIBUTES} is missing")
        text = GITATTRIBUTES.read_text(encoding="utf-8")
        self.assertIn("*.sh", text)
        self.assertIn("eol=lf", text)


class DeployBatchStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DEPLOY_BAT.read_text(encoding="utf-8")

    def test_batch_normalizes_before_ssh(self):
        self.assertIn("normalize_lf.ps1", self.text)
        self.assertIn('< "%LF_SCRIPT%"', self.text)

    def test_batch_no_longer_pipes_the_working_copy_to_ssh(self):
        self.assertNotIn('< "%PROJECT_DIR%deploy_vps.sh"', self.text)
        for line in self.text.splitlines():
            if '< "%' in line:
                self.assertIn("LF_SCRIPT", line)
                self.assertNotIn("deploy_vps.sh", line)

    def test_batch_removes_the_temporary_script(self):
        self.assertIn(":fail", self.text)
        self.assertIn("if defined LF_SCRIPT", self.text)
        self.assertGreaterEqual(self.text.count('del /q "%LF_SCRIPT%"'), 2)


class DeployShellSyntaxTests(unittest.TestCase):
    def test_shell_script_parses(self):
        bash = _find_bash()
        if bash is None:
            self.skipTest("Git Bash was not found; shell syntax check is not run")
        result = subprocess.run(
            [bash, "-n", "deploy_vps.sh"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class DeployShellBehaviourTests(unittest.TestCase):
    """Run the real deploy_vps.sh against an isolated sandbox and shims."""

    def setUp(self):
        self.bash = _find_bash()
        self._tmp = Path(tempfile.mkdtemp(prefix="day16-deploy-test-"))
        if self.bash is None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self.skipTest("Git Bash was not found; behavioural deploy scenarios are not run")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.sandbox = self._tmp
        self.base_env, self.paths = self._build_sandbox()
        probe = subprocess.run(
            [
                self.bash,
                "-c",
                'export PATH="$DAY16_SHIM_DIR:$PATH"; command -v id; id -u',
            ],
            env=self.base_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        lines = probe.stdout.splitlines()
        self.assertIn(
            "shims",
            lines[0] if lines else "",
            f"id resolves to {probe.stdout!r} / {probe.stderr!r}",
        )
        self.assertEqual(
            lines[1].strip() if len(lines) > 1 else "",
            "0",
            f"the id shim did not run: {probe.stdout!r} / {probe.stderr!r}",
        )

    # -- sandbox construction -------------------------------------------------
    def _build_sandbox(self):
        sandbox = self.sandbox
        shims = sandbox / "shims"
        repo = sandbox / "repo"
        project = repo / "week-04" / "day-16-mcp-agent"
        etc = sandbox / "etc"
        db_dir = sandbox / "db"
        venv_bin = sandbox / "venv" / "bin"
        logs = sandbox / "logs"
        marker = sandbox / "fetch.marker"

        for directory in (
            shims,
            repo / ".git",
            project / "harness",
            etc / "mcp.d",
            etc / "notifier.d",
            db_dir,
            venv_bin,
            logs,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        (project / ".env").write_text("MODEL=shim\n", encoding="utf-8")
        (etc / "search.env").write_text("TAVILY_API_KEY=shim-search-key\n", encoding="utf-8")

        notifier_db = db_dir / "day20-notifier.sqlite3"
        (etc / "notifier.env").write_text(
            "TELEGRAM_BOT_TOKEN=shim-token\n"
            "TELEGRAM_CHAT_ID=shim-chat\n"
            f"NOTIFIER_DB_PATH={_to_posix(notifier_db)}\n",
            encoding="utf-8",
        )

        notifier_python = venv_bin / "python"
        notifier_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")

        for name in SHIM_NAMES:
            shutil.copyfile(SHIM_LIB, shims / name)

        # Normalize the script copy so the sandbox run is not affected by the
        # working-copy line endings; the byte-level test covers normalization.
        raw = DEPLOY_SH.read_bytes()
        (sandbox / "deploy_vps.sh").write_bytes(
            raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        )

        env = dict(os.environ)
        env["PATH"] = str(shims) + os.pathsep + env.get("PATH", "")
        env["DAY16_REPO"] = _to_posix(repo)
        env["DAY16_SEARCH_ENV_FILE"] = _to_posix(etc / "search.env")
        env["DAY16_NOTIFIER_ENV_FILE"] = _to_posix(etc / "notifier.env")
        env["DAY16_NOTIFIER_DB_DIR"] = _to_posix(db_dir)
        env["DAY16_MCP_DROPIN_DIR"] = _to_posix(etc / "mcp.d")
        env["DAY16_MCP_DROPIN_FILE"] = _to_posix(etc / "mcp.d" / "day17-search.conf")
        env["DAY16_NOTIFIER_DROPIN_DIR"] = _to_posix(etc / "notifier.d")
        env["DAY16_NOTIFIER_DROPIN_FILE"] = _to_posix(etc / "notifier.d" / "day20-telegram.conf")
        env["DAY16_NOTIFIER_PYTHON"] = _to_posix(notifier_python)
        env["SHIM_LOG"] = _to_posix(logs / "shim.log")
        env["SHIM_DB_DIR"] = _to_posix(db_dir)
        env["SHIM_FETCH_MARKER"] = _to_posix(marker)
        env["SHIM_SERVERS_JSON"] = '{"servers": []}'
        # Git Bash prepends its own /usr/bin to PATH, so the sandbox run must
        # prepend the shim directory again inside the shell.
        env["DAY16_SHIM_DIR"] = _to_posix(shims)
        # Keep the sandbox isolated from any real credentials in the environment.
        for secret in ("TAVILY_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            env.pop(secret, None)

        executables = [shims / name for name in SHIM_NAMES] + [notifier_python]
        chmod = subprocess.run(
            [self.bash, "-c", 'chmod +x "$@"', "bash", *[_to_posix(path) for path in executables]],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        self.assertEqual(chmod.returncode, 0, "could not make sandbox commands executable")

        paths = {
            "shims": shims,
            "repo": repo,
            "project": project,
            "log": logs / "shim.log",
            "marker": marker,
        }
        return env, paths

    # -- helpers --------------------------------------------------------------
    def _run(self, sha, extra_env=None):
        env = dict(self.base_env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [
                self.bash,
                "-c",
                'export PATH="$DAY16_SHIM_DIR:$PATH"; exec bash "$@"',
                "bash",
                "deploy_vps.sh",
                sha,
            ],
            cwd=str(self.sandbox),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )

    def _shim_log(self) -> str:
        log = self.paths["log"]
        if not log.exists():
            return ""
        return log.read_text(encoding="utf-8", errors="replace")

    # -- scenarios ------------------------------------------------------------
    def test_already_deployed_skips_github_and_passes(self):
        result = self._run(SHA_NEW, {"SHIM_HEAD_SHA": SHA_NEW})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("skipping the GitHub fetch", result.stdout)
        self.assertIn("DEPLOY_STATUS: PASS", result.stdout)
        self.assertIn("already deployed", result.stdout)
        self.assertFalse(
            self.paths["marker"].exists(),
            "git fetch must not be called when HEAD already matches",
        )

    def test_new_commit_fetches_and_passes(self):
        result = self._run(SHA_NEW, {"SHIM_HEAD_SHA": SHA_OLD, "SHIM_FETCH_SHA": SHA_NEW})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DEPLOY_STATUS: PASS", result.stdout)
        self.assertNotIn("already deployed", result.stdout)
        self.assertTrue(self.paths["marker"].exists(), "git fetch should have been called")

    def test_fetch_timeout_reports_github_and_fails(self):
        result = self._run(SHA_NEW, {"SHIM_HEAD_SHA": SHA_OLD, "SHIM_TIMEOUT_EXIT": "124"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("github.com:443", result.stderr)
        self.assertIn("timed out", result.stderr)
        self.assertFalse(self.paths["marker"].exists())

    def test_missing_notifier_interpreter_fails_before_restarts(self):
        missing = self.sandbox / "venv" / "missing-python"
        result = self._run(
            SHA_NEW,
            {
                "SHIM_HEAD_SHA": SHA_NEW,
                "DAY16_NOTIFIER_PYTHON": _to_posix(missing),
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("203/EXEC", result.stderr)
        self.assertNotIn("systemctl restart", self._shim_log())
        self.assertNotIn("DEPLOY_STATUS: PASS", result.stdout)

    def test_notifier_failing_to_start_reports_inspection_command(self):
        result = self._run(
            SHA_NEW,
            {"SHIM_HEAD_SHA": SHA_NEW, "SHIM_FAIL_UNIT": "day16-notifier"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("day16-notifier", result.stderr)
        self.assertIn("did not become active after restart", result.stderr)
        self.assertIn("systemctl status day16-notifier --no-pager", result.stderr)
        self.assertNotIn("DEPLOY_STATUS: PASS", result.stdout)

    def test_tool_list_mismatch_reports_fail(self):
        result = self._run(
            SHA_NEW,
            {"SHIM_HEAD_SHA": SHA_NEW, "SHIM_PYTHON3_EXIT": "1"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DEPLOY_STATUS: FAIL", result.stderr)
        self.assertIn("tool names", result.stderr)


if __name__ == "__main__":
    unittest.main()
