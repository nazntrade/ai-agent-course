"""Application-owned configuration; never expose credentials to UI or logs."""
from dataclasses import dataclass, field
from pathlib import Path
import json
import os
from dotenv import load_dotenv

MODULE = Path(__file__).resolve().parent.parent

@dataclass
class Settings:
    data: Path
    local_url: str = 'http://127.0.0.1:8792/v1'
    local_model: str = 'local-model'
    remote_url: str = 'https://api.deepseek.com/v1'
    remote_model: str = 'deepseek-flash'
    local_key: str = field(default='', repr=False)
    remote_key: str = field(default='', repr=False)
    tavily_key: str = field(default='', repr=False)
    telegram_token: str = field(default='', repr=False)
    telegram_chat: str = field(default='', repr=False)
    github_key: str = field(default='', repr=False)
    runtime: dict = field(default_factory=dict, repr=False)
    temperature: float = 0.6
    max_tokens: int = 8192
    context_chars: int = 18000
    thinking: bool = True

    @classmethod
    def load(cls):
        if os.getenv('ASSISTANT_SKIP_ENV') != '1':
            load_dotenv(MODULE / '.env', override=False)
        data = Path(os.getenv('ASSISTANT_DATA', str(MODULE / 'local-data')))
        data.mkdir(parents=True, exist_ok=True)
        raw = json.loads((data / 'settings.json').read_text(encoding='utf-8')) if (data / 'settings.json').exists() else {}
        allowed = {'local_url','local_model','remote_url','remote_model','runtime','temperature','max_tokens','context_chars','thinking'}
        s = cls(data=data, **{k:v for k,v in raw.items() if k in allowed})
        s.local_key = os.getenv('LOCAL_LLM_API_KEY','')
        s.remote_key = os.getenv('DEEPSEEK_API_KEY','')
        s.tavily_key = os.getenv('TAVILY_API_KEY','')
        s.telegram_token = os.getenv('TELEGRAM_BOT_TOKEN','')
        s.telegram_chat = os.getenv('TELEGRAM_CHAT_ID','')
        s.github_key = os.getenv('GITHUB_TOKEN','')
        s.validate()
        return s

    def validate(self):
        from urllib.parse import urlsplit
        local, remote = urlsplit(self.local_url), urlsplit(self.remote_url)
        if local.hostname not in {'localhost','127.0.0.1','::1'} or local.scheme not in {'http','https'}:
            raise ValueError('Local provider must use a loopback URL')
        if remote.scheme != 'https' or remote.hostname != 'api.deepseek.com':
            raise ValueError('DeepSeek must use its official HTTPS origin')
        if any(u.username or u.query or u.fragment for u in (local,remote)):
            raise ValueError('Provider URL must not contain credentials or parameters')
        if not 0 <= self.temperature <= 2 or not 512 <= self.max_tokens <= 16384 or not 1000 <= self.context_chars <= 40000:
            raise ValueError('Invalid provider budget')

    def scrub(self, text):
        text = str(text)
        for value in (self.local_key,self.remote_key,self.tavily_key,self.telegram_token,self.telegram_chat,self.github_key):
            if value:
                text = text.replace(value, '[redacted]')
        return text

    def public(self):
        return {'local_model':self.local_model, 'deepseek_model':self.remote_model,
                'local_key_configured':bool(self.local_key), 'deepseek_configured':bool(self.remote_key),
                'tavily_configured':bool(self.tavily_key),
                'telegram_configured':bool(self.telegram_token and self.telegram_chat),
                'github_authenticated':bool(self.github_key),
                'temperature':self.temperature,'max_tokens':self.max_tokens,
                'context_chars':self.context_chars,'thinking':self.thinking}
