import pytest
from app.optimization import GenerationProfile,BASELINE,CANDIDATE
from app.context.builder import ContextBuilder
from app.errors import InvalidRequest
from app.providers.base import ChatResult
from app.rag.grounded_answer import generate_grounded

def test_profile_invalid_output_reservation():
    with pytest.raises(InvalidRequest):GenerationProfile('unsafe',0,2048,2048,'A real template').validate()

def test_prompt_scope_and_context_units():
    b=ContextBuilder(prompt_template=CANDIDATE.prompt_template)
    rag,_=b.build(question='Question?',fragments=[{'text':'A fact from survey'}],rag_enabled=True)
    plain,_=b.build(question='Question?',rag_enabled=False)
    assert rag[0].content==CANDIDATE.prompt_template
    assert plain[0].content!=CANDIDATE.prompt_template
    assert BASELINE.context_window!=BASELINE.max_context_chars

def test_options_survive_citation_correction():
    class Provider:
        def __init__(self):self.options=[]
        def chat(self,messages,options=None):
            self.options.append(options)
            return ChatResult(text='"invented words" [9]' if len(self.options)==1 else 'Survey discusses agent memory [1].',model='test',finish_reason='stop',usage=None,latency_ms=1,parameters={})
    p=Provider();result=generate_grounded(p,[],[{'text':'agent memory in the survey'}],options=CANDIDATE.options())
    assert p.options==[CANDIDATE.options(),CANDIDATE.options()]
    assert len(result.parameters['grounding_attempts'])==2
    assert result.parameters['semantic_quality']=='NOT_ASSESSED'


def test_literal_unrelated_citation_does_not_establish_semantic_pass():
    from app.rag.citations import CitationVerifier
    class Provider:
        def chat(self,messages,options=None):
            return ChatResult(text='The agent has 999 obligatory modules. Evidence: "agent memory" [1]',model='mock',finish_reason='stop',usage=None,latency_ms=1,parameters={})
    fragments=[{'text':'agent memory in the survey'}]
    result=generate_grounded(Provider(),[],fragments,options=CANDIDATE.options())
    assert CitationVerifier(fragments).verify(result.text).status=='verified'
    assert result.parameters['semantic_quality']=='NOT_ASSESSED'
