"""SQLite state and versioned FTS5 corpus; no credentials are stored here."""
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import uuid
from .scope import ProjectScope, ScopeError

def now():
    return datetime.now(timezone.utc).isoformat()

def ident():
    return uuid.uuid4().hex

def terms(query):
    stop = {'help','the','a','an','and','what','is','does','how','this','project','our','about','в','и','как','что','это','у','по','про','наш','нашего','проект','проекта','ли','который'}
    parts = [t.lower() for t in re.findall(r'[\w]+',query) if len(t)>1 and t.lower() not in stop]
    aliases = {'структур':'structure components architecture','ветк':'branch git','индекс':'index indexing rag','источник':'source citation retrieval',
               'хран':'storage sqlite persistence','останов':'stop cancel cancellation','контекст':'context retrieval',
               'инструмент':'tools executor scope','эндпоинт':'endpoint api','параметр':'settings parameters','модел':'provider local deepseek',
               'запуск':'setup run launch','документ':'docs documentation','очеред':'queue task worker'}
    for p in list(parts):
        for stem, value in aliases.items():
            if stem in p:
                parts.extend(value.split())
    return list(dict.fromkeys(parts))[:24]

class Store:
    def __init__(self,path,recover=True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT,root TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,project_id TEXT,payload TEXT,status TEXT,created TEXT,updated TEXT);
            CREATE TABLE IF NOT EXISTS documents(project_id TEXT,path TEXT,hash TEXT,PRIMARY KEY(project_id,path));
            CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY,project_id TEXT,path TEXT,hash TEXT,start INTEGER,end INTEGER,heading TEXT,text TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS corpus USING fts5(id UNINDEXED,project_id UNINDEXED,text,tokenize='unicode61');
            CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY,project_id TEXT,title TEXT,created TEXT,updated TEXT,archived INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS task_chats(task_id TEXT PRIMARY KEY,chat_id TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS chat_runs ON task_chats(chat_id);
            CREATE TABLE IF NOT EXISTS deliveries(task_id TEXT PRIMARY KEY,status TEXT,message_id INTEGER);
            ''')
            rows=c.execute("SELECT id,payload FROM tasks WHERE status IN ('running','queued')").fetchall() if recover else []
            for r in rows:
                payload=json.loads(r['payload']);payload.update(status='failed',partial='',answer='',error='Interrupted by application restart. Retry creates a new run.')
                if payload.get('mode')=='files' and 'files_context_budget' in payload:
                    from .maintenance import validate_files_budget
                    validate_files_budget(payload['files_context_budget'])
                if payload.get('mode')=='files' and re.fullmatch(r'[0-9a-f]{32}',r['id']):
                    from .maintenance import Run
                    folder=self.path.parent/'maintenance'/r['id']
                    if (folder/'manifest.json').exists():
                        run=Run(folder)
                        if ('files_context_budget' in payload)!=('files_context_budget' in run.m) or payload.get('files_context_budget')!=run.m.get('files_context_budget'):raise ValueError('Project-files restart context budget mismatch')
                        run.cancel();run.reload()
                        if run.m['stage']!='recovery_required':run.m['stage']='interrupted';run.save()
                        payload['maintenance']=run.public()
                for step in payload.get('plan',[]):
                    if step.get('status') in {'running','pending'}:
                        step['status']='failed'
                c.execute('UPDATE tasks SET status=?,payload=?,updated=? WHERE id=?',('failed',json.dumps(payload),now(),r['id']))
            if recover:
                for r in c.execute("SELECT id,payload FROM tasks WHERE status='completed'").fetchall():
                    payload=json.loads(r['payload'])
                    if payload.get('mode')!='web': continue
                    try: result=json.loads(payload.get('answer',''))
                    except (ValueError,TypeError): continue
                    if isinstance(result,dict) and (result.get('error') or isinstance(result.get('status'),int) and result['status']>=400):
                        payload.update(status='failed',answer='',error='Historical search response was unsuccessful; completion corrected. Original evidence retained in events.')
                        for step in payload.get('plan',[]): step['status']='failed'
                        c.execute('UPDATE tasks SET status=?,payload=?,updated=? WHERE id=?',('failed',json.dumps(payload),now(),r['id']))

        if recover:
            with self.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                for project in c.execute('SELECT id FROM projects').fetchall():
                    rows=c.execute('SELECT t.id FROM tasks t LEFT JOIN task_chats tc ON tc.task_id=t.id WHERE t.project_id=? AND tc.task_id IS NULL',(project['id'],)).fetchall()
                    if not rows: continue
                    chat=c.execute('SELECT id FROM chats WHERE project_id=? AND archived=0 ORDER BY created,id LIMIT 1',(project['id'],)).fetchone()
                    cid=chat['id'] if chat else ident()
                    if not chat:
                        c.execute('INSERT INTO chats VALUES (?,?,?,?,?,0)',(cid,project['id'],'Previous conversations',now(),now()))
                    c.executemany('INSERT INTO task_chats VALUES (?,?)',[(r['id'],cid) for r in rows])

    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=10)
        c.row_factory=sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    def project(self,root,name=None):
        scope=ProjectScope(root)
        with self.connect() as c:
            r=c.execute('SELECT * FROM projects WHERE root=?',(str(scope.root),)).fetchone()
            if r:
                return dict(r)
            p={'id':ident(),'name':name or scope.root.name,'root':str(scope.root)}
            c.execute('INSERT INTO projects VALUES (:id,:name,:root)',p)
            return p

    def projects(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM projects ORDER BY rowid')]

    def get_project(self,pid):
        with self.connect() as c:
            r=c.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone()
        if not r:
            raise ValueError('Unknown project')
        return dict(r)

    def index(self,pid):
        scope=ProjectScope(self.get_project(pid)['root'])
        documents=[];chunks=[]
        for path in scope.docs():
            try:
                p=scope.checked(path)
                if p.stat().st_size>512000:
                    continue
                text=p.read_text(encoding='utf-8-sig')
                if '\x00' in text:
                    continue
            except (OSError,UnicodeError,ScopeError):
                continue
            digest=hashlib.sha256(text.encode()).hexdigest()
            documents.append((pid,path,digest))
            lines=text.splitlines();start=1;buf=[];heading=path;part_index=0
            def flush(end):
                nonlocal part_index
                if not buf:
                    return
                content='\n'.join(buf)
                cid=hashlib.sha256(f'{pid}:{path}:{digest}:{start}:{part_index}'.encode()).hexdigest()[:16]
                part_index+=1
                chunks.append((cid,pid,path,digest,start,end,heading,content))
            for n,line in enumerate(lines,1):
                if len(line)>1100:
                    flush(n-1);buf=[];start=n
                    for offset in range(0,len(line),1000):
                        buf=[line[offset:offset+1000]];flush(n)
                    buf=[];start=n+1
                    continue
                if (line.startswith('#') and buf) or len('\n'.join(buf))+len(line)>1100:
                    flush(n-1);buf=[];start=n
                if line.startswith('#'):
                    heading=line.lstrip('# ').strip()
                buf.append(line)
            flush(len(lines))
        with self.connect() as c:
            c.execute('DELETE FROM corpus WHERE project_id=?',(pid,))
            c.execute('DELETE FROM chunks WHERE project_id=?',(pid,))
            c.execute('DELETE FROM documents WHERE project_id=?',(pid,))
            c.executemany('INSERT INTO documents VALUES (?,?,?)',documents)
            c.executemany('INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)',chunks)
            c.executemany('INSERT INTO corpus VALUES (?,?,?)',[(x[0],pid,f'{x[2]} {x[6]}\n{x[7]}') for x in chunks])
        return self.manifest(pid)

    def manifest(self,pid):
        with self.connect() as c:
            docs=[dict(r) for r in c.execute('SELECT path,hash FROM documents WHERE project_id=? ORDER BY path',(pid,))]
            count=c.execute('SELECT COUNT(*) FROM chunks WHERE project_id=?',(pid,)).fetchone()[0]
        return {'documents':docs,'document_count':len(docs),'chunk_count':count,'retrieval':'SQLite FTS5 / BM25'}

    def retrieve(self,pid,query,limit=8):
        ts=terms(query)
        if not ts:
            return []
        expr=' OR '.join('"'+t.replace('"','')+'"' for t in ts)
        with self.connect() as c:
            rows=c.execute('''SELECT ch.*,bm25(corpus) AS rank FROM corpus JOIN chunks ch ON ch.id=corpus.id
                    WHERE corpus MATCH ? AND corpus.project_id=? ORDER BY rank LIMIT ?''',(expr,pid,limit)).fetchall()
        result=[]
        for r in rows:
            d=dict(r);d.pop('project_id',None);d.pop('rank',None)
            d['label']=f'S{len(result)+1}'
            result.append(d)
        return result

    def save_task(self,t):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT payload FROM tasks WHERE id=?',(t['id'],)).fetchone()
            if t.get('mode')=='files':
                from .maintenance import validate_files_budget
                if 'files_context_budget' in t:validate_files_budget(t['files_context_budget'])
                if old:
                    previous=json.loads(old['payload'])
                    if ('files_context_budget' in previous)!=('files_context_budget' in t) or previous.get('files_context_budget')!=t.get('files_context_budget'):raise ValueError('Immutable task files context budget changed')
            cid=t.get('chat_id')
            if not old and cid:
                self.check_chat(c,cid,t['project_id'])
            c.execute('''INSERT INTO tasks VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                payload=excluded.payload,status=excluded.status,updated=excluded.updated''',
                (t['id'],t['project_id'],json.dumps(t,ensure_ascii=False),t['status'],t['created'],now()))
            if not old and cid:
                c.execute('INSERT INTO task_chats VALUES (?,?)',(t['id'],cid))
                c.execute('UPDATE chats SET updated=? WHERE id=?',(now(),cid))
                chat=c.execute('SELECT title FROM chats WHERE id=?',(cid,)).fetchone()
                if chat['title']=='New chat':
                    c.execute('UPDATE chats SET title=? WHERE id=?',(t['question'][:64],cid))

    def hydrate_files(self,data):
        if data.get('mode')=='files' and 'files_context_budget' in data:
            from .maintenance import validate_files_budget
            validate_files_budget(data['files_context_budget'])
        if data.get('mode')=='files' and re.fullmatch(r'[0-9a-f]{32}',data.get('id','')):
            from .maintenance import Run
            folder=self.path.parent/'maintenance'/data['id']
            if (folder/'manifest.json').exists():
                run=Run(folder)
                if (run.m['id'],run.m['project_id'],run.m['chat_id'])!=(data['id'],data['project_id'],data.get('chat_id')):raise ValueError('Project-files history binding mismatch')
                if ('files_context_budget' in data)!=('files_context_budget' in run.m) or data.get('files_context_budget')!=run.m.get('files_context_budget'):raise ValueError('Project-files history context budget mismatch')
                data['maintenance']=run.public()
        return data

    def task(self,tid):
        with self.connect() as c:
            r=c.execute('SELECT t.payload,tc.chat_id FROM tasks t LEFT JOIN task_chats tc ON tc.task_id=t.id WHERE t.id=?',(tid,)).fetchone()
        if not r:
            raise ValueError('Unknown task')
        data=json.loads(r[0])
        if r['chat_id']: data['chat_id']=r['chat_id']
        return self.hydrate_files(data)

    def tasks(self,pid):
        with self.connect() as c:
            rows=c.execute('SELECT t.payload,tc.chat_id FROM tasks t LEFT JOIN task_chats tc ON tc.task_id=t.id WHERE t.project_id=? ORDER BY t.rowid DESC LIMIT 50',(pid,)).fetchall()
        result=[]
        for r in rows:
            data=json.loads(r['payload'])
            if r['chat_id']: data['chat_id']=r['chat_id']
            result.append(self.hydrate_files(data))
        return result

    def check_chat(self,c,cid,pid,active=True):
        r=c.execute('SELECT * FROM chats WHERE id=? AND project_id=?',(cid,pid)).fetchone()
        if not r: raise ValueError('Chat does not belong to the selected project')
        if active and r['archived']: raise ValueError('Restore the deleted chat before starting work')
        return dict(r)

    def chat(self,cid):
        with self.connect() as c: r=c.execute('SELECT * FROM chats WHERE id=?',(cid,)).fetchone()
        if not r: raise ValueError('Unknown chat')
        return dict(r)

    def chats(self,pid,include_archived=False):
        self.get_project(pid)
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM chats WHERE project_id=?'+('' if include_archived else ' AND archived=0')+' ORDER BY updated DESC,id',(pid,))]

    def create_chat(self,pid,title='New chat'):
        self.get_project(pid);title=title.strip()
        if not title or len(title)>80: raise ValueError('Chat name must contain 1 to 80 characters')
        chat=dict(id=ident(),project_id=pid,title=title,created=now(),updated=now(),archived=0)
        with self.connect() as c: c.execute('INSERT INTO chats VALUES (:id,:project_id,:title,:created,:updated,:archived)',chat)
        return chat

    def resolve_chat(self,pid,cid=None):
        self.get_project(pid)
        with self.connect() as c:
            if cid: return self.check_chat(c,cid,pid)['id']
            r=c.execute('SELECT id FROM chats WHERE project_id=? AND archived=0 ORDER BY created,id LIMIT 1',(pid,)).fetchone()
        return r['id'] if r else self.create_chat(pid)['id']

    def rename_chat(self,cid,title):
        title=title.strip()
        if not title or len(title)>80: raise ValueError('Chat name must contain 1 to 80 characters')
        with self.connect() as c:
            r=c.execute('SELECT id FROM chats WHERE id=?',(cid,)).fetchone()
            if not r: raise ValueError('Unknown chat')
            c.execute('UPDATE chats SET title=?,updated=? WHERE id=?',(title,now(),cid))
        return self.chat(cid)

    def archive_chat(self,cid,archived=True):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute('SELECT * FROM chats WHERE id=?',(cid,)).fetchone()
            if not r: raise ValueError('Unknown chat')
            if archived and not r['archived']:
                busy=c.execute("SELECT 1 FROM tasks t JOIN task_chats tc ON tc.task_id=t.id WHERE tc.chat_id=? AND t.status IN ('queued','running') LIMIT 1",(cid,)).fetchone()
                if busy: raise ValueError('Stop active or queued tasks before deleting this chat')
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schedules'").fetchone():
                    for row in c.execute('SELECT payload FROM schedules WHERE project_id=?',(r['project_id'],)):
                        schedule=json.loads(row['payload'])
                        if schedule.get('chat_id')==cid and (schedule['enabled'] or schedule.get('inflight')):
                            raise ValueError('Pause attached schedules and wait for running tasks before deleting this chat')
            c.execute('UPDATE chats SET archived=?,updated=? WHERE id=?',(int(archived),now(),cid))
        return self.chat(cid)

    def chat_tasks(self,cid,before=None,limit=50):
        self.chat(cid)
        with self.connect() as c:
            rows=c.execute('SELECT t.rowid AS position,t.payload FROM tasks t JOIN task_chats tc ON tc.task_id=t.id WHERE tc.chat_id=?'+(' AND t.rowid<?' if before else '')+' ORDER BY t.rowid DESC LIMIT ?',([cid,before,limit+1] if before else [cid,limit+1])).fetchall()
        more=len(rows)>limit;rows=rows[:limit];result=[]
        for r in reversed(rows):
            t=json.loads(r['payload']);t['chat_id']=cid;t['run_order']=r['position'];result.append(self.hydrate_files(t))
        return {'tasks':result,'next_before':rows[-1]['position'] if more else None}



