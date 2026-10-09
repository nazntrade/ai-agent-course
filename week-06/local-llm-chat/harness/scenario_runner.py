"""Fixed, manifest-declared test scenarios; no arbitrary paths or commands.

This dispatcher is an entrypoint validator, not a sandbox for project code.
Scenario owners retain responsibility for their own resources and bounded work.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import runpy
import stat
import sys
from urllib.parse import quote, urlsplit

SLUG = re.compile(r'[a-z][a-z0-9-]{0,63}', re.ASCII)

class ScenarioBlocked(ValueError):
    pass


def checked_path(path: Path, *, directory: bool = False) -> Path:
    """Reject links/junctions at every ancestor before reading project code."""
    path = path.absolute()
    for candidate in (*reversed(path.parents), path):
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
            raise ValueError('linked scenario path is forbidden')
        if candidate != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError('invalid scenario ancestor')
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('invalid scenario file type')
    return path


def validate_live_profile(env):
    if env.get('AI_TEST_LIVE_POLICY') != 'allowed':
        raise ScenarioBlocked('LIVE policy does not allow this scenario')
    value = lambda key: str(env.get('AI_TEST_MODEL_' + key, '')).strip()
    kind, base, name, key = (value(k) for k in ('KIND', 'BASE_URL', 'NAME', 'API_KEY'))
    if kind not in ('local', 'remote') or not all((base, name, key)):
        raise ScenarioBlocked('complete selected profile required')
    try:
        parsed = urlsplit(base)
        port = parsed.port
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError()
        loopback = parsed.hostname in ('127.0.0.1', 'localhost', '::1')
        if (kind == 'local' and not loopback) or (kind == 'remote' and parsed.scheme != 'https' and not loopback):
            raise ValueError()
        model_id, lease, inherited = (value(k) for k in ('ID', 'LEASE_URL', 'LEASE_ID'))
        if kind == 'local' and not all((model_id, lease)):
            raise ValueError()
        if lease:
            target = urlsplit(lease)
            if (kind != 'local' or not model_id or target.scheme != parsed.scheme or target.hostname != parsed.hostname or target.port != port or
                target.path != '/api/local-models/' + quote(model_id, safe='') + '/test-leases' or
                target.username or target.password or target.query or target.fragment):
                raise ValueError()
        if inherited and (kind != 'local' or not lease or env.get('AI_TEST_MODEL_PARENT_READY') != '1'):
            raise ValueError()
    except ValueError:
        raise ScenarioBlocked('selected profile endpoint or lifecycle is invalid') from None


def execute(project_root: Path, arguments) -> int:
    if len(arguments) != 1 or not isinstance(arguments[0], str) or not SLUG.fullmatch(arguments[0]):
        print('SCENARIO_STATUS: setup error (exactly one ASCII scenario slug required)')
        return 2
    slug = arguments[0]
    try:
        root = checked_path(Path(project_root), directory=True)
        folder = checked_path(root / 'tests' / 'scenarios', directory=True)
        script = checked_path(folder / (slug + '.py'))
        declaration = checked_path(folder / (slug + '.json'))
        if script.resolve().parent != folder.resolve() or declaration.resolve().parent != folder.resolve():
            raise ValueError('scenario escaped its fixed directory')
        manifest = json.loads(declaration.read_text(encoding='utf-8'))
        if not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'kind'} or manifest['schema_version'] != 'test-scenario-v1' or manifest['kind'] not in ('live', 'offline', 'owned-local'):
            raise ValueError('invalid scenario declaration')
        if manifest['kind'] == 'owned-local':
            # Explicit standalone ownership, not a borrowed lease or silent fallback.
            if os.environ.get('D29_ENABLE_OWNED_LOCAL') != '1' or os.environ.get('AI_TEST_LIVE_POLICY') != 'allowed':
                raise ScenarioBlocked('owned local LIVE was not explicitly enabled')
            if any(str(v).strip() for k,v in os.environ.items() if k.startswith('AI_TEST_MODEL_')):
                raise ScenarioBlocked('owned local cannot replace an external test profile')
            if os.environ.get('APP_SKIP_ENV_FILE') != '1' or not all(Path(os.environ.get(k,'')).is_file() for k in ('GEMMA_RUNTIME_PATH','GEMMA_GGUF_PATH')):
                raise ScenarioBlocked('owned local runtime/model and environment isolation required')
        if manifest['kind'] == 'live':
            validate_live_profile(os.environ)
    except ScenarioBlocked:
        print('SCENARIO_STATUS: BLOCKED (LIVE policy/profile; real calls NOT_RUN)')
        return 3
    except (OSError, ValueError, TypeError):
        print('SCENARIO_STATUS: setup error (scenario path or declaration invalid)')
        return 2

    old_env, old_argv, old_path = dict(os.environ), sys.argv, sys.path
    argv_values, path_values = list(sys.argv), list(sys.path)
    old_main = sys.modules.get('__main__')
    try:
        if manifest['kind'] == 'offline':
            for key in tuple(os.environ):
                if key.startswith(('AI_TEST_MODEL_', 'AI_TEST_TAVILY_')):
                    os.environ.pop(key, None)
            os.environ['AI_TEST_LIVE_POLICY'] = 'forbidden'
            os.environ['KNOWLEDGE_SKIP_ENV_FILE'] = '1'
        sys.argv = [str(script)]
        sys.path = [str(root), *path_values]
        runpy.run_path(str(script), run_name='__main__')
        result = 0
    except SystemExit as error:
        result = error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
    except KeyboardInterrupt:
        result = 130
    except Exception:
        # Never print an arbitrary scenario traceback carrying keys/endpoints.
        print('SCENARIO_STATUS: FAIL (scenario raised an error)')
        result = 1
    finally:
        os.environ.clear()
        os.environ.update(old_env)
        old_argv[:] = argv_values
        old_path[:] = path_values
        sys.argv, sys.path = old_argv, old_path
        if old_main is None:
            sys.modules.pop('__main__', None)
        else:
            sys.modules['__main__'] = old_main
    print('SCENARIO_STATUS: ' + ('PASS' if result == 0 else 'INTERRUPTED' if result == 130 else 'BLOCKED' if result == 3 else 'FAIL'))
    return result


def main(argv=None):
    root = Path(__file__).absolute().parent.parent
    return execute(root, sys.argv[1:] if argv is None else argv)

if __name__ == '__main__':
    sys.exit(main())
