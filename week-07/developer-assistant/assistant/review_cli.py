"""Trusted-base CLI. The PR head is only read through GitHub REST."""
import argparse
import asyncio
import json
import os
import hashlib
from pathlib import Path
import httpx
from .config import Settings
from .runtime import LocalRuntime
from .provider import Provider
from .github_review import GitHub,repository,DEFAULT_SCOPE
from .review import run_review,markdown

def append_summary(settings,text,output):
    summary=os.getenv('GITHUB_STEP_SUMMARY')
    if not summary:return None
    # GitHub supplies the step-owned file. Read only this exact file after our write,
    # never an arbitrary repository path or credential file.
    with open(summary,'a',encoding='utf-8') as f:f.write(settings.scrub(text)+'\n')
    content=Path(summary).read_bytes()
    (output/'job-summary.md').write_bytes(content)
    receipt={'file':'job-summary.md','bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()}
    (output/'summary-receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    return receipt

def publication_state(report,result):
    report['publication']=result
    if result.get('status')=='stale':report['stale']=True
    return result.get('status')=='published'

async def execute(args):
    settings=Settings.load();runtime=LocalRuntime(settings);github=GitHub(settings.github_key)
    repo=None;number=args.pr;expected_head=None;expected_base=None
    folder=Path(args.output);folder.mkdir(parents=True,exist_ok=True)
    try:
        configured_model=os.getenv('D32_DEEPSEEK_MODEL','')
        if configured_model:
            import re
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}',configured_model):raise ValueError('Invalid CI model name')
            settings.remote_model=configured_model
        repo=repository(args.repo) if args.repo else None
        if args.event:
            event=json.loads(Path(args.event).read_text(encoding='utf-8'))
            repo=repository(event['repository']['full_name']);pr=event['pull_request'];number=pr['number']
            expected_head=pr['head']['sha'];expected_base=pr['base']['sha']
            if pr['head'].get('repo',{}).get('full_name')!=repo:raise ValueError('Fork event safely skipped before model invocation')
        if not repo or not number:raise ValueError('Supply repository/PR or GitHub event JSON')
        snapshot=await github.collect(repo,number,args.scope,expected_head,expected_base)
        snapshot=json.loads(settings.scrub(json.dumps(snapshot,ensure_ascii=False)))
        report=await run_review(snapshot,Provider(settings,runtime),settings,args.provider,origin='ci' if args.event else 'cli')
        if (await github.metadata(repo,number))['head']['sha']!=report['head_sha']:report['stale']=True
        else:report['stale']=False
        text=markdown(report)
        if args.publish and not report['stale']:
            try:publication_state(report,await github.publish(report,text))
            except ValueError as e:report['publication']={'status':'failed','error':settings.scrub(str(e))}
        if args.telegram and not report['stale'] and report.get('publication',{}).get('status')=='published':
            try:
                report['notification']=await notify_owner(settings,github,report)
                # Keep the delivery record inside the same owned head-labelled comment.
                if not report['stale']:publication_state(report,await github.publish(report,markdown(report)))
            except ValueError as e:report['notification']={'status':'failed_or_uncertain','error':settings.scrub(str(e))}
        text=markdown(report)
        report['summary_receipt']=append_summary(settings,text,folder)
        (folder/'review.json').write_text(settings.scrub(json.dumps(report,ensure_ascii=False,indent=2)),encoding='utf-8')
        (folder/'review.md').write_text(settings.scrub(text),encoding='utf-8')
        print(json.dumps({'status':report['status'],'quality':report['quality'],'head_sha':report['head_sha'],'file_count':len(report['units']),
                          'publication':report.get('publication'),'notification':report.get('notification'),'summary_receipt':report['summary_receipt']},ensure_ascii=False))
        return 1 if report.get('publication',{}).get('status')=='failed' else 0
    except Exception as e:
        message=settings.scrub(str(e)) if isinstance(e,ValueError) else 'Review pipeline failed; no successful review recorded'
        failure={'status':'failed','quality':'NOT_ASSESSED','error':message,'repo':repo,'number':number,'head_sha':expected_head,'base_sha':expected_base}
        failure_text='# AI review failed\n\n'+message+'\n\nNo clean review was recorded.'
        (folder/'review.md').write_text(failure_text,encoding='utf-8')
        failure['summary_receipt']=append_summary(settings,failure_text,folder)
        (folder/'review.json').write_text(json.dumps(failure,indent=2),encoding='utf-8')
        print(json.dumps(failure));return 1
    finally:await runtime.close()

async def notify_owner(settings,github,report):
    if report.get('stale'):return {'status':'skipped_stale','duplicate_prevented':True}
    if not settings.telegram_token or not settings.telegram_chat:return {'status':'not_configured'}
    # The marked comment persists delivery intent across CI reruns; uncertain sends are not retried.
    old=await github.comments(report['repo'],report['number'])
    from .github_review import MARKER
    import base64,re
    for c in old:
        if c.get('user',{}).get('login')!='github-actions[bot]' or not c.get('body','').startswith(MARKER):continue
        match=re.search(r'<!-- d32-json:([A-Za-z0-9+/=]+) -->',c['body'])
        if match:
            try:previous=json.loads(base64.b64decode(match[1],validate=True))
            except ValueError:continue
            if previous.get('head_sha')==report['head_sha'] and previous.get('notification',{}).get('status') in {'sending','sent','uncertain','failed'}:
                return dict(previous['notification'],duplicate_prevented=True)
    report['notification']={'status':'sending'}
    if not publication_state(report,await github.publish(report,markdown(report))):
        return {'status':'skipped_stale' if report.get('stale') else 'skipped_unpublished','duplicate_prevented':True}
    count=sum(len(u.get('result',{}).get('findings',[])) for u in report['units'])
    text=f"D32 AI review · PR #{report['number']}\nProvider: {report['provider']}\nExecution: {report['status']} · Findings: {count}\nQuality: NOT_ASSESSED\nHead: {report['head_sha'][:12]}\n{report['url']}"
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            response=await c.post('https://api.telegram.org/bot'+settings.telegram_token+'/sendMessage',json={'chat_id':settings.telegram_chat,'text':text,'disable_web_page_preview':True})
        data=response.json()
        if response.status_code!=200 or not data.get('ok'):return {'status':'failed','duplicate_prevented':False}
        return {'status':'sent','message_id':data['result']['message_id'],'duplicate_prevented':False}
    except Exception:return {'status':'uncertain','duplicate_prevented':False}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--repo');p.add_argument('--pr',type=int);p.add_argument('--event')
    p.add_argument('--scope',default=DEFAULT_SCOPE);p.add_argument('--provider',choices=['local','deepseek'],default='deepseek')
    p.add_argument('--output',default='local-data/review-output');p.add_argument('--publish',action='store_true');p.add_argument('--telegram',action='store_true')
    raise SystemExit(asyncio.run(execute(p.parse_args())))

if __name__=='__main__':main()
