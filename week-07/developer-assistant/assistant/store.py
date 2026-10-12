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
            CREATE TABLE IF NOT EXISTS deliveries(task_id TEXT PRIMARY KEY,status TEXT,message_id INTEGER);
            ''')
            rows=c.execute("SELECT id,payload FROM tasks WHERE status IN ('running','queued')").fetchall() if recover else []
            for r in rows:
                payload=json.loads(r['payload']);payload.update(status='failed',partial='',answer='',error='Interrupted by application restart. Retry creates a new run.')
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
            c.execute('''INSERT INTO tasks VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                payload=excluded.payload,status=excluded.status,updated=excluded.updated''',
                (t['id'],t['project_id'],json.dumps(t,ensure_ascii=False),t['status'],t['created'],now()))

    def task(self,tid):
        with self.connect() as c:
            r=c.execute('SELECT payload FROM tasks WHERE id=?',(tid,)).fetchone()
        if not r:
            raise ValueError('Unknown task')
        return json.loads(r[0])

    def tasks(self,pid):
        with self.connect() as c:
            return [json.loads(r[0]) for r in c.execute('SELECT payload FROM tasks WHERE project_id=? ORDER BY rowid DESC LIMIT 50',(pid,))]
