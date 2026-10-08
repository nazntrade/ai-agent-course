"""Derive a column-aware reading view without changing the vector index."""
import pdfplumber,sqlite3,json,re,hashlib,unicodedata,math
from pathlib import Path
root=Path(__file__).resolve().parent.parent
db=root.parents[1]/'week-05/knowledge-agent/local-data/index.db'
c=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
rows=c.execute("SELECT ch.chunk_id,ch.text,ch.metadata_json,s.uri,s.content_sha256 FROM chunks ch JOIN documents d ON d.document_id=ch.document_id JOIN sources s ON s.source_id=d.source_id WHERE ch.index_version_id=(SELECT active_index_version_id FROM collections WHERE name='agents-survey')").fetchall()
source=Path(rows[0]['uri']);digest=hashlib.sha256(source.read_bytes()).hexdigest();assert digest==rows[0]['content_sha256']
def normalize(text):
 text=unicodedata.normalize('NFKC',text)
 text=re.sub(r'(?<=[a-z])-\n(?=[a-z])','',text)
 return re.sub(r'\s+',' ',text).strip()
stop=set('the and with from that this their into they which were have for are its can such also through more these those'.split())
def terms(text):return set(re.findall(r'[a-z]{3,}',text.lower()))-stop
windows=[]
with pdfplumber.open(source) as pdf:
 for n,page in enumerate(pdf.pages,1):
  # Explicit two-column mode for this source, validated against rendered pages.
  for column,(x0,x1) in enumerate(((0,page.width/2),(page.width/2,page.width)),1):
   text=normalize(page.crop((x0,52,x1,page.height-28)).extract_text(x_tolerance=1,y_tolerance=3) or '')
   for start in range(0,len(text),1050):
    left=text.find(' ',start) + 1 if start else 0
    right=text.rfind(' ',left,min(left+1600,len(text))) if left+1600<len(text) else len(text)
    snippet=text[left:right]
    if len(snippet)>100:windows.append({'page':n,'column':column,'text':snippet,'terms':terms(snippet)})
views={}
for row in rows:
 meta=json.loads(row['metadata_json']);wanted=terms(row['text']);ranked=[]
 for w in windows:
  if not meta.get('page_start',1)<=w['page']<=meta.get('page_end',42):continue
  common=wanted&w['terms'];score=len(common)/math.sqrt(max(1,len(wanted)*len(w['terms'])))
  ranked.append((score,w))
 selected=[]
 for score,w in sorted(ranked,key=lambda a:-a[0]):
  if score<0.36 or len(selected)>=2:break
  if any(v['page']==w['page'] and v['column']==w['column'] for v in selected):continue
  selected.append({k:v for k,v in w.items() if k!='terms'}|{'overlap_score':round(score,5)})
 if selected:
  selected.sort(key=lambda v:(v['page'],v['column']))
  views[row['chunk_id']]={'original_text_sha256':hashlib.sha256(row['text'].encode()).hexdigest(),'passages':selected}
out={'schema':1,'source_sha256':digest,'source_path':str(source),'method':'PDF two columns; x_tolerance=1; NFKC; line-wrap dehyphenation; original chunks unchanged','views':views}
path=root/'local-data/day28-reading-view.json';path.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
print('Reading views:',len(views),'of',len(rows))
print(json.dumps(views.get('d01f7f89e63a3215a691c1389ca9367fcedcf0d18000446d55efaa45538da5d9'),ensure_ascii=False,indent=2))
