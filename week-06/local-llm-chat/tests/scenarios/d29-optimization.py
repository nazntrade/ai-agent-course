"""Trusted D29 owned-local LIVE reproduction; isolated chat DB, retained results."""
import json
import os
from pathlib import Path
import tempfile
import subprocess
from dataclasses import replace
from app.config import load_settings
from app.__main__ import build_service
from app.optimization import OptimizationLab

def main():
    settings=load_settings(os.environ)
    output=Path(os.environ.get('D29_OUTPUT_DIR','local-data/d29-reproduction')).resolve()
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='d29-owned-') as temp:
        settings=replace(settings,dialogue_db_path=str(Path(temp)/'dialogues.sqlite'))
        service=build_service(settings)
        try:
            lab=OptimizationLab(service);service.optimization=lab
            # Artifacts persist; benchmark chat state remains isolated in TEMP.
            lab.folder=output
            # Execute synchronously: cleanup cannot race a daemon's recovery.
            # Every load and HTTP call still has its configured finite timeout.
            def logged_start(args, **kwargs):
                with (output/'runtime-startup.log').open('ab') as log:
                    kwargs.update(stdout=log, stderr=log)
                    return subprocess.Popen(args, **kwargs)
            service.gemma._popen=logged_start
            lab.busy=True
            lab._run()
            result=lab.result
            if result.get('error') or len(result['baseline'])!=6 or len(result['optimized'])!=6 or result['truncation_probe']['answer']['finish_reason']!='length':raise RuntimeError('LIVE comparison incomplete; retained negative result')
            for name in ('baseline','optimized'):
                if any(r['resources']['sampling_error'] or not r['answer']['text'] for r in result[name]):raise RuntimeError('Missing answer/resource evidence')
            print('D29_LIVE_EXECUTION: PASS (12 real answers, verified contexts, resource counters and truncation; semantic review remains independent)')
            print('TEST_STATUS: PASS')
            return 0
        finally:service.close()

if __name__=='__main__':raise SystemExit(main())
