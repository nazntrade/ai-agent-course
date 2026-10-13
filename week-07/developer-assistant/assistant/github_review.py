"""Fixed-origin GitHub adapter. PR code is bounded data, never executed."""
import base64
import hashlib
import json
import re
import os
import subprocess
from pathlib import PurePosixPath
import httpx
from .scope import SECRET,EXCLUDED

REPO=re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
SHA=re.compile(r'^[0-9a-f]{40}$')
DEFAULT_SCOPE='week-07/developer-assistant'
MARKER='<!-- developer-assistant-d32-review -->'
TEXT={'.py','.js','.ts','.tsx','.css','.html','.md','.txt','.json','.yaml','.yml','.toml'}

def repository(value):
    if not isinstance(value,str) or not REPO.fullmatch(value) or '..' in value:
        raise ValueError('Repository must be owner/name on github.com')
    return value

def scope_prefix(value):
    if not isinstance(value,str):raise ValueError('Invalid review scope')
    value=value.strip('/')
    if value and (not re.fullmatch(r'[A-Za-z0-9_./-]+',value) or any(p in {'..','.'} for p in value.split('/'))):
        raise ValueError('Scope must be a relative project directory')
    return value

def manifest_path(value):
    if not isinstance(value,str) or '\\' in value or len(value)>512:return False
    p=PurePosixPath(value)
    return bool(p.parts) and not p.is_absolute() and not any(x in {'.','..'} for x in p.parts) and not any(ord(c)<32 for c in value)

def safe_path(value):
    if not manifest_path(value):return False
    p=PurePosixPath(value)
    if any(x.lower() in EXCLUDED or SECRET.search(x) or 'private_key' in x.lower() for x in p.parts):return False
    if any('/'.join(p.parts[i:i+2]).lower() in {'docs/evidence','docs/specs'} for i in range(len(p.parts)-1)):return False
    return p.suffix.lower() in TEXT and p.name.lower() not in {'settings.json','package-lock.json'}

def in_scope(path,prefix):return not prefix or path.startswith(prefix+'/')

def changed_lines(patch):
    result=[];line=None
    for text in patch.splitlines():
        m=re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@',text)
        if m:line=int(m.group(1));continue
        if line is None:continue
        if text.startswith('+'):result.append(line);line+=1
        elif text.startswith(' '):line+=1
    return result

class GitHub:
    def __init__(self,token='',transport=None,bound_repo=None):self.token=token;self.transport=transport;self.bound_repo=bound_repo;self.cache={}
    async def request(self,method,path,body=None):
        if not path.startswith('/repos/') and path!='/user':raise ValueError('GitHub route denied')
        headers={'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'developer-assistant-d32'}
        if self.token:
            if self.bound_repo and not (path=='/repos/'+self.bound_repo or path.startswith('/repos/'+self.bound_repo+'/')):raise ValueError('GitHub owner credential is bound to the connected repository')
            headers['Authorization']='Bearer '+self.token
        async with httpx.AsyncClient(base_url='https://api.github.com',timeout=30,follow_redirects=False,transport=self.transport) as c:
            r=await c.request(method,path,json=body,headers=headers)
        if r.status_code not in {200,201,204}:raise ValueError(f'GitHub operation rejected (HTTP {r.status_code})')
        if len(r.content)>8_000_000:raise ValueError('GitHub response exceeds bounded limit')
        if r.status_code==204:return {}
        try:return r.json()
        except ValueError:raise ValueError('GitHub returned invalid JSON') from None
    async def pulls(self,repo):
        rows=await self.request('GET',f'/repos/{repository(repo)}/pulls?state=open&per_page=30')
        return [{'number':r['number'],'title':str(r['title'])[:300],'url':r['html_url'],'head_sha':r['head']['sha']} for r in rows]
    async def metadata(self,repo,number):
        repo=repository(repo)
        if type(number)!=int or not 1<=number<=10000000:raise ValueError('Invalid PR number')
        r=await self.request('GET',f'/repos/{repo}/pulls/{number}')
        for part in ('base','head'):
            if not SHA.fullmatch(r[part]['sha']):raise ValueError('Invalid GitHub snapshot SHA')
        return r
    async def comments(self,repo,number):
        result=[]
        for page in range(1,11):
            batch=await self.request('GET',f'/repos/{repository(repo)}/issues/{number}/comments?per_page=100&page={page}')
            result.extend(batch)
            if len(batch)<100:return result
        raise ValueError('Comment pagination budget exhausted; deduplication cannot be confirmed')
    async def text(self,repo,path,sha):
        from urllib.parse import quote
        if not safe_path(path) or not SHA.fullmatch(sha):raise ValueError('Source path or snapshot denied')
        r=await self.request('GET',f'/repos/{repository(repo)}/contents/{quote(path,safe="/")}?ref={sha}')
        if r.get('type')!='file' or r.get('size',0)>64000 or r.get('encoding')!='base64':raise ValueError('Source is unavailable, binary or oversized')
        try:text=base64.b64decode(r['content']).decode('utf-8-sig')
        except (ValueError,UnicodeError):raise ValueError('Source is not UTF-8 text') from None
        if '\x00' in text:raise ValueError('Binary source denied')
        return text
    async def collect(self,repo,number,prefix=DEFAULT_SCOPE,expected_head=None,expected_base=None):
        repo=repository(repo);prefix=scope_prefix(prefix);r=await self.metadata(repo,number)
        head=r['head']['sha'];base=r['base']['sha'];limitations=[]
        if expected_head and head!=expected_head:raise ValueError('PR head changed before collection')
        if expected_base and base!=expected_base:raise ValueError('PR base changed before collection')
        if r['head'].get('repo',{}).get('full_name')!=repo:raise ValueError('Fork PR review skipped: trusted same-repository scope only')
        cachekey=(repo,number,prefix,base,head)
        if cachekey in self.cache:return json.loads(json.dumps(self.cache[cachekey]))
        allfiles=[]
        for page in range(1,31):
            batch=await self.request('GET',f'/repos/{repo}/pulls/{number}/files?per_page=100&page={page}')
            allfiles.extend(batch)
            if len(batch)<100:break
        current=await self.metadata(repo,number)
        if current['head']['sha']!=head or current['base']['sha']!=base:
            raise ValueError('PR base/head changed during diff pagination; no snapshot or model review recorded')
        if len(allfiles)!=r.get('changed_files'):limitations.append('Changed-files API coverage differs from metadata (limit 3000)')
        files=[];outside=[]
        for f in allfiles:
            path=f['filename']
            if not in_scope(path,prefix):outside.append(path);continue
            patch=f.get('patch','');issues=[]
            if not safe_path(path):issues.append('Excluded secret, binary or unsupported text path');patch=''
            elif not patch:issues.append('Patch missing or binary; cannot claim full review')
            elif len(patch)>12000:issues.append('Patch exceeds per-file review budget');patch=''
            elif (sum(x.startswith('+') for x in patch.splitlines())!=f['additions'] or sum(x.startswith('-') for x in patch.splitlines())!=f['deletions']):
                issues.append('Patch hunk counts do not cover metadata additions/deletions');patch=''
            if issues:limitations.extend(path+': '+i for i in issues)
            files.append({'path':path,'status':f['status'],'previous_path':f.get('previous_filename'),'additions':f['additions'],
                          'deletions':f['deletions'],'patch':patch,'changed_lines':changed_lines(patch),'limitations':issues})
        if len(files)>12:limitations.append('More than 12 selected files; only first 12 can be reviewed')
        if outside:limitations.append(f'{len(outside)} changed files outside selected scope; not reviewed')
        if not files:limitations.append('No changed files within selected project scope')
        tree=await self.request('GET',f'/repos/{repo}/git/trees/{base}?recursive=1')
        head_tree=await self.request('GET',f'/repos/{repo}/git/trees/{head}?recursive=1')
        if tree.get('truncated'):limitations.append('Base tree truncated; context discovery incomplete')
        if head_tree.get('truncated'):limitations.append('Head tree truncated; source-mode coverage incomplete')
        trees={'base':{x['path']:x for x in tree.get('tree',[])},'head':{x['path']:x for x in head_tree.get('tree',[])}}
        candidates=[x['path'] for x in tree.get('tree',[]) if x.get('type')=='blob' and x.get('mode')!='120000' and in_scope(x['path'],prefix) and safe_path(x['path'])]
        stems=set()
        for f in files[:12]:
            stems.add(PurePosixPath(f['path']).stem);stems.update(re.findall(r'\b[A-Za-z_][A-Za-z0-9_]{3,}\b',f['patch'])[:30])
        docs=[p for p in candidates if PurePosixPath(p).suffix.lower() in {'.md','.txt','.json','.yaml','.yml'} and ('/docs/' in p or PurePosixPath(p).name.lower()=='readme.md')]
        directories={str(PurePosixPath(f['path']).parent) for f in files[:12]}
        related=sorted([p for p in candidates if p not in docs],key=lambda p:(-(20*(str(PurePosixPath(p).parent) in directories)+sum(s.lower() in p.lower() for s in stems)),p))
        sources=[];paths=list(dict.fromkeys(docs[:12]+related[:20]+[f['path'] for f in files[:12] if safe_path(f['path'])]))
        context_limits=[]
        if len(docs)>12 or len(related)>20:context_limits.append('Context discovery bounded to 12 documents and 20 related code files, not the whole repository')
        for path in paths:
            revisions=[('base',base)]
            if any(f['path']==path and f['status']!='removed' for f in files[:12]):revisions.append(('head',head))
            for kind,sha in revisions:
                try:
                    entry=trees[kind].get(path,{})
                    if entry.get('type')!='blob' or entry.get('mode') not in {'100644','100755'}:raise ValueError('Nonregular source denied')
                    text=await self.text(repo,path,sha)
                    sources.append({'repo':repo,'sha':sha,'revision':kind,'path':path,'text':text,'hash':hashlib.sha256(text.encode()).hexdigest(),
                                    'kind':'documentation' if ('/docs/' in path or PurePosixPath(path).name.lower()=='readme.md') else 'code'})
                except ValueError:
                    if not (kind=='base' and any(f['path']==path and f['status']=='added' for f in files)):
                        limitations.append(f'Context unavailable: {kind}:{path}')
        snapshot={'schema':1,'repo':repo,'number':number,'url':f'https://github.com/{repo}/pull/{number}','scope':prefix,'base_sha':base,'head_sha':head,
                'files':files[:12],'all_file_count':len(allfiles),'selected_file_count':len(files),'outside_scope':outside,
                'limitations':list(dict.fromkeys(limitations)),'context_limits':context_limits,'corpus_sources':sources}
        current=await self.metadata(repo,number)
        if current['head']['sha']!=head or current['base']['sha']!=base:
            raise ValueError('PR base/head changed during context collection; snapshot not cached')
        if len(self.cache)>=8:self.cache.pop(next(iter(self.cache)))
        self.cache[cachekey]=snapshot
        return json.loads(json.dumps(snapshot))
    async def publish(self,report,markdown):
        repo=repository(report['repo']);number=report['number']
        if (await self.metadata(repo,number))['head']['sha']!=report['head_sha']:return {'status':'stale','error':'New head prevents current publication'}
        comments=await self.comments(repo,number)
        # The workflow actor is a GitHub App bot; only update its own marked comment.
        old=next((c for c in reversed(comments) if c.get('user',{}).get('login')=='github-actions[bot]' and c.get('body','').startswith(MARKER)),None)
        if old:
            m=re.search(r'<!-- d32-json:([A-Za-z0-9+/=]+) -->',old['body'])
            try:previous=json.loads(base64.b64decode(m[1],validate=True)) if m else {}
            except (ValueError,TypeError):previous={}
            if previous.get('head_sha')==report['head_sha'] and previous.get('notification',{}).get('status') in {'sending','sent','failed','uncertain'}:
                report.setdefault('notification',previous['notification'])
        envelope=base64.b64encode(json.dumps(report,separators=(',',':')).encode()).decode()
        body=MARKER+'\n'+markdown+'\n\n<!-- d32-json:'+envelope+' -->'
        if len(body)>62000:raise ValueError('Validated report exceeds comment size limit')
        path=f'/repos/{repo}/issues/comments/{old["id"]}' if old else f'/repos/{repo}/issues/{number}/comments'
        r=await self.request('PATCH' if old else 'POST',path,{'body':body})
        return {'status':'published','url':r['html_url'],'comment_id':r['id']}

def project_owner_auth(root,repo):
    """Explicit owner UI action. Existing OS Git auth stays in application memory only."""
    repo=repository(repo)
    env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='Never')
    try:
        remote=subprocess.run(['git','remote','get-url','origin'],cwd=root,capture_output=True,text=True,timeout=10,env=env)
        origin=remote.stdout.strip() if remote.returncode==0 else ''
        if origin not in {f'https://github.com/{repo}',f'https://github.com/{repo}.git',f'git@github.com:{repo}',f'git@github.com:{repo}.git'}:
            raise ValueError('Connected repository must match the selected project trusted Git origin')
        filled=subprocess.run(['git','credential','fill'],input=f'protocol=https\nhost=github.com\npath={repo}.git\n\n',capture_output=True,text=True,timeout=15,env=env)
        token=next((x[9:] for x in filled.stdout.splitlines() if x.startswith('password=')),'') if filled.returncode==0 else ''
        if not token:raise ValueError('Existing Git login unavailable noninteractively; no credential file was read')
        return token
    except (OSError,subprocess.TimeoutExpired):raise ValueError('Existing Git login unavailable') from None
