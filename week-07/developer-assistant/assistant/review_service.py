"""Review persistence and UI integration share the existing task worker/Stop boundary."""
import asyncio
import base64
import json
import re
from .store import ident,now
from .github_review import GitHub,repository,scope_prefix,DEFAULT_SCOPE,MARKER,project_owner_auth
from .review import run_review,validate_report,markdown
from .tools import send_report

class Reviews:
    def __init__(self,service):
        self.service=service;self.store=service.store;self.settings=service.settings
        self.github=GitHub(self.settings.github_key)
        with self.store.connect() as c:
            c.executescript('CREATE TABLE IF NOT EXISTS review_repositories(project_id TEXT PRIMARY KEY,repository TEXT,scope TEXT); CREATE TABLE IF NOT EXISTS review_imports(review_key TEXT PRIMARY KEY,task_id TEXT);')
    def connection(self,pid):
        self.store.get_project(pid)
        with self.store.connect() as c:r=c.execute('SELECT * FROM review_repositories WHERE project_id=?',(pid,)).fetchone()
        return dict(r) if r else {'project_id':pid,'repository':'','scope':DEFAULT_SCOPE}
    async def connect(self,pid,repo,prefix):
        self.store.get_project(pid);repo=repository(repo);prefix=scope_prefix(prefix)
        await self.github.request('GET',f'/repos/{repo}')
        with self.store.connect() as c:c.execute('INSERT INTO review_repositories VALUES (?,?,?) ON CONFLICT(project_id) DO UPDATE SET repository=excluded.repository,scope=excluded.scope',(pid,repo,prefix))
        return self.connection(pid)
    async def list(self,pid):
        conn=self.connection(pid)
        if not conn['repository']:return {'pulls':[],'connection':conn}
        return {'pulls':await self.github.pulls(conn['repository']),'connection':conn}
    async def authenticate(self,pid):
        conn=self.connection(pid);repo=conn['repository']
        if not repo:raise ValueError('Connect the selected Git repository first')
        token=await asyncio.to_thread(project_owner_auth,self.store.get_project(pid)['root'],repo)
        client=GitHub(token,bound_repo=repo)
        await client.request('GET',f'/repos/{repo}')
        self.github=client
        # Error/output scrubbing includes the in-memory credential; nothing persists it.
        self.settings.github_key=token
        return {'authenticated':True,'repository':repo,'scope':'Selected trusted Git origin only','credential_values_exposed':False}
    def create(self,pid,number,provider,chat_id=None,telegram=False):
        conn=self.connection(pid)
        if not conn['repository']:raise ValueError('Connect a GitHub repository first')
        if provider not in {'local','deepseek'}:raise ValueError('Invalid review provider')
        if type(number)!=int or not 1<=number<=10000000:raise ValueError('Invalid PR number')
        cid=self.store.resolve_chat(pid,chat_id)
        t=dict(id=ident(),project_id=pid,chat_id=cid,question=f"Review PR #{number} in {conn['repository']}",provider=provider,mode='review',status='queued',created=now(),
               answer='',partial='',sources=[],events=[],plan=[{'id':'review','title':'Fixed diff → RAG docs + code → selected model → validated report','status':'pending'}],quality='NOT_ASSESSED',grounding='NOT_ASSESSED',error=None,
               review_request={'repository':conn['repository'],'scope':conn['scope'],'number':number,'telegram':telegram})
        self.store.save_task(t);self.service.jobs[t['id']]=asyncio.create_task(self._run(t));return t
    async def _run(self,t):
        def save():
            if self.store.task(t['id'])['status']=='cancelled':raise asyncio.CancelledError()
            self.store.save_task(t)
        def event(e):t['events'].append(e);save()
        def partial(text):t['partial']=text;save()
        try:
            async with self.service.lock:
                t.update(status='running');t['plan'][0]['status']='running';save()
                req=t['review_request'];snapshot=await self.github.collect(req['repository'],req['number'],req['scope'])
                snapshot=json.loads(self.settings.scrub(json.dumps(snapshot,ensure_ascii=False)))
                event({'kind':'github','tool':'get_pr_diff','status':'completed','head_sha':snapshot['head_sha'],'base_sha':snapshot['base_sha'],'files':len(snapshot['files'])})
                report=await run_review(snapshot,self.service.provider,self.settings,t['provider'],event,partial)
                current=await self.github.metadata(req['repository'],req['number']);report['stale']=current['head']['sha']!=report['head_sha']
                t.update(review=report,answer=markdown(report),sources=[dict(s,label=f"{u['path']}:{s['label']}") for u in report['units'] for s in u['sources']],
                         status='completed',partial='',elapsed_s=report['elapsed_s'],grounding='REVIEW_REFERENCES_UNVERIFIED')
                t['plan'][0]['status']='completed';save()
                if req['telegram'] and not report['stale']:
                    try:t['notification']=await self.notify(t)
                    except ValueError as e:t['notification']={'status':'failed_or_uncertain','error':self.settings.scrub(str(e))}
                    save()
        except asyncio.CancelledError:
            current=self.store.task(t['id'])
            if current['status']!='cancelled':current.update(status='cancelled',partial='',answer='',error='Review interrupted');self.store.save_task(current)
        except Exception as e:
            if self.store.task(t['id'])['status']=='cancelled':return
            t.update(status='failed',answer='',partial='',error=self.settings.scrub(str(e)) if isinstance(e,ValueError) else 'Review failed; no clean review recorded')
            t['plan'][0]['status']='failed';self.store.save_task(t)
    async def notify(self,t):
        if t['status']!='completed' or not t.get('review'):raise ValueError('Only saved review results may be notified')
        r=t['review'];key=f"{r['repo']}:{r['number']}:{r['head_sha']}:{r.get('origin','manual')}"
        # Link all CI imports/manual reimports to one saved report ID per origin/head.
        with self.store.connect() as c:
            old=c.execute('SELECT task_id FROM review_imports WHERE review_key=?',(key,)).fetchone()
            if not old:c.execute('INSERT INTO review_imports VALUES (?,?)',(key,t['id']))
        return await send_report(self.settings,self.store,old['task_id'] if old else t['id'])
    async def sync(self,pid,number,cid=None,telegram=False,expected_repository=None):
        conn=self.connection(pid);repo=conn['repository']
        if not repo:raise ValueError('Connect a repository first')
        if expected_repository is not None and repo!=expected_repository:raise ValueError('Repository changed; report watcher must pause')
        head=(await self.github.metadata(repo,number))['head']['sha']
        comments=await self.github.comments(repo,number)
        imported=[]
        for c in comments:
            if c.get('user',{}).get('login')!='github-actions[bot]' or not c.get('body','').startswith(MARKER):continue
            match=re.search(r'<!-- d32-json:([A-Za-z0-9+/=]+) -->',c['body'])
            if not match or len(match[1])>70000:continue
            try:r=validate_report(json.loads(base64.b64decode(match[1],validate=True)))
            except (ValueError,TypeError,KeyError):raise ValueError('CI comment contains invalid review envelope') from None
            if r['repo']!=repo or r['number']!=number or r.get('scope')!=conn['scope']:raise ValueError('CI report crosses selected repository/project')
            r['origin']='ci';r['stale']=r['head_sha']!=head
            key=f"{repo}:{number}:{r['head_sha']}:ci"
            with self.store.connect() as db:old=db.execute('SELECT task_id FROM review_imports WHERE review_key=?',(key,)).fetchone()
            if old:
                saved=self.store.task(old['task_id']);saved['review']['stale']=r['stale'];self.store.save_task(saved)
                imported.append({'task_id':old['task_id'],'stale':r['stale'],'duplicate_prevented':True});continue
            t=dict(id=ident(),project_id=pid,chat_id=self.store.resolve_chat(pid,cid),question=f"CI review PR #{number} in {repo}",provider=r.get('provider','deepseek'),mode='review',status='completed',created=now(),
                   review=r,answer=markdown(r),partial='',sources=[dict(s,label=f"{u['path']}:{s['label']}") for u in r['units'] for s in u['sources']],events=[],plan=[],quality='NOT_ASSESSED',grounding='REVIEW_REFERENCES_UNVERIFIED',elapsed_s=r.get('elapsed_s',0))
            self.store.save_task(t)
            with self.store.connect() as db:db.execute('INSERT INTO review_imports VALUES (?,?)',(key,t['id']))
            if telegram and not r['stale']:
                # CI already sends its own owner notification; importing must not resend it.
                t['notification']=dict(r.get('notification',{'status':'not_recorded'}),duplicate_prevented=True);self.store.save_task(t)
            imported.append({'task_id':t['id'],'stale':r['stale'],'duplicate_prevented':False})
        return {'imported':imported,'head_sha':head,'message':'No CI report yet' if not imported else 'Validated CI report imported'}
