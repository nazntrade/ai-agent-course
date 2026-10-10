"""Standard-library dispatcher behind the protected batch entrypoints."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from email.parser import BytesParser

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def clean_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AI_TEST_MODEL_", "AI_TEST_TAVILY_", "PIP_"))}
    env["AI_TEST_LIVE_POLICY"] = "forbidden"
    env["KNOWLEDGE_SKIP_ENV_FILE"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".pending")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def pip(arguments: list[str]) -> None:
    result = subprocess.run([sys.executable, "-m", "pip", "--isolated", *arguments], cwd=ROOT,
                            env=clean_environment(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        # Package-manager output can contain machine-specific configuration.
        raise RuntimeError("Pinned dependency preparation failed")


def setup() -> int:
    if sys.version_info[:2] != (3, 12) or sys.prefix == sys.base_prefix:
        print("SETUP_ERROR: Python 3.12 in the module virtual environment is required.")
        return 2
    lock = ROOT / "requirements.lock"
    pinned = {}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", line)
        if not match:
            raise RuntimeError("Unpinned dependency is not permitted")
        pinned[re.sub(r"[-_.]+", "-", match[1]).lower()] = match[2]
    if lock.exists():
        locked = {}
        for line in lock.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+) --hash=sha256:([a-f0-9]{64})", line)
            if not match:
                raise RuntimeError("Invalid dependency lock")
            locked[re.sub(r"[-_.]+", "-", match[1]).lower()] = match[2]
        if pinned != locked:
            raise RuntimeError("Pinned dependencies and lock differ; reviewed regeneration is required")
    if not lock.exists():
        with tempfile.TemporaryDirectory(prefix="private-chat-wheels-") as directory:
            folder = Path(directory)
            pip(["download", "--index-url", "https://pypi.org/simple", "--only-binary=:all:",
                 "-r", str(ROOT / "requirements.txt"), "-d", str(folder)])
            rows = []
            resolved = {}
            for wheel in sorted(folder.glob("*.whl")):
                with zipfile.ZipFile(wheel) as archive:
                    files = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
                    if len(files) != 1:
                        raise RuntimeError("Invalid wheel metadata")
                    metadata = BytesParser().parsebytes(archive.read(files[0]))
                name, version = metadata["Name"], metadata["Version"]
                if not name or not version or any(character in name + version for character in "\r\n "):
                    raise RuntimeError("Invalid wheel identity")
                digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
                resolved[re.sub(r"[-_.]+", "-", name).lower()] = version
                rows.append(f"{name}=={version} --hash=sha256:{digest}")
            if not rows:
                raise RuntimeError("No pinned wheels were resolved")
            if resolved != pinned:
                raise RuntimeError("Unexpected dependency resolution requires explicit pinned manifest review")
            text = "# Python 3.12 / Windows x64 wheels; generated once by setup.bat.\n" + "\n".join(sorted(rows, key=str.lower)) + "\n"
            pending = lock.with_name(lock.name + ".pending")
            pending.write_text(text, encoding="utf-8")
            # Validate all hashes and dependency closure before publishing the lock.
            pip(["install", "--no-index", "--find-links", str(folder), "--only-binary=:all:",
                 "--require-hashes", "-r", str(pending)])
            pending.replace(lock)
    else:
        pip(["install", "--index-url", "https://pypi.org/simple", "--only-binary=:all:",
             "--require-hashes", "-r", str(lock)])
    pip(["check"])
    save_json(ROOT / "docs/artifacts/dependency-provenance.json", {
        "schema_version": "dependency-provenance-v1", "scope": "P01 Python boundary",
        "python": platform.python_version(), "platform": platform.system(),
        "machine": platform.machine(), "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "require_hashes": True, "package_check": "PASS", "android_setup": "NOT_ASSESSED"})
    print("SETUP_STATUS: PASS (pinned Python dependencies).")
    if (ROOT/"android/app/build.gradle.kts").exists():
        from harness.android_setup import setup as android_setup
        return android_setup()
    return 0


def preflight() -> int:
    progress = ROOT / "docs/artifacts/preflight-progress.json"
    save_json(progress, {"schema_version": "preflight-progress-v1", "state": "started",
                         "run_id": os.environ.get("APP_RUN_ID", "")})
    # Fixed batch and fixed slug: no user-supplied shell fragments or paths.
    result = subprocess.run(["cmd.exe", "/d", "/c", str(ROOT / "test.bat"), "scenario", "model-preflight"],
                            cwd=ROOT, env=dict(os.environ))
    save_json(progress, {"schema_version": "preflight-progress-v1", "state": "finished", "exit_code": result.returncode,
                         "run_id": os.environ.get("APP_RUN_ID", "")})
    return result.returncode


def tests(mode: str) -> int:
    if mode == "live":
        policy = os.environ.get("AI_TEST_LIVE_POLICY", "")
        if policy not in {"", "allowed"}:
            print("LIVE_STATUS: BLOCKED (policy forbids real calls).")
            return 3
        if any(key.startswith("AI_TEST_MODEL_") for key in os.environ):
            from gateway.app.upstream import ProviderConfig, UpstreamError
            try:
                ProviderConfig.from_environment()
            except UpstreamError:
                print("LIVE_STATUS: BLOCKED (selected model profile is incomplete or invalid).")
                return 3
            from harness.center_integration import requested_scope
            try:
                scope=requested_scope(ROOT)
            except UpstreamError:
                print("INTEGRATION_STATUS: CONFIGURATION_ERROR (invalid explicit request).")
                return 2
            if scope:
                from harness.integration_child import main as integration_child
                return integration_child(scope)
            return preflight()
        import asyncio
        from harness.center_integration import requested_scope,main as center_integration
        from gateway.app.upstream import UpstreamError
        try:
            scope=requested_scope(ROOT)
        except UpstreamError:
            print("INTEGRATION_STATUS: CONFIGURATION_ERROR (invalid explicit request).")
            return 2
        if scope:
            return asyncio.run(center_integration(scope))
        from harness.center_preflight import main as center_preflight
        return asyncio.run(center_preflight())
    if mode not in {"all", "unit", "integration"}:
        return 2
    # Short tracebacks omit fixture argument reprs, including temporary auth tokens.
    args = [sys.executable, "-m", "pytest", "-q", "--tb=short"]
    if mode != "all":
        args += ["-m", mode]
    with tempfile.TemporaryDirectory(prefix="private-chat-results-") as directory:
        junit=Path(directory)/"results.xml"
        args += ["--junitxml",str(junit)]
        result=subprocess.run(args,cwd=ROOT,env=clean_environment()).returncode
        if junit.exists():
            tree=ET.parse(junit)
            cases=tree.findall(".//testcase")
            summary={"schema_version":"gateway-offline-verification-v1","scope":"offline isolated tests",
                     "technical_status":"PASS" if result==0 else "FAIL","test_mode":mode,
                     "tests_run":len(cases),"tests_failed":sum(bool(list(case.findall('failure'))+list(case.findall('error'))) for case in cases),
                     "test_names":[case.attrib.get("name","") for case in cases],
                     "model_calls":"NOT_RUN","criteria_acceptance":"PARTIAL","remaining":"LIVE, Android, external route, final independent acceptance"}
            save_json(ROOT/"docs/artifacts/gateway-offline-verification.json",summary)
    return result


def smoke() -> int:
    for package in ("fastapi", "pydantic", "httpx", "uvicorn", "gateway.app.upstream","gateway.app.main"):
        importlib.import_module(package)
    from gateway.app.config import Settings
    from gateway.app.main import create_app
    with tempfile.TemporaryDirectory(prefix="private-chat-schema-") as directory:
        app=create_app(Settings(Path(directory)))
        try:
            schema=app.openapi()
            if schema.get("openapi")!="3.1.0":
                raise RuntimeError("Unsupported OpenAPI version")
            if (ROOT/"android/app/build.gradle.kts").exists():
                from harness.generate_android_contract import check_saved,OUTPUT
                check_saved(schema,OUTPUT.read_bytes())
            save_json(ROOT/"docs/api/openapi.json",schema)
        finally:
            app.state.store.close()
    print("SMOKE_STATUS: PASS (isolated gateway imports and OpenAPI 3.1 export).")
    return 0


def run(mode: str) -> int:
    if mode == "network":
        from gateway.app.upstream import ProviderConfig, UpstreamError
        if os.environ.get("AI_TEST_LIVE_POLICY", "") not in {"", "allowed"}:
            print("SERVICE_STATUS: CONFIGURATION_ERROR (real calls forbidden by policy).")
            return 3
        if any(key.startswith("AI_TEST_MODEL_") for key in os.environ):
            try:
                ProviderConfig.from_environment()
            except (UpstreamError, ValueError):
                print("SERVICE_STATUS: CONFIGURATION_ERROR (invalid selected model profile).")
                return 3
        else:
            import asyncio
            from harness.center_service import main as center_service
            return asyncio.run(center_service())
    application = importlib.import_module("gateway.app.main")
    return application.main(mode)


def main(argv: list[str]) -> int:
    try:
        if argv == ["setup"]:
            return setup()
        if argv == ["preflight"]:
            return preflight()
        if len(argv)==2 and argv[0]=="integration-child" and argv[1] in {"android-emulator","integration-live"}:
            from harness.integration_child import main as integration_child
            return integration_child(argv[1])
        if argv == ["smoke"]:
            environment = clean_environment()
            os.environ.clear()
            os.environ.update(environment)
            return smoke()
        if len(argv) == 2 and argv[0] == "test":
            return tests(argv[1])
        if len(argv) == 2 and argv[0] == "run" and argv[1] in {"stub", "live", "local", "network"}:
            if not (ROOT / "gateway/app/main.py").exists():
                print("SETUP_ERROR: The gateway application is not implemented yet.")
                return 2
            return run(argv[1])
        print("SETUP_ERROR: Unsupported dispatcher arguments.")
        return 2
    except KeyboardInterrupt:
        print("RUNNER_STATUS: INTERRUPTED")
        return 130
    except Exception:
        print("RUNNER_STATUS: FAIL (bounded operation failed; private details suppressed).")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
