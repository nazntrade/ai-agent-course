"""Trusted D22 10-pair runner on an owned backend/index, never an existing port.

Completion is execution evidence; manual answer/source assessment stays separate.
The existing TestSession owns the lease. The backend lives in this process so an
abrupt parent shutdown cannot orphan another backend process.
"""
from __future__ import annotations
import dataclasses
import hashlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
import urllib.request

MODULE_DIR = Path(__file__).resolve().parent.parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))
from harness.live_policy import report_live_blocked
from harness.test_profile import TestSession, SessionError
from harness import rag_eval
from knowledge_agent.config import load_settings
from knowledge_agent.chat.test_profile import load_test_profile
from knowledge_agent.__main__ import build_service, build_chat_service
from knowledge_agent.api.app import create_app

class RunnerBlocked(RuntimeError):
    pass

class CleanupFailed(RuntimeError):
    pass


def is_stub(value):
    return str(value or '').lower().startswith(('stub', 'fake', 'mock', 'test-stub'))


def selected_source(settings):
    """Select only the registered evaluation corpus, not an arbitrary collection."""
    data = rag_eval.load_questions(rag_eval.QUESTIONS_PATH)
    label = (data.get('corpus') or {}).get('label')
    if not isinstance(label, str) or Path(label).name != label or not label or len(data.get('questions') or []) != 10:
        raise RunnerBlocked('registered evaluation corpus is invalid')
    path = Path(settings.source_path) if settings.source_path else MODULE_DIR / 'local-data' / 'input' / label
    if not path.is_absolute():
        path = MODULE_DIR / path
    if path.name != label or not path.is_file():
        raise RunnerBlocked('registered evaluation corpus is missing or does not match configured source')
    return path.resolve(), label


def verify_chat_identity(model, profile):
    info = model.preflight()
    identity = model.identity()
    if not info.get('reachable') or not info.get('model_present'):
        raise RunnerBlocked('selected chat provider is not ready')
    if identity.provider != 'openai-compatible' or identity.model != profile.name or is_stub(identity.model):
        raise RunnerBlocked('selected chat identity is inconsistent or a stub')
    options = identity.default_options
    if options.get('model_check_kind') != profile.kind:
        raise RunnerBlocked('selected chat kind is inconsistent')
    if profile.kind == 'local' and options.get('local_model_id') != profile.model_id:
        raise RunnerBlocked('selected local chat identity is inconsistent')
    return {'provider': identity.provider, 'model': identity.model,
            'kind': profile.kind, 'local_model_id': options.get('local_model_id'),
            'settings': dict(options)}


def ready_index(service, source, digest):
    """Build exactly the registered source in the owned fresh index."""
    collection = service.create_collection('d22-eval')['collection_id']
    build = service.build(collection, [{'path': str(source)}], 'structure', wait=True)
    version = service.get_index_version(build['index_version_id'])
    if version.get('status') != 'ready':
        raise RunnerBlocked('registered corpus index is not ready')
    sources = (version.get('manifest') or {}).get('sources') or []
    if len(sources) != 1 or sources[0].get('content_sha256') != digest:
        raise RunnerBlocked('ready index does not match the registered source')
    service.set_active_index(collection, version['index_version_id'])
    return collection, version


class OwnedBackend:
    """A prebound socket and Uvicorn thread owned by this process only."""
    def __init__(self, service, chat_service):
        self.service = service
        self.chat_service = chat_service
        self.socket = None
        self.server = None
        self.thread = None
        self.base_url = None

    def start(self):
        import uvicorn
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(('127.0.0.1', 0))
        self.base_url = f'http://127.0.0.1:{self.socket.getsockname()[1]}'
        config = uvicorn.Config(create_app(self.service, self.chat_service), host='127.0.0.1',
            port=self.socket.getsockname()[1], log_level='warning', access_log=False,
            timeout_graceful_shutdown=None)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={'sockets': [self.socket]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() >= deadline:
                raise RunnerBlocked('owned backend did not start')
            time.sleep(.05)
        return self.base_url

    def stop(self):
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=20)
            if self.thread.is_alive():
                # Never close the store/delete TEMP while a worker may still use it.
                raise CleanupFailed('owned backend did not drain; TEMP retained')
        if self.socket is not None:
            self.socket.close()


def verify_health(base, profile):
    target = urlsplit(base)
    if target.hostname != '127.0.0.1' or not target.port:
        raise RunnerBlocked('owned backend address is invalid')
    with urllib.request.urlopen(base + '/api/health', timeout=10) as response:
        health = json.load(response)
    chat = health.get('chat') or {}
    if not chat.get('reachable') or not chat.get('model_present') or chat.get('model') != profile.name:
        raise RunnerBlocked('owned backend does not use the selected ready chat profile')


def evaluate(base, collection, output, model_name):
    previous = (rag_eval.BASE, rag_eval.RUNS_PATH)
    old_model = os.environ.get('CHAT_MODEL')
    try:
        rag_eval.RUNS_PATH = output
        # Legacy evaluator prefers CHAT_MODEL for its summary. Pin it to the
        # verified profile instead of leaking a stale standalone model name.
        os.environ['CHAT_MODEL'] = model_name
        return rag_eval.main(['--base-url', base, '--collection', collection, '--live'])
    finally:
        rag_eval.BASE, rag_eval.RUNS_PATH = previous
        if old_model is None:
            os.environ.pop('CHAT_MODEL', None)
        else:
            os.environ['CHAT_MODEL'] = old_model


def completed_pairs(output):
    summaries = list(output.glob('eval-summary-*.json'))
    if len(summaries) != 1:
        raise RunnerBlocked('evaluation did not persist exactly one summary')
    summary = json.loads(summaries[0].read_text(encoding='utf-8'))
    pairs = summary.get('pairs') or []
    if len(pairs) != 10:
        raise RunnerBlocked('evaluation did not complete all ten pairs')
    run_ids = [pair.get(mode, {}).get('run_id') for pair in pairs for mode in ('with_rag', 'without_rag')]
    if len(set(run_ids)) != 20 or any(not isinstance(run_id, str) or Path(run_id).name != run_id for run_id in run_ids):
        raise RunnerBlocked('evaluation run identities are incomplete')
    for run_id in run_ids:
        if not (output / (run_id + '.json')).is_file():
            raise RunnerBlocked('evaluation run record is missing')
    return len(pairs)


def cleanup_temp(root, parent):
    target = root.resolve()
    if target.parent != parent or not target.name.startswith('knowledge-rag-eval-') or root.is_symlink():
        raise CleanupFailed('owned TEMP cleanup target is invalid')
    shutil.rmtree(target)


def run_owned(settings, source, label):
    if report_live_blocked('RAG_EVAL_RUNNER_STATUS'):
        return 3
    if settings.test_profile is None or is_stub(settings.test_profile.name):
        print('RAG_EVAL_RUNNER_STATUS: BLOCKED (selected profile required; no fallback)')
        return 3
    temporary_parent = Path(tempfile.gettempdir()).resolve()
    root = Path(tempfile.mkdtemp(prefix='knowledge-rag-eval-', dir=temporary_parent))
    output = MODULE_DIR / 'local-data' / 'chat-runs' / ('rag-eval-' + uuid.uuid4().hex)
    store = backend = None
    cleanup_ok = True
    receipt = {'schema_version': 'rag-eval-receipt-v1', 'quality_status': 'NOT_ASSESSED',
               'created_at': datetime.now(timezone.utc).isoformat(), 'source_label': label,
               'pairs_completed': 0, 'runner_status': 'FAIL', 'cleanup_status': 'NOT_RUN'}
    result = 1
    try:
        output.mkdir(parents=True, exist_ok=False)
        settings = dataclasses.replace(settings, db_path=str(root / 'index.db'), chat_runs_path=str(output),
                                       host='127.0.0.1', port=0)
        service, store = build_service(settings)
        chat = build_chat_service(settings, service)
        receipt['chat'] = verify_chat_identity(chat.chat_model, settings.test_profile)
        embedding = service._embedder.preflight()
        if (not embedding.get('reachable') or not embedding.get('model_present') or
                is_stub(embedding.get('version')) or is_stub(settings.embed_model)):
            raise RunnerBlocked('real configured embedding provider is not ready')
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        collection, version = ready_index(service, source, digest)
        receipt['source_sha256'] = digest
        receipt['index'] = {'index_version_id': version['index_version_id'], 'collection_id': collection,
                            'strategy': version['strategy'], 'fingerprint': version.get('fingerprint')}
        receipt['embedding'] = {'model': version.get('model'), 'dimension': version.get('dimension'),
                                'digest': version.get('digest')}
        backend = OwnedBackend(service, chat)
        base = backend.start()
        verify_health(base, settings.test_profile)
        result = evaluate(base, collection, output, settings.test_profile.name)
        if result == 0:
            receipt['pairs_completed'] = completed_pairs(output)
            receipt['runner_status'] = 'COMPLETED'
        else:
            receipt['runner_status'] = 'FAIL'
    except RunnerBlocked:
        print('RAG_EVAL_RUNNER_STATUS: BLOCKED (provider, corpus or index validation; no fallback)')
        result = 3
        receipt['runner_status'] = 'BLOCKED'
    except KeyboardInterrupt:
        receipt['runner_status'] = 'INTERRUPTED'
        result = 130
    except Exception:
        print('RAG_EVAL_RUNNER_STATUS: FAIL (owned evaluation failed)')
        result = 1
    finally:
        try:
            if backend is not None:
                backend.stop()
            if store is not None:
                store.close()
            cleanup_temp(root, temporary_parent)
        except Exception:
            cleanup_ok = False
            result = 1
            print('RAG_EVAL_CLEANUP_STATUS: FAIL (owned resources retained; no shared process stopped)')
        receipt['cleanup_status'] = 'PASS' if cleanup_ok else 'FAIL'
        if output.is_dir():
            (output / 'rag-eval-receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            print('RAG_EVAL_RESULT_DIRECTORY: ' + output.relative_to(MODULE_DIR).as_posix())
        if cleanup_ok:
            print('RAG_EVAL_CLEANUP_STATUS: PASS')
    print('RAG_EVAL_QUALITY_STATUS: NOT_ASSESSED (manual answer/source evaluations required)')
    return result


def main(argv=None):
    if report_live_blocked('RAG_EVAL_RUNNER_STATUS'):
        return 3
    arguments = sys.argv[1:] if argv is None else argv
    if arguments:
        print('RAG_EVAL_RUNNER_STATUS: setup error (this trusted mode accepts no arguments)')
        return 2
    env = dict(os.environ)
    try:
        try:
            profile = load_test_profile(env)
        except ValueError:
            raise RunnerBlocked('selected chat profile is invalid') from None
        if profile is None or is_stub(profile.name):
            raise RunnerBlocked('a complete selected non-stub chat profile is required')
        settings = load_settings(env)
        source, label = selected_source(settings)
        # Reuse the existing lifecycle implementation; a borrowed ready session
        # neither acquires another lease nor releases its parent lease.
        with TestSession(env) as ready_env:
            settings = load_settings(ready_env)
            result = run_owned(settings, source, label)
        return result
    except (RunnerBlocked, SessionError):
        print('RAG_EVAL_RUNNER_STATUS: BLOCKED (selected profile, corpus or lifecycle unavailable)')
        return 3
    except KeyboardInterrupt:
        print('RAG_EVAL_RUNNER_STATUS: INTERRUPTED')
        return 130
    except Exception:
        print('RAG_EVAL_RUNNER_STATUS: setup error (configuration or result storage unavailable)')
        return 2

if __name__ == '__main__':
    sys.exit(main())
