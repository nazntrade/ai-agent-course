"""Isolated support state, owner/version guards and sealed per-run JSON bridge."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import re
import sqlite3
from .store import ident, now

class Conflict(ValueError):
    pass

def safe_text(text, scrub=lambda x:x):
    text=scrub(str(text))
    text=re.sub(r'(?i)(bearer\s+)[\w.\-]{8,}',r'\1[redacted]',text)
    text=re.sub(r'(?i)((?:api[_ -]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+',r'\1[redacted]',text)
    text=re.sub(r'\bsk-[A-Za-z0-9_\-]{12,}\b','[redacted]',text)
    text=re.sub(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', '[redacted contact]', text)
    text=re.sub(r'(?<!\w)(?:\+?\d[ \-()]*){10,15}(?!\w)', '[redacted contact]', text)
    return text

DIAGNOSTIC_FIELDS={'component','status_code','error_code','connection_state','observed_at','attempted_steps'}
def diagnostics(value, scrub=lambda x:x):
    if not isinstance(value,dict) or set(value)-DIAGNOSTIC_FIELDS:
        raise ValueError('Unsupported diagnostic fields')
    result={}
    for key,v in value.items():
        if key=='status_code':
            if v is not None and (type(v) is not int or not 100<=v<=599):
                raise ValueError('Invalid status code')
            result[key]=v
        elif key=='attempted_steps':
            if not isinstance(v,list) or len(v)>8 or any(not isinstance(x,str) or len(x)>300 for x in v):
                raise ValueError('Invalid attempted steps')
            result[key]=[safe_text(x,scrub) for x in v]
        else:
            if not isinstance(v,str) or len(v)>160:
                raise ValueError('Invalid diagnostic field')
            result[key]=safe_text(v,scrub)
    return result

class SupportStore:
    def __init__(self,data,seed=None,scrub=lambda x:x,recover=True):
        self.data=Path(data);self.data.mkdir(parents=True,exist_ok=True)
        self.path=self.data/'support.sqlite3';self.scrub=scrub
        with self.connect() as c:
            c.executescript("""PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,owner TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY,owner TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,ticket TEXT NOT NULL,role TEXT NOT NULL,text TEXT NOT NULL,run TEXT,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,ticket TEXT NOT NULL,owner TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_ticket ON runs(ticket) WHERE status IN ('queued','running');
            CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,ticket TEXT NOT NULL,owner TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL);
            """)
            if seed and not c.execute('SELECT 1 FROM users').fetchone():
                source=json.loads(Path(seed).read_text(encoding='utf-8'))
                for user in source['users']:
                    c.execute('INSERT INTO users VALUES (?,?)',(user['id'],json.dumps(user)))
                for t in source['tickets']:
                    t=dict(t,context_version=1,state='open',created=now(),updated=now())
                    t['diagnostics']=diagnostics(t.get('diagnostics',{}),scrub)
                    c.execute('INSERT INTO tickets VALUES (?,?,?)',(t['id'],t['owner'],json.dumps(t)))
                    c.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',(ident(),t['id'],'user',safe_text(t['description'],scrub),None,now()))
            if recover:
                for row in c.execute("SELECT * FROM runs WHERE status IN ('queued','running')").fetchall():
                    r=json.loads(row['payload']);r.update(status='failed',partial='',answer='',error='Interrupted by support restart; retry is explicit.')
                    c.execute('UPDATE runs SET status=?,payload=? WHERE id=?',('failed',json.dumps(r),r['id']))
                for row in c.execute("SELECT * FROM deliveries WHERE status='sending'").fetchall():
                    r=json.loads(row['payload']);r['status']='uncertain';r['error']='Interrupted delivery; do not automatically resend.'
                    c.execute('UPDATE deliveries SET status=?,payload=? WHERE id=?',('uncertain',json.dumps(r),r['id']))

    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=10);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()

    def users(self):
        with self.connect() as c:return [json.loads(x['payload']) for x in c.execute('SELECT * FROM users ORDER BY id')]

    def session(self,user):
        with self.connect() as c:
            if not c.execute('SELECT 1 FROM users WHERE id=?',(user,)).fetchone():raise ValueError('Unknown synthetic persona')
            sid=ident();c.execute('INSERT INTO sessions VALUES (?,?,?)',(sid,user,now()))
        return sid

    def owner(self,sid):
        with self.connect() as c:r=c.execute('SELECT owner FROM sessions WHERE id=?',(sid,)).fetchone()
        if not r:raise ValueError('Choose a synthetic demo persona first')
        return r['owner']

    def _ticket(self,c,owner,tid):
        row=c.execute('SELECT payload FROM tickets WHERE id=? AND owner=?',(tid,owner)).fetchone()
        if not row:raise ValueError('Ticket unavailable in this demo session')
        return json.loads(row['payload'])

    def _save_ticket(self,c,t):
        c.execute('UPDATE tickets SET payload=? WHERE id=?',(json.dumps(t,ensure_ascii=False),t['id']))

    def tickets(self,owner):
        with self.connect() as c:return [json.loads(x['payload']) for x in c.execute('SELECT payload FROM tickets WHERE owner=? ORDER BY rowid DESC',(owner,))]

    def ticket(self,owner,tid):
        with self.connect() as c:
            t=self._ticket(c,owner,tid)
            t['messages']=[dict(x) for x in c.execute('SELECT * FROM messages WHERE ticket=? ORDER BY rowid',(tid,))]
            t['runs']=[json.loads(x['payload']) for x in c.execute('SELECT payload FROM runs WHERE ticket=? AND owner=? ORDER BY rowid DESC',(tid,owner))]
            t['deliveries']=[json.loads(x['payload']) for x in c.execute('SELECT payload FROM deliveries WHERE ticket=? AND owner=? ORDER BY rowid DESC',(tid,owner))]
        return t

    def create(self,owner,title,description,component='unknown',facts=None):
        title=safe_text(title,self.scrub).strip();description=safe_text(description,self.scrub).strip()
        if not 1<=len(title)<=120 or not 1<=len(description)<=4000:raise ValueError('Title and description are required and bounded')
        t={'id':ident(),'owner':owner,'product':'Developer Assistant','component':component,'title':title,'description':description,
           'diagnostics':diagnostics(facts or {},self.scrub),'context_version':1,'state':'open','created':now(),'updated':now()}
        with self.connect() as c:
            if not c.execute('SELECT 1 FROM users WHERE id=?',(owner,)).fetchone():raise ValueError('Unknown persona')
            c.execute('INSERT INTO tickets VALUES (?,?,?)',(t['id'],owner,json.dumps(t,ensure_ascii=False)))
            c.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',(ident(),t['id'],'user',description,None,now()))
        return self.ticket(owner,t['id'])

    def update(self,owner,tid,version,facts):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');t=self._ticket(c,owner,tid)
            if t['context_version']!=version:raise Conflict('Ticket changed; reload before updating diagnostics')
            t['diagnostics']=diagnostics(facts,self.scrub);t['context_version']+=1;t['updated']=now();self._save_ticket(c,t)
        return self.ticket(owner,tid)

    def state(self,owner,tid,version,state):
        if state not in {'open','resolved','escalated'}:raise ValueError('Invalid ticket state')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');t=self._ticket(c,owner,tid)
            if t['context_version']!=version:raise Conflict('Ticket changed; reload before changing its state')
            if c.execute("SELECT 1 FROM runs WHERE ticket=? AND status IN ('queued','running')",(tid,)).fetchone():raise Conflict('Stop active answer before changing ticket state')
            if t['state']!=state:
                t['state']=state;t['context_version']+=1;t['updated']=now();self._save_ticket(c,t)
        return self.ticket(owner,tid)

    def create_run(self,owner,tid,version,question,provider):
        q=safe_text(question,self.scrub).strip()
        if not 1<=len(q)<=4000 or provider not in {'local','deepseek'}:raise ValueError('Invalid question or provider')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');t=self._ticket(c,owner,tid)
            if t['context_version']!=version:raise Conflict('Ticket changed; reload before sending')
            if t['state']=='resolved':raise Conflict('Reopen the resolved ticket before sending a follow-up')
            if c.execute("SELECT 1 FROM runs WHERE ticket=? AND status IN ('queued','running')",(tid,)).fetchone():raise Conflict('One active answer per ticket; wait or Stop')
            t['context_version']+=1;t['updated']=now();self._save_ticket(c,t)
            rid=ident();c.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',(ident(),tid,'user',q,rid,now()))
            r={'id':rid,'ticket':tid,'owner':owner,'context_version':t['context_version'],'question':q,'provider':provider,
               'status':'queued','created':now(),'partial':'','answer':'','error':None,'quality':'NOT_ASSESSED','grounding':'NOT_ASSESSED',
               'sources':[],'events':[],'elapsed_s':0,'usage':{},'delivery':None}
            c.execute('INSERT INTO runs VALUES (?,?,?,?,?)',(rid,tid,owner,r['status'],json.dumps(r,ensure_ascii=False)))
        return r

    def run(self,owner,rid):
        with self.connect() as c:r=c.execute('SELECT payload FROM runs WHERE id=? AND owner=?',(rid,owner)).fetchone()
        if not r:raise ValueError('Answer unavailable in this demo session')
        return json.loads(r['payload'])

    def save_run(self,r,final=False):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT status FROM runs WHERE id=? AND owner=?',(r['id'],r['owner'])).fetchone()
            if not old or old['status'] not in {'queued','running'}:return False
            t=self._ticket(c,r['owner'],r['ticket'])
            if t['context_version']!=r['context_version']:
                r.update(status='stale',partial='',answer='',error='Ticket context changed; this answer is not current.')
            c.execute('UPDATE runs SET status=?,payload=? WHERE id=?',(r['status'],json.dumps(r,ensure_ascii=False),r['id']))
            if final and r['status']=='completed':
                c.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)',(ident(),r['ticket'],'assistant',r['answer'],r['id'],now()))
            return r['status']!='stale'

    def cancel(self,owner,rid):
        r=self.run(owner,rid)
        if r['status'] in {'queued','running'}:
            r.update(status='cancelled',partial='',answer='',error='Stopped by ticket owner');self.save_run(r)
        return self.run(owner,rid)

    def snapshot(self,r):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');t=self._ticket(c,r['owner'],r['ticket'])
            if t['context_version']!=r['context_version']:raise Conflict('Ticket changed before MCP snapshot')
            user=json.loads(c.execute('SELECT payload FROM users WHERE id=?',(r['owner'],)).fetchone()['payload'])
            history=[dict(x) for x in c.execute('SELECT role,text FROM messages WHERE ticket=? ORDER BY rowid DESC LIMIT 8',(r['ticket'],))][::-1]
            count=c.execute('SELECT count(*) FROM messages WHERE ticket=?',(r['ticket'],)).fetchone()[0]
        left=12000;bounded=[]
        for m in reversed(history):
            text=m['text'][-left:];left-=len(text);bounded.append(dict(m,text=text))
            if left<=0:break
        snap={'schema_version':'support-context-v1','snapshot_id':ident(),'context_version':t['context_version'],'synthetic':True,
              'user':user,'ticket':t,'history':bounded[::-1],'history_truncated':count>len(bounded) or sum(len(x['text']) for x in history)>12000,
              'omitted_messages':count-len(bounded)}
        blob=json.dumps(snap,ensure_ascii=False).encode('utf-8')
        if len(blob)>65536:raise ValueError('Support snapshot exceeds its size limit')
        folder=self.data/'snapshots';folder.mkdir(exist_ok=True)
        path=folder/(snap['snapshot_id']+'.json')
        with path.open('xb') as f:f.write(blob)
        return path,{'snapshot_id':snap['snapshot_id'],'context_version':snap['context_version'],'sha256':hashlib.sha256(blob).hexdigest()}

    def delivery(self,owner,key):
        with self.connect() as c:row=c.execute('SELECT payload FROM deliveries WHERE id=? AND owner=?',(key,owner)).fetchone()
        return json.loads(row['payload']) if row else None

    def delivery_intent(self,owner,tid,version,action,rid=None):
        if action=='escalation' and rid is not None:
            raise ValueError('Escalation notifications do not accept a run identity')
        if action=='answer' and (not isinstance(rid,str) or not rid):
            raise ValueError('Answer notifications require a completed run identity')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');t=self._ticket(c,owner,tid)
            key=hashlib.sha256(f'{tid}:{rid}:{version}:{action}'.encode()).hexdigest()
            old=c.execute('SELECT payload FROM deliveries WHERE id=? AND owner=?',(key,owner)).fetchone()
            if old:return json.loads(old['payload']),False
            if t['context_version']!=version:raise Conflict('Ticket changed; notification is no longer current')
            if action=='answer':
                row=c.execute('SELECT payload FROM runs WHERE id=? AND owner=? AND ticket=?',(rid,owner,tid)).fetchone()
                if not row:raise ValueError('Completed answer unavailable')
                run=json.loads(row['payload'])
                if run['status']!='completed' or run['context_version']!=version:raise Conflict('Only a current completed answer may be reported')
            elif action!='escalation' or t['state']!='escalated':raise ValueError('Escalate the ticket explicitly first')
            r={'id':key,'ticket':tid,'action':action,'run':rid,'context_version':version,'status':'sending','message_id':None,'created':now()}
            c.execute('INSERT INTO deliveries VALUES (?,?,?,?,?)',(key,tid,owner,r['status'],json.dumps(r)))
        return r,True

    def save_delivery(self,owner,r):
        with self.connect() as c:c.execute('UPDATE deliveries SET status=?,payload=? WHERE id=? AND owner=?',(r['status'],json.dumps(r),r['id'],owner))
