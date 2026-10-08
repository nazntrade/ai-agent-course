"""Real D28 evidence capture. Semantic quality requires an explicit review."""
from __future__ import annotations
import sys,os,json,time,hashlib,urllib.request,urllib.error
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from app.config import apply_env_file,load_settings
from app.context.builder import ContextBuilder
from app.providers.network_deepseek import DeepSeekProvider
from app.rag.citations import CitationVerifier
from app.rag.grounded_answer import generate_grounded
from harness.live_policy import report_live_blocked

QUESTIONS=[
('architecture','What roles do the profile, memory, planning and action modules play in the architecture of LLM-based autonomous agents, according to the survey?'),
('memory','According to the survey, how do short-term and long-term memory differ, and how are memory reading, writing and reflection used by agents?'),
('planning','According to the survey, how does planning with feedback differ from planning without feedback? Give supported examples.'),
('evaluation','According to the survey, what approaches are used to evaluate LLM-based autonomous agents, and what limitations do these evaluations have?'),
('insufficient-context','What is the exact MD5 checksum of the original agents-survey.pdf file? Give the checksum only if it is stated in the supplied document fragments; otherwise explain that the provided evidence does not establish it.')]
SUFFIX=''

def api(path,data=None):
    req=urllib.request.Request(os.environ.get('D28_APP_URL','http://127.0.0.1:8790').rstrip('/')+'/api'+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=650) as response:return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError('Application HTTP '+str(exc.code)+': '+exc.read().decode()[:500]) from None

def save(out,name,payload):
    (out/name).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')

def capture(out,index,topic,question):
    dialog=api('/dialogues',{'name':f'D28 Q{index} · {topic}'})
    started=time.perf_counter()
    try:
        result=api('/ask',{'dialogue_id':dialog['dialogue_id'],'question':question,'rag_enabled':True,'top_k':5})
        artifact={'question':question,'topic':topic,'response':result,'quality_status':'NOT_ASSESSED','quality_ok':None,'elapsed_ms':round((time.perf_counter()-started)*1000,3)}
    except Exception as exc:
        artifact={'question':question,'topic':topic,'error':str(exc),'quality_status':'FAIL','quality_ok':False}
    save(out,f'q-{index}.json',artifact)
    print(f'Q{index}: '+('captured, semantic review pending' if 'response' in artifact else artifact['error']),flush=True)
    return artifact

def run():
    if report_live_blocked('D28_STATUS'):return 3
    apply_env_file(ROOT/'.env')
    settings=load_settings(os.environ)
    out=ROOT/'local-data/acceptance/day-28'/('run-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    out.mkdir(parents=True)
    index=Path(settings.week05_index_path)
    hashes={str(p.name):hashlib.sha256(p.read_bytes()).hexdigest() for p in [index,index.with_name(index.name+'-wal')] if p.exists()}
    save(out,'preflight.json',{'provider':api('/provider'),'index':api('/rag/collections'),'source_hashes_before':hashes})
    print('Evidence directory: '+str(out),flush=True)
    answers=[capture(out,i,topic,q+SUFFIX) for i,(topic,q) in enumerate(QUESTIONS,1)]
    repeats=[capture(out,'repeat-'+str(i),QUESTIONS[i-1][0],QUESTIONS[i-1][1]+SUFFIX) for i in (1,2)]
    save(out,'stability.json',{'status':'NOT_ASSESSED','questions':[1,2],'repeats':repeats,'note':'Compare substantive claims and support, not character positions.'})
    provider=DeepSeekProvider(base_url=settings.deepseek_base_url,model_id=settings.deepseek_model_id,api_key=settings.deepseek_api_key,timeout=300,max_output_tokens=4096,temperature=0)
    comparisons=[]
    for i,artifact in enumerate(answers,1):
        if 'response' not in artifact:
            comparisons.append({'question':i,'error':'Local evidence capture failed'});continue
        sources=artifact['response']['rag']['sources']
        fragments=[{**s,'text':s['quote']} for s in sources]
        messages,trace=ContextBuilder(max_context_chars=max(settings.rag_max_context_chars, sum(len(f["text"]) for f in fragments)+len(artifact["question"])+4000)).build(question=artifact['question'],fragments=fragments,rag_enabled=True)
        try:
            result=generate_grounded(provider,messages,fragments)
            item={'question':artifact['question'],'answer':result.to_dict(),'sources':trace['sources'],'citations':CitationVerifier(fragments).verify(result.text).to_dict(),'quality_status':'NOT_ASSESSED','quality_ok':None,'identical_fragments':[(s['chunk_id'],s['quote']) for s in sources]==[(s['chunk_id'],s['quote']) for s in trace['sources']]}
        except Exception as exc:item={'question':artifact['question'],'error':type(exc).__name__+': '+str(exc),'quality_status':'FAIL','quality_ok':False}
        comparisons.append(item);save(out,'deepseek-comparison.json',{'status':'NOT_ASSESSED','results':comparisons});print(f'DeepSeek Q{i}: '+('captured' if 'answer' in item else item['error']),flush=True)
    after={str(p.name):hashlib.sha256(p.read_bytes()).hexdigest() for p in [index,index.with_name(index.name+'-wal')] if p.exists()}
    save(out,'summary.json',{'status':'NOT_ASSESSED','reason':'Human semantic review and UI evidence required','source_unchanged':hashes==after,'source_hashes_after':after,'captured_answers':sum('response' in x for x in answers),'deepseek_answers':sum('answer' in x for x in comparisons)})
    print('D28_STATUS: NOT_ASSESSED — evidence captured; review is required',flush=True)
    return 2

if __name__=='__main__':raise SystemExit(run())
