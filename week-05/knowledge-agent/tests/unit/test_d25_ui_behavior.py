"""Execute the mini-chat UI against a fake DOM to check request lifecycle."""
from pathlib import Path
import shutil
import subprocess
import pytest

NODE = shutil.which("node")
UI = Path(__file__).resolve().parents[2] / "knowledge_agent" / "ui" / "app.js"

@pytest.mark.skipif(NODE is None, reason="Node is required for JavaScript behavior checks")
def test_dialogue_switch_retry_and_failed_citation_ui():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function element() {
  const classes = new Set();
  return { children: [], value: '', style: {}, disabled: false, attributes: {},
    classList: { toggle(name, force) { const enabled=force ?? !classes.has(name); enabled ? classes.add(name) : classes.delete(name); return enabled; }, contains(name) { return classes.has(name); } },
    appendChild(child) { this.children.push(child); child.parentNode=this; },
    removeChild(child) { this.children=this.children.filter(item=>item!==child); child.parentNode=null; },
    setAttribute(name, value) { this.attributes[name]=value; },
  };
}
const nodes = new Map();
const $ = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
for (const [id, value] of Object.entries({'chat-top-k':'5', 'd23-min-score':'0', 'd23-prefilter':'20', 'd23-postfilter':'5', 'd23-mode':'A', 'chat-strategy':'active'})) $(id).value=value;
const context=vm.createContext({ console, $, crypto:{randomUUID:()=>String(++context.uuid)}, uuid:0,
  state:{collectionId:'collection'}, document:{createElement:element,createTextNode:text=>({textContent:text})}, showError:()=>{}, api:()=>{},
});
const full=fs.readFileSync(process.argv[2],'utf8');
const source=full.slice(full.indexOf('const d25 = {')).split('\nwireD25();')[0];
vm.runInContext(source,context);
vm.runInContext('renderDialogueList=()=>{}; renderConversation=()=>{}; renderMemory=()=>{}; d25.dialogueId="a";', context);
(async()=>{
  let finish;
  context.api=(path)=>path.endsWith('/turns') ? new Promise(resolve=>finish=resolve) : Promise.resolve(path.endsWith('/turns?limit=200') ? {turns:[]} : {memory:null});
  $('d25-input').value='Question';
  const pending=vm.runInContext('sendDialogueMessage()',context);
  await vm.runInContext('openDialogue("b")',context);
  finish({turn_id:'t1',status:'ok',answer:{text:'answer'}});
  await pending;
  assert.equal(vm.runInContext('d25.busy',context),false,'switching dialogue must release the send lock');
  assert.equal($('d25-send').disabled,false);
  const requests=[];
  context.api=async(path,options)=>{ if(options?.method==='POST') { requests.push(JSON.parse(options.body)); if(requests.length===1) throw new Error('response lost'); return {turn_id:'t2',status:'ok',answer:{text:'answer'}}; } return {}; };
  await vm.runInContext('sendDialogueMessage()',context);
  await vm.runInContext('sendDialogueMessage()',context);
  assert.equal(requests[0].client_turn_id,requests[1].client_turn_id,'retry must reuse the idempotency key');
  vm.runInContext('wireD25()',context);
  $('rag-toggle').onclick();
  assert.equal($('chat-layout').classList.contains('rag-hidden'),true);
  assert.equal($('rag-toggle').attributes['aria-expanded'],'false');
  $('rag-toggle').onclick();
  assert.equal($('chat-layout').classList.contains('rag-hidden'),false);
  vm.runInContext('renderTurn({status:"citation_failed",answer:{text:"unverified"}})',context);
  assert.equal($('conversation').children.at(-1).className,'message assistant error');
  vm.runInContext('d25.turns=[{turn_id:"recent",ordinal:201}]; d25.turnTotal=201;',context);
  const pages=[];
  context.api=async path=>{ pages.push(path); return {turns:[{turn_id:'older',ordinal:200}],total:201}; };
  await vm.runInContext('loadOlderDialogueTurns()',context);
  assert.ok(pages[0].endsWith('limit=200&before=201'));
  assert.equal(vm.runInContext('d25.turns.map(t=>t.turn_id).join(",")',context),'older,recent');
  assert.equal(vm.runInContext('d25.olderRequest',context),null);
  context.api=()=>new Promise(resolve=>finish=resolve);
  const olderPending=vm.runInContext('loadOlderDialogueTurns()',context);
  vm.runInContext('d25.dialogueId="other"; d25.requestId++; d25.turns=[];',context);
  finish({turns:[{turn_id:'stale',ordinal:1}],total:201});
  await olderPending;
  assert.equal(vm.runInContext('d25.turns.length',context),0,'an older-page response must not leak into another dialogue');
  vm.runInContext('d25.dialogueId="a"; d25.memory=null;',context);
  $('d25-input').value='Another question';
  context.api=path=>path.endsWith('/turns') ? Promise.resolve({turn_id:'late-memory',answer:{text:'answer'}})
    : path==='/api/dialogues/a/memory' ? new Promise(resolve=>finish=resolve)
    : Promise.resolve(path.endsWith('/turns?limit=200') ? {turns:[]} : {memory:{dialogue_id:'b'}});
  const memoryPending=vm.runInContext('sendDialogueMessage()',context);
  await new Promise(resolve=>setImmediate(resolve));
  await vm.runInContext('openDialogue("b")',context);
  finish({dialogue_id:'a'});
  await memoryPending;
  assert.equal(vm.runInContext('d25.memory.dialogue_id',context),'b','late memory fetch must not overwrite the selected dialogue');
  let releaseOld;
  let opens=0;
  context.api=path=>path.endsWith('/turns?limit=200') && ++opens===1 ? new Promise(resolve=>releaseOld=resolve)
    : Promise.resolve(path.endsWith('/turns?limit=200') ? {turns:[{turn_id:'latest',ordinal:2}],total:2} : {memory:null});
  const oldOpen=vm.runInContext('openDialogue("a")',context);
  await vm.runInContext('openDialogue("b")',context);
  await vm.runInContext('openDialogue("a")',context);
  releaseOld({turns:[{turn_id:'stale',ordinal:1}],total:1});
  await oldOpen;
  assert.equal(vm.runInContext('d25.turns[0].turn_id',context),'latest','A-B-A navigation must not accept an old page response');
  vm.runInContext('d25.lastUserTurnId="user"; renderMemoryEditor({version:1,goal:{text:"old"},constraints:[]});',context);
  const editor=$('memory-block').children;
  editor[1].value='new goal';
  context.api=()=>new Promise(resolve=>finish=resolve);
  const editPending=editor.find(item=>item.textContent==='Save').onclick();
  vm.runInContext('d25.dialogueId="b"; d25.requestId++; d25.memory={dialogue_id:"b"};',context);
  finish({dialogue_id:'a',version:2});
  await editPending;
  assert.equal(vm.runInContext('d25.memory.dialogue_id',context),'b','late editor response must remain in its original dialogue');
  vm.runInContext('d25.dialogueId="a"; d25.turns=[{turn_id:"existing"}]; d25.turnTotal=1;',context);
  $('d25-input').value='Retry after history refresh';
  context.api=path=>Promise.resolve(path.endsWith('/turns') ? {turn_id:'existing',answer:{text:'saved'}} : {dialogue_id:'a'});
  await vm.runInContext('sendDialogueMessage()',context);
  assert.equal(vm.runInContext('d25.turns.length',context),1,'a saved idempotent turn must not be rendered twice');
  assert.equal(vm.runInContext('d25.turnTotal',context),1);
  $('chat-top-k').value='10'; $('d23-mode').value='A';
  const plainPayload=vm.runInContext('d25StrategyPayload()',context);
  assert.equal(plainPayload.prefilter_top_k,10,'visible plain-chat top-K must control the actual request');
  assert.equal(plainPayload.postfilter_top_k,10);
  $('d23-mode').value='B';
  const filteredPayload=vm.runInContext('d25StrategyPayload()',context);
  assert.equal(filteredPayload.prefilter_top_k,20); assert.equal(filteredPayload.postfilter_top_k,5);
  const memoryReply=element(); context.memoryReply=memoryReply;
  vm.runInContext('renderTurnSources(memoryReply,{answer:{task_state_summary:true},sources:[{source:"unrelated document"}]})',context);
  assert.match(memoryReply.children[0].textContent,/confirmed task memory/,"document retrieval must not be shown as evidence for the user's saved goal");
  vm.runInContext('renderTurn({status:"incomplete",answer:{text:"cut off",truncated:true}})',context);
  assert.equal($('conversation').children.at(-1).className,'message assistant error');
  $('d25-input').value='Incomplete answer test';
  context.api=path=>Promise.resolve(path.endsWith('/turns') ? {turn_id:'cut',status:'incomplete',answer:{text:'cut off',truncated:true}} : {});
  await vm.runInContext('sendDialogueMessage()',context);
  assert.match($('conversation-state').textContent,/incomplete/);
  assert.equal($('conversation-state').className,'conversation-state error');
  const memoryGround=element(); context.memoryGround=memoryGround;
  vm.runInContext('addMemoryItem(memoryGround,"Goal","saved",["very-old-full-turn-id"])',context);
  const groundLine=memoryGround.children[0].children[1];
  const groundButton=groundLine.children[0];
  const groundText=groundLine.children[1];
  const groundPaths=[];
  context.api=async path=>{groundPaths.push(path);return {user_message:'Original confirmed goal'};};
  await groundButton.onclick();
  assert.equal(groundPaths[0],'/api/dialogues/a/turns/very-old-full-turn-id');
  assert.match(groundText.textContent,/Original confirmed goal/,'memory grounds must open a complete original user message even outside the loaded history page');
  assert.equal(groundButton.title,'very-old-full-turn-id');
  context.api=()=>new Promise(resolve=>finish=resolve);
  const groundPending=groundButton.onclick();
  vm.runInContext('d25.dialogueId="b"; d25.requestId++;',context);
  finish({user_message:'stale goal'});
  await groundPending;
  assert.doesNotMatch(groundText.textContent,/stale goal/,'a late ground lookup must remain isolated');
  const checkedReply=element(); context.checkedReply=checkedReply;
  vm.runInContext('renderTurnSources(checkedReply,{answer:{grounding:{status:"verified",meaning_check:"not_performed"}},sources:[{source:"corpus",chunk_id:"chunk"}],citations:[]})',context);
  assert.match(checkedReply.children[0].children[1].textContent,/Citation check: verified.*Meaning support: not checked/,'formal quote verification must not claim semantic support');
  console.log('D25_UI_BEHAVIOR: PASS');
})().catch(error=>{ console.error(error); process.exitCode=1; });
"""
    result = subprocess.run([NODE, "-", str(UI)], input=script, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "D25_UI_BEHAVIOR: PASS" in result.stdout
