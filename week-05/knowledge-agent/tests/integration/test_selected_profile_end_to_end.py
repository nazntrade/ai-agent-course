"""D22-P01/P02 real loopback transport with an explicitly fake selected provider."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .test_chat_end_to_end import Backend, http, _start_server, _stop_server, _build, post_stream
import embed_stub
from harness.test_profile import TestSession


def test_selected_profile_lease_chat_two_modes_and_stream(tmp_path):
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def send(self,data):
            body=json.dumps(data).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            assert self.headers.get('Authorization') == 'Bearer fixture-key'
            self.send({'data':[{'id':'selected','context_length':8192}]})
        def do_POST(self):
            assert self.headers.get('Authorization') == 'Bearer fixture-key'
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path.endswith('/test-leases'):
                requests.append(('acquire',None))
                self.send({'lease_id':'owned'})
                return
            assert body['model']=='selected'
            assert self.headers.get('X-AI-Test-Model-Lease-Id')=='owned'
            requests.append(('chat',body))
            if body.get('stream'):
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.end_headers()
                for frame in [{'choices':[{'delta':{'content':'Selected answer'},'finish_reason':None}]},
                              {'choices':[{'delta':{},'finish_reason':'stop'}],'usage':{'prompt_tokens':30,'completion_tokens':2}}, '[DONE]']:
                    text=frame if isinstance(frame,str) else json.dumps(frame)
                    self.wfile.write(('data: '+text+'\n\n').encode())
                    self.wfile.flush()
                self.close_connection=True
            else:
                self.send({'model':'selected','choices':[{'message':{'content':'Selected answer'},'finish_reason':'stop'}],
                           'usage':{'prompt_tokens':30,'completion_tokens':2,'total_tokens':32}})
        def do_DELETE(self):
            assert self.headers.get('Authorization') == 'Bearer fixture-key'
            assert self.path.endswith('/owned')
            requests.append(('release',None))
            self.send({})
    selected=_start_server(ThreadingHTTPServer(('127.0.0.1',0),Handler))
    embed=_start_server(embed_stub.create_server('127.0.0.1',0,dimension=64))
    base=f'http://127.0.0.1:{selected.server_port}'
    env={'AI_TEST_MODEL_KIND':'local','AI_TEST_MODEL_NAME':'selected','AI_TEST_MODEL_BASE_URL':base+'/v1',
         'AI_TEST_MODEL_API_KEY':'fixture-key','AI_TEST_MODEL_ID':'fixture',
         'AI_TEST_MODEL_LEASE_URL':base+'/api/local-models/fixture/test-leases',
         # D24 pins this D22 profile regression to the flat rag-v1 path.
         'RAG_GROUNDING_ENABLED':'0',
         'CHAT_RUNS_PATH':str(tmp_path/'runs')}
    backend=None
    source=tmp_path/'source.md'
    source.write_text('# Memory\n\n'+('Agents use memory to store observations. '*60),encoding='utf-8')
    try:
        with TestSession(env) as child:
            backend=Backend(tmp_path/'index.db',f'http://127.0.0.1:{embed.server_address[1]}',base,extra_env=child)
            assert backend.wait()
            collection,_=_build({'base':backend.base,'source':source},'selected-profile')
            for mode in ('without_rag','with_rag'):
                payload={'mode':mode,'question':'Memory?'}
                if mode=='with_rag': payload['collection_id']=collection
                status,record=http('POST',backend.base+'/api/chat',payload)
                assert status==200,record
                assert record['model']['provider']=='openai-compatible'
                assert record['model']['settings']['model_check_kind']=='local'
                assert record['model']['model']=='selected'
                assert record['usage']['input_tokens']==30
                assert 'fixture-key' not in json.dumps(record)
            events=post_stream(backend.base+'/api/chat/stream',{'mode':'without_rag','question':'Memory?'})
            assert [e['type'] for e in events]==['start','token','done']
            backend.stop()
            backend=None
        assert [kind for kind,_ in requests]==['acquire','chat','chat','chat','release']
    finally:
        if backend: backend.stop()
        _stop_server(embed)
        _stop_server(selected)
