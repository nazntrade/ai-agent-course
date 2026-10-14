"""Separate allowlisted product FAQ corpus with actual FTS5/BM25 retrieval."""
import hashlib
import re
from pathlib import Path
from .config import MODULE

def query_terms(query):
    stop={'the','and','how','what','does','why','with','this','that','for','not','work','works','working','product','assistant',
          'почему','как','что','это','мне','меня','работает','работают','не','на','в','и','у','по','ли','так','проблема','приложение'}
    parts=[x.lower() for x in re.findall(r'\w+',query) if len(x)>2 and x.lower() not in stop]
    aliases={'авторизац':'authentication key provider','ключ':'key credential configuration','лимит':'rate limit github',
             'локаль':'local server connection','подключ':'connection endpoint server','источник':'source citation rag',
             'документ':'documentation index rag','индекс':'index reindex rag','стар':'version reindex',
             'останов':'stop cancel','истори':'history persistence','уведом':'telegram notification','телег':'telegram notification'}
    for term in list(parts):
        for stem,words in aliases.items():
            if stem in term:parts.extend(words.split())
    return list(dict.fromkeys(parts))[:32]

class SupportRag:
    def __init__(self,store,root=MODULE):
        self.store=store;self.root=Path(root).resolve()
        with store.connect() as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS faq_chunks(id TEXT PRIMARY KEY,path TEXT,hash TEXT,start INTEGER,end INTEGER,title TEXT,text TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS faq_fts USING fts5(id UNINDEXED,text,tokenize='unicode61');
            CREATE TABLE IF NOT EXISTS faq_meta(id INTEGER PRIMARY KEY,version TEXT,documents INTEGER,chunks INTEGER,indexed TEXT);""")
        self.index()

    def paths(self):
        candidates=[self.root/'docs/architecture.md',self.root/'docs/api.md']
        support=self.root/'docs/support'
        if support.is_dir():candidates.extend(sorted(support.glob('*.md')))
        result=[]
        for p in candidates:
            if not p.is_file() or p.stat().st_size>200000:continue
            chain=[p,*p.parents]
            if any(x.is_symlink() or x.is_junction() for x in chain):continue
            if not p.resolve().is_relative_to(self.root):continue
            result.append(p)
        return result

    def index(self):
        from .store import now
        chunks=[];versions=[];paths=self.paths()
        for p in paths:
            text=p.read_text(encoding='utf-8');h=hashlib.sha256(text.encode()).hexdigest();path=p.relative_to(self.root).as_posix();versions.append(path+':'+h)
            lines=text.splitlines();title=p.stem;current=[];start=1
            def flush(end):
                nonlocal current,start
                if current:
                    body='\n'.join(current);cid=hashlib.sha256(f'{path}:{h}:{start}:{end}'.encode()).hexdigest()[:24]
                    chunks.append((cid,path,h,start,end,title,body));current=[]
            for i,line in enumerate(lines,1):
                if line.startswith('#'):
                    flush(i-1);title=line.lstrip('# ').strip();start=i
                if len(line)>2000:
                    flush(i-1)
                    for j in range(0,len(line),2000):
                        body=line[j:j+2000];cid=hashlib.sha256(f'{path}:{h}:{i}:{j}'.encode()).hexdigest()[:24]
                        chunks.append((cid,path,h,i,i,title,body))
                    start=i+1;continue
                if current and (len(current)>=50 or sum(len(x)+1 for x in current)+len(line)>2200):flush(i-1);start=i
                if not current:start=i
                current.append(line)
            flush(len(lines))
        version=hashlib.sha256('\n'.join(versions).encode()).hexdigest()
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');c.execute('DELETE FROM faq_fts');c.execute('DELETE FROM faq_chunks')
            c.executemany('INSERT INTO faq_chunks VALUES (?,?,?,?,?,?,?)',chunks)
            c.executemany('INSERT INTO faq_fts VALUES (?,?)',[(x[0],x[5]+' '+x[6]) for x in chunks])
            c.execute('INSERT OR REPLACE INTO faq_meta VALUES (1,?,?,?,?)',(version,len(paths),len(chunks),now()))
        return self.manifest()

    def manifest(self):
        with self.store.connect() as c:return dict(c.execute('SELECT * FROM faq_meta WHERE id=1').fetchone())

    def articles(self):
        with self.store.connect() as c:return [dict(r) for r in c.execute('SELECT * FROM faq_chunks ORDER BY path,start')]

    def retrieve(self,query,budget=18000):
        parts=query_terms(query)
        if not parts:return []
        expr=' OR '.join('"'+x.replace('"','')+'"' for x in parts)
        with self.store.connect() as c:
            rows=c.execute('SELECT c.*,bm25(faq_fts) score FROM faq_fts JOIN faq_chunks c ON c.id=faq_fts.id WHERE faq_fts MATCH ? ORDER BY score,path,start LIMIT 6',(expr,)).fetchall()
        result=[];left=budget
        for row in rows:
            if left<=0:break
            source=dict(row);source['text']=source['text'][:left];left-=len(source['text']);source['label']='F'+str(len(result)+1);result.append(source)
        return result
