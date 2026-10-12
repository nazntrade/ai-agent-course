"""Explicit provider routing and streaming; never publish private reasoning."""
import asyncio
import json
import httpx

class Provider:
    def __init__(self,settings,runtime):
        self.settings=settings;self.runtime=runtime

    async def completion(self,kind,messages,tools,partial):
        s=self.settings
        if kind=='local':
            await self.runtime.ensure()
            base,model,key=s.local_url,s.local_model,s.local_key
        elif kind=='deepseek':
            if not s.remote_key:
                raise ValueError('DEEPSEEK_API_KEY is not configured')
            base,model,key=s.remote_url,s.remote_model,s.remote_key
        else:
            raise ValueError('Unknown provider')
        body={'model':model,'messages':messages,'max_tokens':s.max_tokens,'stream':True}
        if tools:
            body['tools']=tools
        if kind=='deepseek':
            body['thinking']={'type':'enabled' if s.thinking else 'disabled'}
            if s.thinking:
                body['reasoning_effort']='high'
            else:
                body['temperature']=s.temperature
        else:
            body.update(temperature=s.temperature,top_p=0.95,top_k=20,min_p=0,reasoning_budget=2048)
        headers={'Authorization':'Bearer '+key} if key else {}
        content='';reason='';calls={};finish=None;usage={};stream_done=False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(360,connect=15)) as client:
                async with client.stream('POST',base+'/chat/completions',json=body,headers=headers) as response:
                    if response.status_code!=200:
                        raise ValueError('Provider rejected request (HTTP '+str(response.status_code)+')')
                    async for line in response.aiter_lines():
                        if not line.startswith('data:'):
                            continue
                        value=line[5:].strip()
                        if value=='[DONE]':
                            stream_done=True
                            break
                        item=json.loads(value)
                        if item.get('usage'):
                            usage=item['usage']
                        for choice in item.get('choices',[]):
                            delta=choice.get('delta',{})
                            reason+=delta.get('reasoning_content') or ''
                            content+=delta.get('content') or ''
                            if delta.get('content'):
                                partial(s.scrub(content))
                            for t in delta.get('tool_calls',[]):
                                index=t.get('index',0);entry=calls.setdefault(index,{'id':'','type':'function','function':{'name':'','arguments':''}})
                                if t.get('id'):
                                    entry['id']=t['id']
                                f=t.get('function',{})
                                entry['function']['name']+=f.get('name') or ''
                                entry['function']['arguments']+=f.get('arguments') or ''
                            finish=choice.get('finish_reason') or finish
            if not stream_done or finish not in {'stop','tool_calls'}:
                if finish=='length':
                    raise ValueError('Provider output budget exhausted; answer is incomplete')
                raise ValueError('Provider returned an incomplete stream')
            if finish=='length':
                raise ValueError('Provider output budget exhausted; answer is incomplete')
            msg={'role':'assistant','content':content}
            if reason:
                msg['reasoning_content']=reason
            if calls:
                msg['tool_calls']=[calls[k] for k in sorted(calls)]
            if not content.strip() and not calls:
                raise ValueError('Provider returned no answer or tool call')
            return msg,usage
        except asyncio.CancelledError:
            raise
        except ValueError as e:
            if str(e).startswith(('Provider rejected','Provider output','Provider returned')):
                raise
            raise ValueError('Provider response could not be parsed') from None
        except Exception:
            raise ValueError('Selected provider is unavailable or timed out; no fallback was used') from None
