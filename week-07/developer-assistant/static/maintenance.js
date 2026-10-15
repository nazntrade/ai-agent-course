/* Project-files controls inside the existing conversation. */
window.MaintenanceUI=(()=>{
  function context(t){const captured=epoch;return {base:`/api/projects/${t.project_id}/files/${t.id}`,body:{chat_id:t.chat_id},valid:()=>captured===epoch&&projectId===t.project_id&&chatId===t.chat_id};}
  async function refresh(t,c){if(!c.valid())return;await refreshThread();}
  const actionGenerations=new Map();
  async function transaction(t,card,c,kind,body,success){
    if(!c.valid()||cards.get(t.id)!==card||!runs.has(t.id))return;
    const sequence=(actionGenerations.get(t.id)||0)+1;actionGenerations.set(t.id,sequence);taskRefreshGeneration++;
    const current=()=>c.valid()&&actionGenerations.get(t.id)===sequence&&cards.get(t.id)===card&&runs.has(t.id);
    let actionError=null;
    try{await api(c.base+'/'+kind,body);}catch(error){actionError=error;}
    if(!current())return;
    taskRefreshGeneration++;
    let refreshError=null;
    try{
      const saved=await api(`/api/tasks/${t.id}`);
      if(!current())return;
      const known=runs.get(t.id);
      if(saved.id!==t.id||saved.project_id!==t.project_id||saved.chat_id!==t.chat_id||saved.mode!=='files'||(known.maintenance?.revision!==undefined&&(saved.maintenance?.revision===undefined||saved.maintenance.revision<known.maintenance.revision))||(['completed','failed','cancelled'].includes(known.status)&&saved.status!==known.status))throw Error('Saved run response is stale or belongs to another context');
      saved.run_order=known.run_order;mergeRun(saved);card.filesKey=null;renderThread();
    }catch(error){refreshError=error;}
    if(!current())return;
    taskRefreshGeneration++;clearTimeout(pollTimer);pollTimer=setTimeout(()=>guarded(()=>refreshThread()),3000);
    if(refreshError)notice(`${actionError?String(actionError.message||actionError)+'. ':''}Latest saved file state could not be refreshed. Reload this run before another file operation.`);
    else notice(actionError?String(actionError.message||actionError):success);
  }
  function details(title){const d=node('details',undefined,'maintenance-panel');d.append(node('summary',title));return d;}
  async function readAll(t,path){const c=context(t);let text='',start=1,continuation=null,last;
    do{const params=new URLSearchParams({chat_id:t.chat_id,path,start_line:String(start),count:'200'});if(continuation)params.set('continuation',continuation);last=await api(c.base+'/read?'+params);if(!c.valid())return null;text+=last.text;if(last.next_continuation)continuation=last.next_continuation;else{continuation=null;start=last.next_start_line;}}while(continuation||start);
    return {text,last,c};
  }
  async function editor(t,path){const value=await readAll(t,path);if(!value)return;const {text,last,c}=value;
    const d=node('dialog',undefined,'maintenance-editor');const h=node('h2',path);const info=node('p',`Persisted ${last.origin} · ${last.sha256} · revision ${last.revision}`,'version');const input=node('textarea');input.value=text;input.rows=22;input.setAttribute('aria-label',`Draft contents of ${path}`);const msg=node('p','','hint');
    const close=button('Close',()=>d.close());const save=button('Save draft',async()=>guarded(async()=>{if(!c.valid())throw Error('Project or chat changed; reopen this draft');await api(c.base+'/edit',{...c.body,path,content:input.value,expected_hash:last.version});d.close();await refresh(t,c);notice('Draft saved. Run checks and reopen Diff before Apply.');}));save.disabled=['queued','running'].includes(t.status)||selectedChat?.archived||t.maintenance?.application==='APPLIED';
    const actions=node('div',undefined,'dialog-actions');actions.append(close,save);d.append(h,info,input,msg,actions);document.body.append(d);d.addEventListener('close',()=>d.remove());d.showModal();
  }
  function update(card,t){
    if(t.mode!=='files')return;
    card.sb.hidden=true;card.pb.hidden=true;card.db.hidden=true;card.report.hidden=true;
    if(!card.filesRoot){card.filesRoot=node('div',undefined,'maintenance-result');card.body.after(card.filesRoot);}
    const m=t.maintenance||{};const key=json([t.status,m.revision,m.stage,m.application,m.journal?.status,t.events?.length,m.checks]);if(card.filesKey===key)return;card.filesKey=key;
    const root=card.filesRoot;const open=new Set([...root.querySelectorAll('details[open]')].map(x=>x.firstChild.textContent));root.replaceChildren();
    root.append(node('p',`Project files · ${m.stage||t.status} · revision ${m.revision??'pending'} · Application: ${m.application||'NOT_APPLIED'}`,'maintenance-state'));
    const budget=t.files_context_budget;const origin={profile_default:'profile default',explicit_run:'explicitly selected',retry_preserved:'retry preserved',legacy_retry_profile_default:'legacy retry with current profile default'};
    root.append(node('p',budget?`Context budget: ${budget.effective_chars} characters · ${origin[budget.source]||budget.source}`:'Context budget: unknown (historical run)','hint'));
    card.retry.title=budget?'Retry preserves this saved context budget regardless of the composer.':'Historical budget unknown. Retry creates a new run using the current profile default.';
    root.append(node('p',`Goal review: ${m.goal_review||'NOT_ASSESSED'} · Quality: ${m.quality||'NOT_ASSESSED'}. Draft readiness and execution do not establish content acceptance.`,'hint'));
    const activity=details('Activity');for(const [i,e] of (t.events||[]).entries()){const d=details(`${i+1}. ${e.tool} · ${e.status}`);d.append(node('pre',json(e)));activity.append(d);}root.append(activity);
    const files=details('Files');const paths=[...new Set([...Object.keys(m.draft||{}),...Object.keys(m.reads||{})])];
    if(!paths.length)files.append(node('p','No file evidence yet.','hint'));
    for(const path of paths){const row=node('div',undefined,'maintenance-file');const meta=m.draft?.[path]||m.inventory?.files?.[path];row.append(button(path,()=>guarded(()=>editor(t,path))),node('small',`${m.draft?.[path]?'Draft':'Source'} · ${meta?.sha256?.slice(0,16)||''}`));files.append(row);}
    if(m.inventory)files.append(node('p',`Snapshot: ${Object.keys(m.inventory.files||{}).length} supported files · coverage ${m.inventory.complete?'complete within exclusions':'INCOMPLETE'} · excluded ${json(m.inventory.exclusions)}`,'hint'));root.append(files);
    const diff=details('Diff');const diffText=node('pre','Open to load the actual persisted baseline-to-draft diff.');diff.append(diffText);let reviewed=null;const c=context(t);
    diff.addEventListener('toggle',()=>{if(!diff.open||diff.dataset.loaded)return;diff.dataset.loaded='yes';guarded(async()=>{let cursor=null,text='',last;do{const q=new URLSearchParams(c.body);if(cursor)q.set('cursor',cursor);last=await api(c.base+'/diff?'+q);if(!c.valid())return;text+=last.text;cursor=last.next_cursor;}while(cursor);diffText.textContent=text||'No changed bytes.';diff.append(node('p',`Revision ${last.revision} · digest ${last.digest}`,'version'));if(t.status==='completed'&&m.checks?.static?.status==='PASS'&&m.application!=='APPLIED'&&m.application!=='UNDONE'){await api(c.base+'/review',{...c.body,revision:last.revision,digest:last.digest});if(!c.valid())return;reviewed={revision:last.revision,digest:last.digest};apply.disabled=false;}});});root.append(diff);
    const checks=details('Checks');checks.append(node('pre',json(m.checks||{static:'NOT_ASSESSED',trusted_tests:'NOT_ASSESSED',quality:'NOT_ASSESSED'})));checks.append(button('Run static checks',()=>guarded(async()=>{await api(c.base+'/checks',c.body);await refresh(t,c);})));root.append(checks);
    const actions=node('div',undefined,'maintenance-actions');const apply=button('Apply',()=>{if(!c.valid())return;if(!reviewed){notice('Reopen this run’s Diff before Apply');return;}return transaction(t,card,c,'apply',{...c.body,...reviewed},'Reviewed draft applied. Inspect the recorded application state.');});apply.disabled=true;apply.title='Open Diff to review the current revision first';
    const undo=button('Undo',()=>transaction(t,card,c,'undo',c.body,'Guarded Undo completed.'));undo.hidden=m.application!=='APPLIED';apply.hidden=['APPLIED','UNDONE','ROLLED_BACK'].includes(m.application);
    const recover=button('Recover rollback',()=>transaction(t,card,c,'recover',c.body,'Interrupted batch rolled back with per-file guards.'));recover.hidden=!m.journal||['applied','undone','noop','rolled_back'].includes(m.journal.status);
    const busy=['queued','running'].includes(t.status);for(const b of [apply,undo,recover])if(busy||selectedChat?.archived)b.disabled=true;actions.append(apply,undo,recover);root.append(actions);
    if(m.journal){const journal=details('Application journal');journal.append(node('pre',json({...m.journal,files:m.journal.files.map(({before,after,...x})=>x)})));root.append(journal);}
    for(const d of root.querySelectorAll('details'))if(open.has(d.firstChild.textContent))d.open=true;
  }
  return {update};
})();
