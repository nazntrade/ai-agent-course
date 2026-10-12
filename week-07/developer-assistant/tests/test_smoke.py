import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from assistant.api import create_app
from assistant.config import Settings

class Smoke(unittest.TestCase):
    def test_health_and_interface_without_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            with TestClient(create_app(Settings(data=Path(d)))) as c:
                self.assertEqual(c.get('/api/health').json()['application'],'developer-assistant')
                r=c.get('/')
                self.assertEqual(r.status_code,200)
                self.assertIn('Run /help',r.text)
                self.assertNotIn('remote_key',c.get('/api/config').text)
                self.assertFalse(c.get('/api/config').json()['deepseek_configured'])
