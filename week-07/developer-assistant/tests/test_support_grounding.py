"""Actual retrieval/configuration controls for grounded credential guidance."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from dotenv import load_dotenv
from fastapi.testclient import TestClient
from assistant.config import MODULE,Settings
from assistant.support_store import SupportStore
from assistant.support_rag import SupportRag
from assistant.support_service import SupportService
from assistant.support_api import create_app as support_app
from assistant.api import create_app as main_app

class CredentialKnowledge(unittest.TestCase):
    def test_actual_retrieval_distinguishes_secret_source_and_process_lifetime(self):
        with tempfile.TemporaryDirectory() as d:
            rag=SupportRag(SupportStore(d))
            sources=rag.retrieve('Почему не работает авторизация? deepseek 401 invalid_key Ключ уже заменён в конфигурации.')
            self.assertTrue(sources)
            excerpt='\n'.join(x['text'] for x in sources)
            self.assertIn('DEEPSEEK_API_KEY',excerpt)
            self.assertIn('override=False',excerpt)
            self.assertIn('precedence',excerpt)
            self.assertIn('not API keys',excerpt)
            self.assertIn('does not refresh',excerpt)
            self.assertIn('not evidence',excerpt)
            self.assertTrue(any(x['path']=='docs/support/providers.md' for x in sources))
            self.assertTrue(all(len(x['text'])<=2200 and x['hash'] and x['id'] for x in sources))
            print('Real FAQ retrieval: credential environment/.env source, precedence, independent process lifetime and uncertainty are supplied')
    def test_actual_dotenv_existing_environment_wins_without_reading_any_file(self):
        # Feed synthetic values through the real library's stream API; no .env is read.
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'synthetic-existing'},clear=False):
            load_dotenv(stream=io.StringIO('DEEPSEEK_API_KEY=synthetic-file\n'),override=False)
            self.assertEqual(os.environ['DEEPSEEK_API_KEY'],'synthetic-existing')
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':''},clear=False):
            load_dotenv(stream=io.StringIO('DEEPSEEK_API_KEY=synthetic-file\n'),override=False)
            self.assertEqual(os.environ['DEEPSEEK_API_KEY'],'')
    def test_actual_settings_credentials_do_not_come_from_runtime_json(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            # This isolated synthetic settings fixture is not the user's configuration.
            (Path(d)/'settings.json').write_text(json.dumps({'local_model':'fixture-model','remote_key':'synthetic-json-value'}),encoding='utf-8')
            with patch.dict(os.environ,{'ASSISTANT_DATA':d,'ASSISTANT_SKIP_ENV':'1','DEEPSEEK_API_KEY':'synthetic-env-value'}):
                s=Settings.load()
            self.assertEqual(s.remote_key,'synthetic-env-value');self.assertEqual(s.local_model,'fixture-model')
            self.assertNotIn('synthetic-env-value',str(s.public()))
    def test_independent_factories_load_once_and_read_api_does_not_reload_support(self):
        with tempfile.TemporaryDirectory() as d:
            settings=Settings(data=Path(d))
            with patch('assistant.support_api.Settings.load',return_value=settings) as loader:
                app=support_app()
                with TestClient(app) as c:
                    for _ in range(3):self.assertEqual(c.get('/api/config').status_code,200)
                    self.assertEqual(loader.call_count,1)
                    # Creating a new Main factory must not mutate already-loaded Support settings.
                    with TestClient(main_app(settings)) as main:
                        self.assertEqual(main.get('/api/config').status_code,200)
                    self.assertEqual(loader.call_count,1)
                    self.assertIs(app.state.service.settings,settings)

class GroundingNegative(unittest.IsolatedAsyncioTestCase):
    async def test_related_reference_never_turns_wrong_credential_advice_into_quality_pass(self):
        with tempfile.TemporaryDirectory() as d:
            store=SupportStore(d,MODULE/'examples/support/users-tickets.json');rag=SupportRag(store)
            seen=[]
            class Wrong:
                async def completion(this,provider,messages,tools,partial):
                    seen.extend(messages)
                    return {'content':'Invented advice: put your credential in runtime JSON; this is the most frequent cause [F1] [T].'},{}
            service=SupportService(store,rag,Settings(data=Path(d)),Wrong())
            r=service.create('demo-alex','demo-auth',1,'A credential was already replaced, but the request still fails','local')
            await service.jobs[r['id']];actual=store.run('demo-alex',r['id'])
            self.assertEqual(actual['status'],'completed')
            self.assertEqual(actual['quality'],'NOT_ASSESSED')
            self.assertIn('Invented advice',actual['answer'])
            self.assertIn('DEEPSEEK_API_KEY',seen[1]['content'])
            self.assertEqual(store.ticket('demo-alex','demo-auth')['state'],'open')
            self.assertEqual(store.ticket('demo-alex','demo-auth')['deliveries'],[])
            # This deliberately wrong model stub is a negative semantic control,
            # never proof that an actual provider's full answer is acceptable.
