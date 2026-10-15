"""D34 actual goal/repeat scenarios. No goal-to-file routing in the product engine."""
import asyncio
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import shutil
from assistant.config import Settings,MODULE
from assistant.runtime import LocalRuntime
from assistant.provider import Provider
from assistant.service import Service
from assistant.store import Store
from assistant.maintenance import Run,digest,inventory

GOALS={
 'impact':'Оцени влияние изменения API завершения обращений и сохрани план миграции с опорой на проект.',
 'documentation':'Подготовь документацию текущего API обращений к выпуску и запиши только подтверждённые изменения.',
 'client':'Приведи клиент к текущему API обращений, сохранив остальное поведение.'}

def documentation_artifacts_changed(changed):
    """Mechanical changed-artifact check only; content still needs full review."""
    if not {'README.md','docs/API.md'}.issubset(changed):return False
    for path in changed:
        name=Path(path).name.lower()
        if not name.endswith('.md'):continue
        stem=name[:-3]
        if stem in {'changelog','release_notes','release-notes'} or stem.startswith(('changelog-','changelog_','release_notes-','release_notes_','release-notes-','release-notes_')):return True
    return False

def selected_settings(data):
    original=Settings.load();original.data=data;data.mkdir(parents=True,exist_ok=True)
    values={k:os.getenv('AI_TEST_MODEL_'+k,'') for k in ('KIND','BASE_URL','NAME','API_KEY')}
    if not all(values.values()):raise ValueError('Complete selected profile is required')
    kind='local' if values['KIND']=='local' else 'deepseek'
    if kind=='local':original.local_url=values['BASE_URL'].rstrip('/');original.local_model=values['NAME'];original.local_key=values['API_KEY']
    else:original.remote_url=values['BASE_URL'].rstrip('/');original.remote_model=values['NAME'];original.remote_key=values['API_KEY']
    original.validate();return original,kind

async def matrix(repeat=False):
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');base=MODULE/'local-data'/'d34-runs'/stamp;base.mkdir(parents=True)
    settings,provider=selected_settings(base/'data');runtime=LocalRuntime(settings);service=Service(Store(settings.data/'test.sqlite3'),settings,Provider(settings,runtime));results=[]
    selected_context=os.getenv('D34_FILES_CONTEXT_CHARS','')
    if selected_context not in ('','40000','100000'):raise ValueError('D34_FILES_CONTEXT_CHARS must be omitted,40000 or100000')
    files_context_chars=int(selected_context) if selected_context else None
    evidence=MODULE/'local-data/d34-evidence';evidence.mkdir(parents=True,exist_ok=True)
    # Parent selected-profile harness owns Local acquire/release. Standalone child
    # without parent readiness must not claim borrowed lifecycle ownership.
    if provider=='local' and os.getenv('AI_TEST_MODEL_PARENT_READY')!='1':raise ValueError('Run this matrix through the selected-profile lifecycle harness')
    async def one(goal_name,root,tag):
        p=service.store.project(root,'TicketDesk '+tag);t=service.create(p['id'],GOALS[goal_name],provider,'files',files_context_chars=files_context_chars);await service.jobs[t['id']];saved=service.store.task(t['id'])
        record={'schema_version':'d34-live-goal-v1','provider':provider,'goal_name':goal_name,'goal':GOALS[goal_name],'tag':tag,'run_id':t['id'],'task_status':saved['status'],
                'settings':{'model':settings.local_model if provider=='local' else settings.remote_model,'temperature':settings.temperature,'max_tokens':settings.max_tokens,'context_chars':settings.context_chars,'thinking':settings.thinking},'files_context_budget':saved['files_context_budget'],
                'answer':saved.get('answer'),'error':saved.get('error'),'events':saved['events'],'plan':saved['plan'],'quality':'NOT_ASSESSED','goal_review':'NOT_ASSESSED','observed':{},'limitations':['Content acceptance requires independent whole-source/output review.']}
        folder=settings.data/'maintenance'/t['id'];files={}
        if (folder/'manifest.json').exists():
            run=Run(folder);m=run.public();entries,text,h=run.diff();record.update(manifest=m,diff={'files':entries,'text':text,'digest':h},transcript=json.loads((folder/'transcript.json').read_text()) if (folder/'transcript.json').exists() else [])
            files={path:(folder/'draft'/path).read_text(encoding='utf-8') for path in m['draft']};record['draft_files']=files
            record['observed']={'distinct_source_reads':len(m['reads']),'changed_files':len(entries),'new_files':[x['path'] for x in entries if x['new_file']],
                'late_line_received':any(e.get('tool')=='read_file' and e.get('status')=='completed' and e.get('result',{}).get('path')=='ticket_api.py' and e.get('result',{}).get('text') and e.get('result',{}).get('end_line',0)>200 for e in saved['events']),
                'second_search_page_received':any(e.get('tool')=='search_text' and e.get('status')=='completed' and not e.get('result',{}).get('error') and e.get('arguments',{}).get('cursor') for e in saved['events']),
                'search_pages':[{'query':e.get('arguments',{}).get('query'),'cursor':e.get('arguments',{}).get('cursor'),'paths':sorted({x.get('path') for x in e.get('result',{}).get('matches',[])}),'complete':e.get('result',{}).get('coverage_complete'),'status':e.get('status')} for e in saved['events'] if e.get('tool')=='search_text']}
            if goal_name=='client':record['trusted_behavior']=run.call('run_fixed_checks',{'suite':'ticketdesk'})['trusted_tests']
        record['mechanical_status']='PASS' if saved['status']=='completed' and record['observed'].get('distinct_source_reads',0)>=3 else 'FAIL'
        changed={e['path'] for e in record.get('diff',{}).get('files',[])}
        if 'already-updated' not in tag:
            if goal_name=='impact' and not record['observed'].get('new_files'):record['mechanical_status']='FAIL'
            if goal_name=='documentation' and not documentation_artifacts_changed(changed):record['mechanical_status']='FAIL'
            if goal_name=='client' and 'ticket_client.py' not in changed:record['mechanical_status']='FAIL'
        if goal_name=='client' and record.get('trusted_behavior',{}).get('status')!='PASS':record['mechanical_status']='FAIL'
        criterion={'impact':'04','documentation':'05','client':'06'}[goal_name]
        filename=f'D34-{criterion}-{tag}-{provider}-{stamp}.json';(evidence/filename).write_text(settings.scrub(json.dumps(record,ensure_ascii=False,indent=2)),encoding='utf-8')
        for number in (['14'] if repeat else ['02','03','07','11','13','15']):
            ref={'criterion':'D34-'+number,'method':'LIVE','goal_artifact':filename,'sha256':digest((evidence/filename).read_bytes()),'mechanical_status':record['mechanical_status'],'quality':'NOT_ASSESSED','limitations':record['limitations']}
            (evidence/f'D34-{number}-{tag}-{provider}-{stamp}.json').write_text(json.dumps(ref,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'goal':goal_name,'tag':tag,'provider':provider,'task':saved['status'],'mechanical':record['mechanical_status'],'artifact':filename,'observed':record['observed']},ensure_ascii=False),flush=True)
        results.append(record)
        return Run(folder),record
    try:
        names=['impact','documentation','client'] if not repeat else ['impact','documentation']
        selected=os.getenv('D34_CASE','')
        if selected:
            if selected not in GOALS:raise ValueError('D34_CASE must be impact, documentation or client')
            names=[selected]
        for name in names:
            root=base/(name+'-fresh');shutil.copytree(MODULE/'examples/maintenance_demo',root)
            run,record=await one(name,root,name+'-fresh')
        if repeat and not selected:
            for name in ['documentation','client']:
                root=base/(name+'-updated');shutil.copytree(MODULE/'examples/maintenance_demo',root)
                run,record=await one(name,root,name+'-prepare-updated')
                if record['mechanical_status']!='PASS':continue
                d=run.get_diff();run.transact(d['revision'],d['digest']);before=inventory(root)
                again,second=await one(name,root,name+'-already-updated')
                entries,_,_=again.diff()
                receipt={'criterion':'D34-14','provider':provider,'goal':name,'first_run':run.m['id'],'repeat_run':again.m['id'],'changed_files':len(entries),'before_inventory':before['version'],'after_inventory':inventory(root)['version'],'observed_noop':len(entries)==0,'quality':'NOT_ASSESSED'}
                (evidence/f'D34-14-noop-{name}-{provider}-{stamp}.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
                if entries:second['mechanical_status']='FAIL'
        if any(r['mechanical_status']!='PASS' for r in results):raise ValueError('One or more actual goal runs failed; immutable evidence retained')
    finally:await service.close();await runtime.close()

