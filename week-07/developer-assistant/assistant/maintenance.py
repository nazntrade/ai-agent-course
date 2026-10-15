"""Versioned project drafts and guarded owner-only filesystem transactions (D34)."""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import ast
import base64
import difflib
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import uuid

MAX_FILE=512000
MAX_TOTAL=32*1024*1024
MAX_FILES=2048
MISSING='MISSING'
PROTECTED={'agents.md','project_rules.md','module_rules.md','workflow.md'}
EXCLUDED={'.git','.venv','venv','node_modules','__pycache__','.pytest_cache','local-data','dist','build','target','vendor'}
EXTENSIONS={'.md','.txt','.py','.js','.ts','.html','.css','.json','.yaml','.yml','.toml','.csv','.sql','.rst'}
RESERVED=re.compile(r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$',re.I)

class Conflict(ValueError):pass
class ScopeError(ValueError):pass
class ContextLimit(Conflict):pass

def digest(data):return hashlib.sha256(data).hexdigest()
def encoded(value):return base64.urlsafe_b64encode(json.dumps(value,separators=(',',':')).encode()).decode()
def decoded(value):
    try:return json.loads(base64.urlsafe_b64decode(value))
    except Exception:raise Conflict('Invalid continuation token') from None

def durable(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp');created=False;identity=None
    try:
        with temp.open('xb') as f:
            created=True;info=os.fstat(f.fileno());identity=(info.st_dev,info.st_ino)
            f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    except BaseException as original:
        if created:
            try:
                info=temp.lstat()
                if identity is None or not identity[1] or (info.st_dev,info.st_ino)!=identity or not stat.S_ISREG(info.st_mode):raise OSError('Staging identity changed/unavailable; substituted object preserved')
                temp.unlink()
            except FileNotFoundError:pass
            except BaseException as cleanup:
                original.staging_cleanup_failure={'path':str(temp),'identity':list(identity) if identity else None,'error':type(cleanup).__name__+': '+str(cleanup)}
                original.add_note('Owned staging cleanup failed: '+json.dumps(original.staging_cleanup_failure))
        raise

def validate_files_budget(value):
    required={'schema_version','selection','profile_default_chars','effective_chars','source'}
    if not isinstance(value,dict) or set(value) not in (required,required|{'retry_of'}):raise ValueError('Invalid files context budget snapshot')
    if type(value['schema_version']) is not int or value['schema_version']!=1:raise ValueError('Invalid files context budget schema')
    if type(value['profile_default_chars']) is not int or not 1000<=value['profile_default_chars']<=40000 or type(value['effective_chars']) is not int:raise ValueError('Invalid files context budget integer/range')
    selection=value['selection'];source=value['source']
    if selection not in ('profile_default','expanded_40000','expanded_100000') or source not in ('profile_default','explicit_run','retry_preserved','legacy_retry_profile_default'):raise ValueError('Invalid files context budget selection/source')
    expected={'profile_default':value['profile_default_chars'],'expanded_40000':40000,'expanded_100000':100000}[selection]
    if value['effective_chars']!=expected:raise ValueError('Inconsistent files context budget snapshot')
    if source=='profile_default' and selection!='profile_default' or source=='explicit_run' and selection=='profile_default' or source=='legacy_retry_profile_default' and selection!='profile_default':raise ValueError('Inconsistent files context budget origin')
    retry=source in ('retry_preserved','legacy_retry_profile_default')
    if retry!=('retry_of' in value) or retry and (not isinstance(value['retry_of'],str) or not 1<=len(value['retry_of'])<=64):raise ValueError('Invalid files context budget retry binding')
    return json.loads(json.dumps(value))

def new_files_budget(default,choice=None,*,retry_of=None):
    if choice is not None and (type(choice) is not int or choice not in (40000,100000)):raise ValueError('Files context override must be integer40000, integer100000 or null')
    if type(default) is not int or not 1000<=default<=40000:raise ValueError('Invalid profile default files context budget')
    value={'schema_version':1,'selection':'expanded_'+str(choice) if choice is not None else 'profile_default','profile_default_chars':default,'effective_chars':choice if choice is not None else default,'source':'explicit_run' if choice is not None else 'profile_default'}
    if retry_of is not None:value.update(source='legacy_retry_profile_default',retry_of=retry_of)
    return validate_files_budget(value)

def linked(path):
    info=path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info,'st_file_attributes',0)&0x400)

def checked_root(root):
    root=Path(root).absolute()
    for p in (*reversed(root.parents),root):
        if linked(p):raise ScopeError('Linked project root or ancestor denied')
    if not root.is_dir():raise ScopeError('Project directory missing')
    return root.resolve()

def permitted(path,write=False):
    if not isinstance(path,str) or not path or len(path)>240 or '\\' in path or ':' in path or path.startswith('/'):
        raise ScopeError('Only relative POSIX file paths are allowed')
    parts=path.split('/')
    for part in parts:
        if part in {'','.','..'} or part[-1:] in {' ','.'} or RESERVED.fullmatch(part):raise ScopeError('Invalid path component')
        low=part.lower()
        if low in EXCLUDED or low.startswith('.') or any(x in low for x in ('credential','secret','token','password','settings','config')):
            raise ScopeError('Excluded project path')
    name=parts[-1].lower()
    if Path(name).suffix not in EXTENSIONS or name.endswith(('.bat','.pem','.key')):raise ScopeError('Unsupported file type')
    if write and name in PROTECTED:raise ScopeError('Project rules are read-only')
    return path

def checked(root,path,write=False):
    permitted(path,write)
    root=checked_root(root);current=root
    for part in path.split('/'):
        if current.exists():
            aliases=[p.name for p in current.iterdir() if p.name.casefold()==part.casefold()]
            if aliases and aliases!=[part]:raise ScopeError('Ambiguous or aliased path case')
        current=current/part
        if current.exists() or current.is_symlink():
            if linked(current):raise ScopeError('Linked path denied')
            if current.is_file() and current.stat().st_nlink>1 and write:raise ScopeError('Hardlinked target denied')
    if not current.is_relative_to(root):raise ScopeError('Path escapes selected project')
    return current

def inventory(root):
    root=checked_root(root);files={};excluded={};total=0;complete=True
    def omit(reason):excluded[reason]=excluded.get(reason,0)+1
    def walk(folder):
        nonlocal total,complete
        entries=sorted(folder.iterdir(),key=lambda p:(p.name.casefold(),p.name))
        folded={}
        for p in entries:folded.setdefault(p.name.casefold(),[]).append(p.name)
        for p in entries:
            rel=p.relative_to(root).as_posix()
            if len(folded[p.name.casefold()])>1:omit('case_collision');continue
            if linked(p):omit('linked');continue
            if p.is_dir():
                if p.name.lower() in EXCLUDED or p.name.startswith('.'):omit('excluded_directory');continue
                walk(p);continue
            try:permitted(rel)
            except ScopeError:omit('policy');continue
            if not p.is_file():omit('nonregular');continue
            if len(files)>=MAX_FILES:complete=False;omit('file_budget');continue
            if p.stat().st_size>MAX_FILE:omit('oversized');continue
            if total+p.stat().st_size>MAX_TOTAL:complete=False;omit('byte_budget');continue
            try:
                data=p.read_bytes();text=data.decode('utf-8')
                if '\x00' in text:omit('binary');continue
            except (OSError,UnicodeError):omit('unreadable_or_binary');continue
            files[rel]={'sha256':digest(data),'bytes':len(data)};total+=len(data)
    walk(root)
    version=digest(json.dumps({'files':files,'exclusions':excluded,'complete':complete},sort_keys=True).encode())
    return {'files':files,'exclusions':excluded,'complete':complete,'bytes':total,'version':version}

class WorkspaceLease:
    """An OS-held byte lock shared across app data directories and processes."""
    def __init__(self,root):
        key=digest(os.path.normcase(str(checked_root(root))).encode())
        folder=Path(tempfile.gettempdir())/'developer-assistant-d34-locks';folder.mkdir(exist_ok=True)
        self.path=folder/(key+'.lock');self.file=None
    def __enter__(self):
        self.file=self.path.open('a+b')
        try:
            if os.fstat(self.file.fileno()).st_size==0:self.file.write(b'0');self.file.flush()
            self.file.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:self.file.close();self.file=None;raise Conflict('Selected workspace has another active writer') from None
        return self
    def __exit__(self,*args):
        if self.file:
            self.file.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_UN)
            self.file.close();self.file=None

def workspace_state(root):
    return WorkspaceLease(root).path.with_suffix('.json')

def workspace_guard(root):
    marker=workspace_state(root)
    if marker.exists():
        value=json.loads(marker.read_text(encoding='utf-8'))
        manifest=Path(value['manifest'])
        if not manifest.is_file():raise Conflict('Workspace journal is unavailable; recovery required')
        m=json.loads(manifest.read_text(encoding='utf-8'));j=m.get('journal')
        if j and j['status'] not in {'applied','undone','rolled_back','noop'}:raise Conflict('Workspace has an unresolved interrupted transaction')

@contextmanager
def run_guard(folder,wait=False):
    # Separate lock: cancellation and each complete MCP operation serialize.
    # Workspace lock is held by the parent job, never reacquired by its child.
    lease=WorkspaceLease(folder);deadline=time.monotonic()+15
    while True:
        try:lease.__enter__();break
        except Conflict:
            if not wait or time.monotonic()>=deadline:raise
            time.sleep(.02)
    try:yield
    finally:lease.__exit__(None,None,None)

class Run:
    def __init__(self,folder,token=None):
        self.folder=Path(folder);self.path=self.folder/'manifest.json';self.m=json.loads(self.path.read_text(encoding='utf-8'));self.token=token
        self._budget=validate_files_budget(self.m['files_context_budget']) if 'files_context_budget' in self.m else None
    @classmethod
    def create(cls,data,root,run_id,project_id,chat_id,goal,provider,files_context_budget=None):
        budget=validate_files_budget(files_context_budget) if files_context_budget is not None else None
        root=checked_root(root);workspace_guard(root);folder=Path(data)/'maintenance'/run_id
        if folder.exists():raise Conflict('Run already exists')
        folder.mkdir(parents=True);(folder/'baseline').mkdir();(folder/'draft').mkdir();(folder/'revisions').mkdir()
        view=inventory(root)
        for path,meta in view['files'].items():
            blob=checked(root,path).read_bytes()
            if digest(blob)!=meta['sha256']:raise Conflict('Project changed during snapshot')
            target=folder/'baseline'/path;target.parent.mkdir(parents=True,exist_ok=True);durable(target,blob)
        m={'schema_version':'maintenance-run-v1','id':run_id,'project_id':project_id,'chat_id':chat_id,'root':str(root),'goal':goal,'provider':provider,
           'generation':uuid.uuid4().hex,'active':True,'revision':0,'inventory':view,'draft':{},'reads':{},'inventory_dependency':False,
           'stage':'investigating' if view['complete'] else 'incomplete','checks':{},'quality':'NOT_ASSESSED','goal_review':'NOT_ASSESSED',
           'application':'NOT_APPLIED','events':[],'review':None,'journal':None,'edits':0}
        if budget is not None:m['files_context_budget']=budget
        durable(folder/'manifest.json',json.dumps(m,ensure_ascii=False,indent=2).encode())
        return cls(folder,m['generation'])
    def budget_guard(self):
        actual=validate_files_budget(self.m['files_context_budget']) if 'files_context_budget' in self.m else None
        if actual!=self._budget:raise Conflict('Immutable run files context budget changed')
    def reload(self):self.m=json.loads(self.path.read_text(encoding='utf-8'));self.budget_guard();return self.m
    def save(self):self.budget_guard();durable(self.path,json.dumps(self.m,ensure_ascii=False,indent=2).encode())
    def active(self):
        self.reload()
        if self.token and (not self.m['active'] or self.token!=self.m['generation']):raise Conflict('Run was cancelled or superseded')
    def writable(self):
        self.active()
        if self.m.get('journal') and self.m['journal']['status'] not in {'applied','undone','rolled_back','noop'}:raise Conflict('Workspace requires recovery')
    def cancel(self):
        with run_guard(self.folder,wait=True):
            self.reload();j=self.m.get('journal');stage='recovery_required' if j and j['status'] not in {'applied','undone','noop','rolled_back'} else 'incomplete'
            self.m.update(active=False,generation=uuid.uuid4().hex,stage=stage);self.save()
    def call(self,name,args):
        from .maintenance_mcp import validate_arguments
        validate_arguments(name,args)
        with run_guard(self.folder):return getattr(self,name)(**args)
    def blob(self,path):
        permitted(path)
        origin='draft' if path in self.m['draft'] else 'baseline'
        if origin=='baseline' and path not in self.m['inventory']['files']:
            original=checked(self.m['root'],path)
            if original.exists():
                if original.stat().st_size>MAX_FILE:raise ScopeError('File is oversized and was not snapshotted')
                raise ScopeError('File is binary, excluded, unreadable or outside snapshot coverage')
            raise ScopeError('File is missing from the immutable snapshot')
        p=checked(self.folder/origin,path)
        b=p.read_bytes()
        expected=self.m['draft'][path]['sha256'] if origin=='draft' else self.m['inventory']['files'][path]['sha256']
        if digest(b)!=expected:raise Conflict('Persisted file bytes differ from recorded version')
        return b,origin
    def version(self,path,b,origin,sha256=None):
        h=sha256 or digest(b);v='v'+digest((self.m['id']+path+h).encode())[:14]
        return {'path':path,'sha256':h,'version':v,'origin':origin,'revision':self.m['revision'],'evidence_id':path+'@'+h[:12]}
    def overlay(self):return sorted(set(self.m['inventory']['files'])|set(self.m['draft']))
    def overlay_version(self):
        return digest(json.dumps({'inventory':self.m['inventory']['version'],'draft':{path:meta['sha256'] for path,meta in self.m['draft'].items()}},sort_keys=True).encode())
    def cursor(self,cursor,key):
        if not cursor:return 0
        v=decoded(cursor)
        if not isinstance(v,list) or len(v)!=4 or type(v[1])!=int or v[:3]!=[self.m['id'],self.m['revision'],key] or type(v[3])!=int or v[3]<0:raise Conflict('Stale or mismatched continuation token')
        return v[3]
    def next(self,key,offset):return encoded([self.m['id'],self.m['revision'],key,offset])
    def result_budget(self):return min(16000,self.m.get('tool_result_budget_chars',16000))
    def result_size(self,value):return len(json.dumps(value,ensure_ascii=False))
    def fit_text(self,make_result,maximum):
        full=make_result(maximum)
        if self.result_size(full)<=self.result_budget():return full
        low=0;high=maximum
        while low<high:
            middle=(low+high+1)//2
            if self.result_size(make_result(middle))<=self.result_budget():low=middle
            else:high=middle-1
        result=make_result(low)
        if self.result_size(result)>self.result_budget():return {'error':'Result metadata exceeds the current context allowance; retry with narrower scope','outcome':'context_limited','coverage_complete':False}
        return result
    def list_files(self,cursor=None,page_size=50):
        self.active();self.m['inventory_dependency']=True;paths=self.overlay();offset=self.cursor(cursor,'list')
        revision=self.m['revision'];overlay_version=self.overlay_version()
        result=[]
        def receipt(items):
            end=offset+len(items)
            return {'files':items,'next_cursor':self.next('list',end) if end<len(paths) else None,'complete':end>=len(paths) and self.m['inventory']['complete'],
                    'scan_complete':end>=len(paths),'coverage_complete':end>=len(paths) and self.m['inventory']['complete'],
                    'inventory_version':self.m['inventory']['version'],'revision':revision,'overlay_version':overlay_version,'exclusions':self.m['inventory']['exclusions'],'eligible_files':len(paths),'requested_page_size':page_size,'effective_page_size':len(items)}
        for p in paths[offset:offset+page_size]:
            b,origin=self.blob(p);item=self.version(p,b,origin)|{'bytes':len(b)}
            if self.result_size(receipt(result+[item]))>self.result_budget():break
            result.append(item)
        self.save();return receipt(result)
    def read_file(self,path,start_line=1,count=160,continuation=None):
        self.active();permitted(path);offset=0
        if continuation:
            value=decoded(continuation)
            if not isinstance(value,list) or len(value)!=4 or type(value[1])!=int or value[:2]!=[self.m['id'],self.m['revision']] or not isinstance(value[2],str) or type(value[3])!=int or value[3]<0:raise Conflict('Stale or invalid read continuation')
            bound=re.fullmatch(re.escape('read:'+path+':')+r'([1-9][0-9]*):([1-9][0-9]*)',value[2])
            if not bound:raise Conflict('Read continuation belongs to another path or range')
            start_line,count=map(int,bound.groups());offset=value[3]
            if start_line>1000000 or not 1<=count<=200:raise Conflict('Read continuation range is invalid')
        b,origin=self.blob(path);lines=b.decode('utf-8').splitlines(keepends=True);key='read:'+path+':'+str(start_line)+':'+str(count)
        selected=''.join(lines[start_line-1:start_line-1+count])
        absolute_start=sum(len(line) for line in lines[:start_line-1]);total_chars=sum(map(len,lines))
        if offset>len(selected):raise Conflict('Read continuation offset exceeds its original range')
        if path in self.m['inventory']['files']:self.m['reads'][path]=self.m['inventory']['files'][path]['sha256']
        self.save();version=self.version(path,b,origin)
        def receipt(length):
            text=selected[offset:offset+length];end=offset+len(text);more=end<len(selected)
            next_token=self.next(key,end) if more else None
            next_start=None if more or start_line+count>len(lines) else start_line+count
            next_call={'path':path,'continuation':next_token} if next_token else {'path':path,'start_line':next_start,'count':count} if next_start else None
            return version|{'text':text,'start_line':start_line+selected[:offset].count('\n'),'end_line':start_line+selected[:end].count('\n')-(1 if text.endswith('\n') else 0),
                    'absolute_start_char':absolute_start+offset,'absolute_end_char':absolute_start+end,'total_chars':total_chars,
                    'total_lines':len(lines),'offset_chars':offset,'next_continuation':next_token,'next_call':next_call,'original_range':{'start_line':start_line,'count':count},
                    'next_start_line':next_start,'range_complete':not more,'file_complete':not more and start_line==1 and count>=len(lines),
                    'requested_line_count':count,'effective_text_chars':len(text),'context_page_limited':more}
        return self.fit_text(receipt,min(12000,max(0,len(selected)-offset)))
    def search_text(self,query,cursor=None,page_size=20,prefix=''):
        self.active()
        if prefix:permitted(prefix.rstrip('/')+'/placeholder.txt')
        self.m['inventory_dependency']=True;matches=[];paths=[p for p in self.overlay() if p.startswith(prefix)]
        revision=self.m['revision'];overlay_version=self.overlay_version()
        key='search:'+query+':'+prefix+':'+self.m['inventory']['version']
        position=[0,0,0,0]  # file index, line index, original-character offset, emitted matches
        if cursor:
            value=decoded(cursor)
            if not isinstance(value,list) or len(value)!=4 or value[:3]!=[self.m['id'],self.m['revision'],key]:raise Conflict('Stale or mismatched search continuation')
            position=value[3]
            if not isinstance(position,list) or len(position)!=4 or any(type(x)!=int or x<0 for x in position):raise Conflict('Invalid search position')
        file_index,line_index,offset,emitted=position
        if file_index>len(paths):raise Conflict('Invalid search file position')
        scanned_bytes=0;loaded_files=0;needle=re.compile(re.escape(query),re.IGNORECASE);result_full=False
        def receipt(items,fi,li,off,total):
            exhausted=fi>=len(paths)
            next_cursor=encoded([self.m['id'],self.m['revision'],key,[fi,li,off,total]]) if not exhausted else None
            return {'matches':items,'next_cursor':next_cursor,'coverage_complete':exhausted and self.m['inventory']['complete'],
                    'scan_complete':exhausted,'scanned_files':fi,'eligible_files':len(paths),'matches_emitted':total,
                    'total_matches':total if exhausted else None,'total_matches_exact':exhausted,'total_matches_scope':'eligible_snapshot_files',
                    'scanned_bytes_this_page':scanned_bytes,'loaded_files_this_page':loaded_files,'requested_page_size':page_size,'effective_page_size':len(items),
                    'exclusions':self.m['inventory']['exclusions'],'inventory_version':self.m['inventory']['version'],'revision':revision,'overlay_version':overlay_version}
        # Scan work as well as result construction is bounded. Pages can be empty
        # with a continuation when a large no-match search needs further scanning.
        while file_index<len(paths) and len(matches)<page_size and loaded_files<64 and not result_full:
            path=paths[file_index];meta=self.m['draft'].get(path) or self.m['inventory']['files'][path]
            if loaded_files and scanned_bytes+meta['bytes']>1024*1024:break
            b,origin=self.blob(path);scanned_bytes+=len(b);loaded_files+=1
            version=self.version(path,b,origin,meta['sha256'])
            full_lines=b.decode('utf-8').splitlines(keepends=True);lines=[line.rstrip('\r\n') for line in full_lines];line_offsets=[];absolute=0
            for full_line in full_lines:line_offsets.append(absolute);absolute+=len(full_line)
            if line_index>len(lines) or (line_index<len(lines) and offset>len(lines[line_index])):raise Conflict('Invalid search line position')
            while line_index<len(lines) and len(matches)<page_size:
                line=lines[line_index];found=needle.search(line,offset)
                if found is None:line_index+=1;offset=0;continue
                pos=found.start();excerpt_start=max(0,pos-100);excerpt=line[excerpt_start:pos+500]
                absolute=line_offsets[line_index]+excerpt_start
                item=version|{'line':line_index+1,'offset':pos,'text':excerpt,'absolute_start_char':absolute,'absolute_end_char':absolute+len(excerpt),'line_truncated':len(line)>600}
                if self.result_size(receipt(matches+[item],file_index,line_index,found.end(),emitted+1))>self.result_budget():result_full=True;break
                matches.append(item)
                emitted+=1;offset=found.end()
                if offset>=len(line):line_index+=1;offset=0
            if line_index>=len(lines):file_index+=1;line_index=0;offset=0
        self.save();result=receipt(matches,file_index,line_index,offset,emitted)
        if result_full and not matches:result.update(error='One search match exceeds current context allowance; retry narrower query/path',outcome='context_limited')
        return result
    def write(self,path,content,expected_hash=None,create=False):
        self.writable();permitted(path,True);checked(self.m['root'],path,True)
        b=content.encode('utf-8')
        if len(b)>MAX_FILE or '\x00' in content:raise ScopeError('Draft file exceeds UTF-8 text limits')
        exists=path in self.overlay()
        if create and exists:raise Conflict('create_file requires a missing file')
        if not create:
            previous,origin=self.blob(path);v=self.version(path,previous,origin)
            if expected_hash not in {v['sha256'],v['version']}:raise Conflict('Expected file hash/version is stale')
            if previous==b:return v|{'outcome':'noop'}
        if self.m['edits']>=24:raise Conflict('Edit budget exhausted')
        size=sum((self.m['draft'].get(p) or self.m['inventory']['files'].get(p))['bytes'] for p in self.overlay() if p!=path)+len(b)
        if size>MAX_TOTAL:raise ScopeError('Total draft byte budget exceeded')
        candidate=self.version(path,b,'draft')|{'revision':self.m['revision']+1,'bytes':len(b),'outcome':'created' if create else 'edited'}
        if self.result_size(candidate)>self.result_budget():raise ContextLimit('Write acknowledgement exceeds context allowance; draft bytes and revision are unchanged')
        self.m['revision']+=1;revision=self.m['revision'];self.m['edits']+=1
        target=checked(self.folder/'draft',path,True);target.parent.mkdir(parents=True,exist_ok=True);durable(target,b)
        rev=self.folder/'revisions'/str(revision)/path;durable(rev,b)
        self.m['draft'][path]={'sha256':digest(b),'bytes':len(b),'revision':revision};self.m.update(review=None,checks={},stage='editing');self.save()
        return candidate
    def create_file(self,path,content,expected_missing=True):
        if expected_missing is not True:raise ValueError('expected_missing must be true')
        return self.write(path,content,create=True)
    def apply_edit(self,path,expected_hash,old_text,new_text):
        self.active();b,_=self.blob(path);text=b.decode('utf-8')
        if not old_text or text.count(old_text)!=1:raise Conflict('Edit old_text must match exactly once')
        return self.write(path,text.replace(old_text,new_text,1),expected_hash)
    def diff(self):
        entries=[];full=[]
        for path in sorted(self.m['draft']):
            before=(self.folder/'baseline'/path).read_bytes() if path in self.m['inventory']['files'] else None
            after,_=self.blob(path)
            if before==after:continue
            diff=''.join(difflib.unified_diff((before or b'').decode('utf-8').splitlines(keepends=True),after.decode('utf-8').splitlines(keepends=True),fromfile='a/'+path if before is not None else '/dev/null',tofile='b/'+path))
            entries.append({'path':path,'before_hash':digest(before) if before is not None else MISSING,'after_hash':digest(after),'new_file':before is None,'diff':diff})
            full.append(diff)
        text='\n'.join(full);return entries,text,digest(json.dumps(entries,sort_keys=True).encode())
    def get_diff(self,path=None,cursor=None):
        self.active()
        if cursor:
            value=decoded(cursor)
            if not isinstance(value,list) or len(value)!=4 or type(value[1])!=int or value[:2]!=[self.m['id'],self.m['revision']] or not isinstance(value[2],str) or type(value[3])!=int or value[3]<0:raise Conflict('Stale or invalid diff continuation')
            bound=re.fullmatch(r'diff:(.*):([0-9a-f]{64})',value[2])
            if not bound:raise Conflict('Invalid diff continuation binding')
            bound_path=None if bound[1]=='None' else bound[1]
            if bound_path is not None:permitted(bound_path)
            if path is not None and path!=bound_path:raise Conflict('Diff continuation belongs to another path')
            path=bound_path
        entries,text,h=self.diff()
        if path is not None:permitted(path);entries=[x for x in entries if x['path']==path];text='\n'.join(x['diff'] for x in entries)
        key='diff:'+str(path)+':'+h;offset=self.cursor(cursor,key)
        if offset>len(text):raise Conflict('Diff continuation offset exceeds its bound contents')
        def receipt(length):
            part=text[offset:offset+length];end=offset+len(part)
            token=self.next(key,end) if end<len(text) else None
            return {'revision':self.m['revision'],'digest':h,'path':path,'files':[{k:v for k,v in e.items() if k!='diff'} for e in entries], 'text':part,'next_cursor':token,'next_call':{'cursor':token} if token else None,'complete':end>=len(text),'requested_text_chars':12000,'effective_text_chars':len(part),'context_page_limited':end<len(text)}
        return self.fit_text(receipt,min(12000,max(0,len(text)-offset)))
    def run_fixed_checks(self,suite='static'):
        self.active();failures=[]
        for path in self.overlay():
            b,_=self.blob(path)
            try:
                if path.endswith('.py'):ast.parse(b.decode('utf-8'),filename=path)
                elif path.endswith('.json'):json.loads(b)
            except (SyntaxError,ValueError):failures.append(path)
        static={'type':'syntax_static','status':'FAIL' if failures else 'PASS','failures':failures,'revision':self.m['revision'],'execution':'No project code executed'}
        previous=self.m.get('checks',{})
        trusted=previous.get('trusted_tests') if previous.get('revision')==self.m['revision'] else None
        result={'static':static,'trusted_tests':trusted or {'status':'NOT_ASSESSED','limitations':['No owner-trusted behavioral suite for this project.']},'quality':'NOT_ASSESSED','goal_review':'NOT_ASSESSED','revision':self.m['revision']}
        if suite=='ticketdesk':
            from .maintenance_checks import ticketdesk_checks
            result['trusted_tests']=ticketdesk_checks(self)
        elif suite!='static':raise ValueError('Unknown fixed suite')
        self.m['checks']=result;self.save()
        if self.result_size(result)<=self.result_budget():return result
        # Full observed receipts stay authoritative in the manifest; a bounded
        # tool receipt explicitly identifies omitted failure detail.
        summary={k:result[k] for k in ('quality','goal_review','revision')}
        for name in ('static','trusted_tests'):
            item=result[name];summary[name]={k:item[k] for k in ('status','type','revision','checker_sha256','client_sha256','execution') if k in item}
            summary[name]['failure_count']=len(item.get('failures',[]));summary[name]['details_omitted']=True
        summary['full_receipt_sha256']=digest(json.dumps(result,ensure_ascii=False,sort_keys=True).encode());summary['full_receipt']='Persisted run checks at this revision'
        return summary
    def public(self):
        self.reload();return {k:v for k,v in self.m.items() if k not in {'root','generation'}}
    def review(self,revision,diff_digest):
        self.writable();entries,_,h=self.diff()
        if self.m['stage']=='incomplete':raise Conflict('Incomplete workflow requires a new successful run or explicit owner draft editing')
        if revision!=self.m['revision'] or h!=diff_digest:raise Conflict('Review revision/diff is stale')
        if self.m['checks'].get('static',{}).get('status')!='PASS' or self.m['checks'].get('revision')!=revision:raise Conflict('Run current static checks before review')
        self.m['review']={'revision':revision,'digest':h};self.m['stage']='draft_ready';self.save();return self.m['review']
    def current_hash(self,path,write=True):
        target=checked(self.m['root'],path,write)
        if not target.exists():return MISSING
        if target.stat().st_size>MAX_FILE:raise Conflict('Current target exceeds permitted size')
        return digest(target.read_bytes())
    def guard(self):
        if not self.m['inventory']['complete']:raise Conflict('Snapshot coverage is incomplete')
        for path in set(self.m['reads'])|set(self.m['draft']):
            expected=self.m['inventory']['files'].get(path,{}).get('sha256',MISSING)
            if self.current_hash(path,path in self.m['draft'])!=expected:raise Conflict('Source or target changed: '+path)
        if self.m['inventory_dependency'] and inventory(self.m['root'])['version']!=self.m['inventory']['version']:raise Conflict('Project inventory/search dependency changed')
    def transact(self,revision,diff_digest,fault=None):
        with WorkspaceLease(self.m['root']):
            self.writable();workspace_guard(self.m['root'])
            self.token=self.m['generation']
            if self.m['review']!={'revision':revision,'digest':diff_digest}:raise Conflict('Review the exact current draft before Apply')
            self.guard();entries,_,h=self.diff()
            if h!=diff_digest:raise Conflict('Draft digest changed')
            records=[]
            for e in entries:
                p=e['path'];before=(self.folder/'baseline'/p).read_bytes() if e['before_hash']!=MISSING else None;after,_=self.blob(p)
                records.append({'path':p,'before_hash':e['before_hash'],'after_hash':e['after_hash'],'before':base64.b64encode(before).decode() if before is not None else None,'after':base64.b64encode(after).decode(),'state':'pending'})
            journal={'id':uuid.uuid4().hex,'intent':'apply','status':'prepared','revision':revision,'files':records}
            if not records:
                # No-op is a run receipt, never a newer applied workspace batch.
                with run_guard(self.folder,wait=True):
                    self.reload();journal['status']='noop';self.m.update(journal=journal,stage='noop',application='NOOP');self.save()
                return self.public()
            self.m['journal']=journal;self.m['stage']='recovery_required';self.save()
            durable(workspace_state(self.m['root']),json.dumps({'manifest':str(self.path),'batch_id':journal['id']}).encode())
            self._install(journal,fault)
            return self.public()
    def _install(self,journal,fault=None):
        try:
            if fault:fault('prepared',None)
            for index,item in enumerate(journal['files']):
                with run_guard(self.folder,wait=True):
                    self.reload()
                    if self.token and (self.m['generation']!=self.token or not self.m['active']):raise Conflict('Stopped before next filesystem write')
                    if self.current_hash(item['path'])!=item['before_hash']:raise Conflict('Target changed during transaction: '+item['path'])
                    target=checked(self.m['root'],item['path'],True);target.parent.mkdir(parents=True,exist_ok=True);checked(self.m['root'],item['path'],True)
                    journal['files'][index]['state']='intent';self.m['journal']=journal;self.save()
                    if fault:fault('intent',index)
                    if item['after'] is None:target.unlink()
                    else:durable(target,base64.b64decode(item['after']))
                    if fault:fault('replaced',index)
                    if self.current_hash(item['path'])!=item['after_hash']:raise Conflict('Target readback conflict')
                    journal['files'][index]['state']='written';self.m['journal']=journal;self.save()
                if fault:fault('receipt',index)
            for item in journal['files']:
                if self.current_hash(item['path'])!=item['after_hash']:raise Conflict('Final transaction readback conflict')
            outcome='applied' if journal['intent']=='apply' else 'undone'
            if not journal['files']:outcome='noop'
            with run_guard(self.folder,wait=True):
                self.reload()
                if self.token and (self.m['generation']!=self.token or not self.m['active']):raise Conflict('Stopped before transaction completion')
                journal['status']=outcome;self.m.update(journal=journal,stage=outcome,application=outcome.upper());self.save()
        except BaseException as original:
            if getattr(original,'staging_cleanup_failure',None):journal.setdefault('staging_residues',[]).append(original.staging_cleanup_failure)
            try:
                with run_guard(self.folder,wait=True):
                    self.reload();journal['status']='recovery_required';self.m.update(journal=journal,stage='recovery_required',application='PARTIAL_OR_UNCERTAIN');self.save()
            except BaseException as journal_error:original.add_note('Recovery journal update failed: '+str(journal_error))
            raise
    def undo(self,fault=None):
        with WorkspaceLease(self.m['root']):
            self.reload();j=self.m.get('journal')
            self.token=self.m['generation']
            if not j or j['status']!='applied' or j['intent']!='apply':raise Conflict('No applied batch available to Undo')
            marker=json.loads(workspace_state(self.m['root']).read_text(encoding='utf-8'))
            if marker['manifest']!=str(self.path) or marker['batch_id']!=j['id']:raise Conflict('Only the latest applied workspace batch may be undone')
            # A later apply from another run is caught by current bytes. Only latest
            # workspace batch can undo; the durable workspace marker is also checked.
            for item in j['files']:
                if self.current_hash(item['path'])!=item['after_hash']:raise Conflict('Later user edits prevent Undo: '+item['path'])
            records=[dict(path=x['path'],before_hash=x['after_hash'],after_hash=x['before_hash'],before=x['after'],after=x['before'],state='pending') for x in j['files']]
            journal={'id':uuid.uuid4().hex,'intent':'undo','status':'prepared','revision':self.m['revision'],'files':records}
            self.m.update(journal=journal,stage='recovery_required');self.save();self._install(journal,fault);return self.public()
    def recover(self):
        with WorkspaceLease(self.m['root']):
            self.reload();j=self.m.get('journal')
            if not j or j['status'] in {'applied','undone','rolled_back','noop'}:raise Conflict('No interrupted batch to recover')
            self.m.update(active=True,generation=uuid.uuid4().hex);self.token=self.m['generation'];self.save()
            states={x['path']:self.current_hash(x['path']) for x in j['files']}
            foreign=[x['path'] for x in j['files'] if states[x['path']] not in {x['before_hash'],x['after_hash']}]
            if foreign:
                j['status']='recovery_conflict';self.m['stage']='recovery_required';self.m['recovery_conflicts']=foreign;self.save();raise Conflict('Foreign edits preserved; recovery conflict: '+', '.join(foreign))
            for x in reversed(j['files']):
                with run_guard(self.folder,wait=True):
                    current=self.reload()
                    if not current['active'] or current['generation']!=self.token:raise Conflict('Stopped during guarded recovery')
                    if states[x['path']]==x['before_hash']:x['recovery']='already_before';continue
                    if self.current_hash(x['path'])!=x['after_hash']:raise Conflict('Target changed during recovery')
                    p=checked(self.m['root'],x['path'],True)
                    if x['before'] is None:p.unlink()
                    else:
                        try:durable(p,base64.b64decode(x['before']))
                        except BaseException as original:
                            if getattr(original,'staging_cleanup_failure',None):j.setdefault('staging_residues',[]).append(original.staging_cleanup_failure)
                            try:
                                j['status']='recovery_required';self.m.update(journal=j,stage='recovery_required',application='PARTIAL_OR_UNCERTAIN');self.save()
                            except BaseException as journal_error:original.add_note('Recovery journal update failed: '+str(journal_error))
                            raise
                    x['recovery']='rolled_back';self.m['journal']=j;self.save()
            with run_guard(self.folder,wait=True):
                self.reload()
                if not self.m['active'] or self.m['generation']!=self.token:raise Conflict('Stopped before recovery completion')
                residue=[x['path'] for x in j.get('staging_residues',[]) if os.path.lexists(x['path'])]
                if residue:
                    j['status']='recovery_required';self.m.update(journal=j,stage='recovery_required',application='PARTIAL_OR_UNCERTAIN');self.save()
                    raise Conflict('Known staging residue remains; rollback targets do not establish a clean workspace: '+', '.join(residue))
                j['status']='rolled_back';self.m.update(journal=j,stage='undone',application='ROLLED_BACK');self.save()
            return self.public()
