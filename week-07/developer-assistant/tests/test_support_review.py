"""Concrete post-review reproductions using the actual shipped JS/functions."""
import asyncio
import json
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from assistant.config import MODULE,Settings
from assistant.support_store import SupportStore
from assistant.support_rag import SupportRag
from assistant.support_service import SupportService
from assistant.support_api import create_app
from fastapi.testclient import TestClient

class NotificationIdentity(unittest.IsolatedAsyncioTestCase):
    async def test_escalation_has_one_identity_and_invalid_runs_never_create_intents(self):
        with tempfile.TemporaryDirectory() as d:
            store=SupportStore(d,MODULE/'examples/support/users-tickets.json');rag=SupportRag(store)
            settings=Settings(data=Path(d),telegram_token='synthetic-token',telegram_chat='synthetic-owner')
            service=SupportService(store,rag,settings);t=store.state('demo-alex','demo-auth',1,'escalated');sent=[]
            class Reply:
                status_code=200
                def json(this):return {'ok':True,'result':{'message_id':555}}
            class HTTP:
                async def __aenter__(this):return this
                async def __aexit__(this,*a):pass
                async def post(this,*a,**kw):sent.append(kw);return Reply()
            with patch('assistant.support_service.httpx.AsyncClient',return_value=HTTP()):
                await service.notify('demo-alex','demo-auth',t['context_version'],'escalation')
                try:await service.notify('demo-alex','demo-auth',t['context_version'],'escalation','')
                except ValueError:pass
                self.assertEqual(len(sent),1,'Empty run identity must not send a second escalation')
                foreign=store.create_run('demo-jamie','demo-index',1,'Foreign owned run','local')
                unrelated=store.create_run('demo-alex','demo-rate',1,'Different ticket owned run','local')
                for rid in ('unknown-run',foreign['id'],unrelated['id']):
                    with self.assertRaises(ValueError):await service.notify('demo-alex','demo-auth',t['context_version'],'escalation',rid)
                self.assertEqual(len(store.ticket('demo-alex','demo-auth')['deliveries']),1,'Invalid run IDs must not persist a sending intent')
                again=await service.notify('demo-alex','demo-auth',t['context_version'],'escalation')
                self.assertTrue(again['duplicate_prevented']);self.assertEqual(len(sent),1)

class FrontendOrdering(unittest.TestCase):
    def run_js(self,program):
        node=shutil.which('node')
        if not node:self.fail('JavaScript runtime required for this declared ordering control')
        env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','PATH','PATHEXT','TEMP','TMP'}}
        r=subprocess.run([node,'-'],input=program,text=True,encoding='utf-8',capture_output=True,timeout=10,env=env)
        self.assertEqual(r.returncode,0,r.stderr)
    def test_actual_persona_mutations_serialize_before_scoped_loads(self):
        js=(MODULE/'static/support/app.js').read_text(encoding='utf-8')
        start=js.index('async function selectPersona(') if 'async function selectPersona(' in js else js.index('function selectPersona(')
        loader=js[start:js.index('async function loadTickets',start)]
        self.run_js(r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
let server='demo-alex',pending=[],loads=[],inflight=0,max=0;const fields={persona:{value:'demo-alex'},thread:{replaceChildren(){}}};
const ctx=vm.createContext({epoch:0,current:null,user:'demo-alex',sessionQueue:Promise.resolve(),sessionChanging:false,pollTimer:null,
 clearTimeout(){},notice(){},render(){},closeContextDialogs(){},$:id=>fields[id],personas:[{id:'demo-alex'}],
 api:(path,body)=>{
   if(path==='/session'&&body){inflight++;max=Math.max(max,inflight);return new Promise((resolve,reject)=>pending.push({value:body.user,finish(){server=body.user;inflight--;resolve({user:server})},reject(){inflight--;reject(Error('Switch failed'))}}));}
   if(path==='/session')return Promise.resolve({user:server});throw Error('Unexpected request '+path);
 },loadTickets:async()=>{loads.push({server,visible:fields.persona.value,user:ctx.user});}
});
vm.runInContext(LOADER,ctx);const tick=()=>new Promise(setImmediate);
(async()=>{
 vm.runInContext("globalThis.a=selectPersona('demo-jamie');globalThis.b=selectPersona('demo-sam')",ctx);await tick();
 assert.equal(pending.length,1,'Only one session mutation may be in flight');assert.equal(ctx.sessionChanging,true);
 pending[0].finish();await tick();assert.equal(pending.length,2);assert.equal(loads.length,0,'Stale selection must not load ticket data');
 pending[1].finish();await Promise.all([ctx.a,ctx.b]);assert.equal(max,1);assert.equal(server,'demo-sam');assert.equal(fields.persona.value,server);assert.equal(ctx.user,server);assert.equal(ctx.sessionChanging,false);
 assert.equal(loads.length,1);assert.equal(loads[0].visible,loads[0].server);
 vm.runInContext("globalThis.c=selectPersona('demo-jamie')",ctx);await tick();pending[2].reject();await ctx.c;
 assert.equal(ctx.user,'demo-sam');assert.equal(fields.persona.value,'demo-sam');assert.equal(ctx.sessionChanging,false);
 console.log('Actual persona mutation ordering and failed-switch restoration passed');
})().catch(e=>{console.error(e);process.exitCode=1});
""".replace('LOADER',json.dumps(loader)))
    def test_actual_old_answer_and_source_rendering_is_historical(self):
        js=(MODULE/'static/support/app.js').read_text(encoding='utf-8');start=js.index('function sourceDetails(');end=js.index('function render(){',start)
        rendering=js[start:end]
        self.run_js(r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
function element(tag,value='',cls=''){return {tag,textContent:value,className:cls,children:[],append(...items){this.children.push(...items)}};}
function all(e){return [e.textContent,...e.children.map(all)].join(' ')}
const run={id:'old',provider:'local',status:'completed',elapsed_s:1,quality:'NOT_ASSESSED',context_version:2,sources:[{label:'F1',path:'docs/support/f.md',start:1,end:2,hash:'abcdef',text:'Older source'}],context:{ticket:{id:'t'},context_version:2,user:{name:'Demo'},transport:'stdio',calls:['get_ticket_context'],snapshot_id:'snap',sha256:'abcdef'}};
const ctx=vm.createContext({document:{createElement:tag=>element(tag)},text:element,current:{id:'t',state:'open',context_version:3},config:{providers:{telegram_configured:true}}});
vm.runInContext(RENDER,ctx);ctx.run=run;vm.runInContext('globalThis.card=renderRun(run);globalThis.source=sourceDetails(run)',ctx);
assert.match(all(ctx.card),/historical/i);assert.match(all(ctx.card),/version 2/);assert.match(all(ctx.card),/version 3/);
assert.doesNotMatch(all(ctx.source),/Current ticket/);assert.match(all(ctx.source),/snapshot/i);assert.doesNotMatch(all(ctx.card),/Send summary/);
ctx.current.context_version=2;vm.runInContext('globalThis.currentCard=renderRun(run)',ctx);assert.match(all(ctx.currentCard),/Send summary/);
console.log('Actual historical answer/snapshot rendering and current notification guard passed');
""".replace('RENDER',json.dumps(rendering)))


    def test_actual_stale_session_conflict_does_not_clear_new_context(self):
        js=(MODULE/'static/support/app.js').read_text(encoding='utf-8');start=js.index('async function api(');end=js.index('function text(',start)
        self.run_js(r"""
const vm=require('node:vm'),assert=require('node:assert/strict');const pending=[];
const ctx=vm.createContext({user:'demo-alex',epoch:1,current:{id:'alex-ticket'},pollTimer:null,sessionChanging:false,
 clearTimeout(){},closeContextDialogs(){},render(){},notice(){},fetch:(path,options)=>new Promise(resolve=>pending.push({path,options,resolve}))});
vm.runInContext(API,ctx);
(async()=>{
 vm.runInContext("globalThis.old=api('/tickets').catch(()=>{})",ctx);
 assert.equal(pending[0].options.headers['X-Support-Persona'],'demo-alex');
 ctx.user='demo-sam';ctx.epoch=2;ctx.current={id:'sam-ticket'};
 pending[0].resolve({ok:false,json:async()=>({error:'Old session mismatch',session_changed:true})});await ctx.old;
 assert.equal(ctx.user,'demo-sam');assert.equal(ctx.current.id,'sam-ticket');assert.equal(ctx.epoch,2);
 vm.runInContext("globalThis.currentRequest=api('/tickets').catch(()=>{})",ctx);
 pending[1].resolve({ok:false,json:async()=>({error:'Current session mismatch',session_changed:true})});await ctx.currentRequest;
 assert.equal(ctx.user,null);assert.equal(ctx.current,null);assert.equal(ctx.sessionChanging,true);assert.equal(ctx.epoch,3);
 console.log('Actual obsolete-session error guard and current invalidation passed');
})().catch(e=>{console.error(e);process.exitCode=1});
""".replace('API',json.dumps(js[start:end])))

class SessionHeaderGuard(unittest.TestCase):
    def test_another_tab_cookie_switch_cannot_read_or_mutate_under_old_selector(self):
        with tempfile.TemporaryDirectory() as d:
            app=create_app(Settings(data=Path(d)))
            with TestClient(app) as client:
                client.post('/api/session',json={'user':'demo-alex'})
                client.post('/api/session',json={'user':'demo-jamie'})
                before=len(app.state.store.tickets('demo-jamie'))
                headers={'X-Support-Persona':'demo-alex'}
                r=client.get('/api/tickets',headers=headers)
                self.assertEqual(r.status_code,409);self.assertTrue(r.json().get('session_changed'));self.assertNotIn('demo-index',r.text)
                r=client.post('/api/tickets',headers=headers,json={'title':'Wrong visible persona','description':'Must not save under another cookie owner'})
                self.assertEqual(r.status_code,409);self.assertEqual(len(app.state.store.tickets('demo-jamie')),before)
                self.assertEqual(client.get('/api/session').json()['user'],'demo-jamie')
                self.assertEqual(client.get('/api/tickets',headers={'X-Support-Persona':'demo-jamie'}).status_code,200)
