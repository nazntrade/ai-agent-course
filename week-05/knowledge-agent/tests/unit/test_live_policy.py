"""LIVE authorization: block before model parsing, leases, HTTP, TEMP or inference."""
from __future__ import annotations
import pytest
from harness import live_policy, test_profile, live_chat, live_embed, rag_eval, acceptance

@pytest.mark.parametrize('value', ['forbidden', '', 'unknown'])
def test_forbidden_invalid_cli_zero_calls_even_with_stale_profile(monkeypatch,capsys,value):
    monkeypatch.setenv('AI_TEST_LIVE_POLICY',value)
    monkeypatch.setenv('RUN_CHAT_LIVE','1')
    monkeypatch.setenv('RUN_EMBED_LIVE','1')
    monkeypatch.setenv('AI_TEST_MODEL_KIND','invalid-stale-profile')
    def unexpected(*args,**kwargs):
        pytest.fail('blocked policy must make zero model/TEMP/HTTP/process calls')
    monkeypatch.setattr(test_profile,'load_test_profile',unexpected)
    monkeypatch.setattr(test_profile.subprocess,'Popen',unexpected)
    monkeypatch.setattr(live_chat,'load_settings',unexpected)
    monkeypatch.setattr(live_embed,'load_settings',unexpected)
    monkeypatch.setattr(live_chat.tempfile,'TemporaryDirectory',unexpected)
    monkeypatch.setattr(rag_eval,'request',unexpected)
    monkeypatch.setattr(rag_eval,'load_questions',unexpected)
    assert test_profile.main(['--','not-run'])==3
    assert live_chat.main()==3
    assert live_embed.main()==3
    assert rag_eval.main([])==3
    assert rag_eval.run('http://arbitrary-backend',None,live=False)==3
    assert rag_eval.run('http://arbitrary-backend',None,live=True)==3
    text=capsys.readouterr().out
    assert 'NOT_RUN' in text and 'PASS' not in text

@pytest.mark.parametrize('value', ['forbidden', 'bad'])
def test_session_direct_guard_precedes_profile_validation_and_lifecycle(monkeypatch,value):
    calls=[]
    monkeypatch.setattr(test_profile,'load_test_profile',lambda env:calls.append('parse'))
    with pytest.raises(live_policy.LivePolicyBlocked):
        test_profile.TestSession({'AI_TEST_LIVE_POLICY':value,'AI_TEST_MODEL_API_KEY':'stale-secret'},
                                 opener=lambda *a,**k:calls.append('network'))
    assert calls==[]

@pytest.mark.parametrize('kind',['local','remote','legacy'])
def test_allowed_or_legacy_owner_runs_selected_command_without_permission_question(monkeypatch,kind):
    for key in tuple(test_profile.os.environ):
        if key.startswith('AI_TEST_MODEL_') or key=='AI_TEST_LIVE_POLICY':
            monkeypatch.delenv(key)
    if kind!='legacy':
        monkeypatch.setenv('AI_TEST_LIVE_POLICY','allowed')
        profile={'KIND':kind,'BASE_URL':'http://127.0.0.1:8888/v1','NAME':'selected','API_KEY':'fixture'}
        if kind=='local':
            profile.update(ID='fixture',LEASE_URL='http://127.0.0.1:8888/api/local-models/fixture/test-leases',
                           LEASE_ID='borrowed',PARENT_READY='1')
        for key,value in profile.items(): monkeypatch.setenv('AI_TEST_MODEL_'+key,value)
    calls=[]
    class Child:
        def wait(self): return 0
    def spawn(command,env):
        calls.append((command,env))
        return Child()
    monkeypatch.setattr(test_profile.subprocess,'Popen',spawn)
    assert test_profile.main(['--','owned-test'])==0
    assert calls[0][0]==['owned-test']

@pytest.mark.parametrize('requested',[False,True])
def test_forbidden_acceptance_continues_offline_but_never_runs_live(monkeypatch,capsys,requested):
    monkeypatch.setenv('AI_TEST_LIVE_POLICY','forbidden')
    for key in ('RUN_CHAT_LIVE','RUN_EMBED_LIVE','RUN_KNOWLEDGE_READONLY_AUDIT'):
        monkeypatch.delenv(key,raising=False)
    if requested:
        monkeypatch.setenv('RUN_CHAT_LIVE','1')
        monkeypatch.setenv('RUN_EMBED_LIVE','1')
    calls=[]
    monkeypatch.setattr(acceptance,'run',lambda args:calls.append(args) or 0)
    assert acceptance.main()==(3 if requested else 0)
    assert len(calls)==4
    assert all(not any('live_' in item for item in args) for args in calls)
    output=capsys.readouterr().out
    assert 'CHAT_LIVE_STATUS: BLOCKED' in output
    assert 'EMBEDDING_LIVE_STATUS: BLOCKED' in output
    assert 'CHAT_LIVE_STATUS: PASS' not in output


def test_allowed_flag_does_not_select_unrequested_acceptance_tests(monkeypatch):
    monkeypatch.setenv('AI_TEST_LIVE_POLICY','allowed')
    for key in ('RUN_CHAT_LIVE','RUN_EMBED_LIVE','RUN_KNOWLEDGE_READONLY_AUDIT'):
        monkeypatch.delenv(key,raising=False)
    calls=[]
    monkeypatch.setattr(acceptance,'run',lambda args:calls.append(args) or 0)
    assert acceptance.main()==0
    assert len(calls)==4

@pytest.mark.parametrize('value',['allowed',None])
def test_policy_allowed_and_missing_preserve_legacy_live_selection(monkeypatch,value):
    if value is None: monkeypatch.delenv('AI_TEST_LIVE_POLICY',raising=False)
    else: monkeypatch.setenv('AI_TEST_LIVE_POLICY',value)
    monkeypatch.delenv('RUN_CHAT_LIVE',raising=False)
    monkeypatch.delenv('RUN_EMBED_LIVE',raising=False)
    assert live_chat.main()==3
    assert live_embed.main()==3
