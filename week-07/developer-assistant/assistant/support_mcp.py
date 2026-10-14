"""Genuine scoped SDK stdio MCP; JSON is the only context data source."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
from mcp import Client, StdioServerParameters
from mcp.server import MCPServer
from .config import MODULE
from .tools import unpack

def read_snapshot(path,owner):
    p=Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_size>65536:raise ValueError('Snapshot unavailable or too large')
    blob=p.read_bytes();s=json.loads(blob.decode('utf-8'))
    if not isinstance(s,dict) or s.get('schema_version')!='support-context-v1' or s.get('synthetic') is not True:raise ValueError('Invalid support snapshot')
    if not isinstance(s.get('context_version'),int) or s['context_version']<1:raise ValueError('Invalid context version')
    if not isinstance(s.get('user'),dict) or s['user'].get('id')!=owner:raise ValueError('Snapshot user scope mismatch')
    if not isinstance(s.get('ticket'),dict) or s['ticket'].get('owner')!=owner or s['ticket'].get('context_version')!=s['context_version']:raise ValueError('Snapshot ticket scope mismatch')
    if not isinstance(s.get('history'),list) or len(s['history'])>8 or any(set(x)!={'role','text'} or x['role'] not in {'user','assistant'} or not isinstance(x['text'],str) for x in s['history']):raise ValueError('Invalid bounded history')
    return s,hashlib.sha256(blob).hexdigest()

def make_server(path,owner):
    server=MCPServer('Developer Assistant Support JSON',version='1.0.0')
    @server.tool()
    def get_user_context() -> dict:
        """Read the current synthetic user from the fixed scoped JSON snapshot."""
        s,h=read_snapshot(path,owner)
        return {'user':s['user'],'snapshot_id':s['snapshot_id'],'context_version':s['context_version'],'sha256':h,'synthetic':True}
    @server.tool()
    def get_ticket_context(ticket_id:str) -> dict:
        """Read only the ticket and bounded history in this JSON snapshot."""
        s,h=read_snapshot(path,owner)
        if ticket_id!=s['ticket']['id']:raise ValueError('Ticket unavailable in this MCP scope')
        return {k:s[k] for k in ('ticket','history','snapshot_id','context_version','synthetic','history_truncated','omitted_messages')}|{'sha256':h}
    return server

def minimal_env():
    allowed={'SYSTEMROOT','WINDIR','COMSPEC','PATH','PATHEXT','TEMP','TMP','USERPROFILE','APPDATA','LOCALAPPDATA','PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMDATA'}
    env={k:v for k,v in os.environ.items() if k.upper() in allowed}
    env.update(PYTHONUTF8='1',ASSISTANT_SKIP_ENV='1')
    return env

async def context_call(path,owner,ticket,expected):
    params=StdioServerParameters(command=sys.executable,args=['-m','assistant.support_mcp','--snapshot',str(path),'--owner',owner],cwd=str(MODULE),env=minimal_env())
    try:
        async with Client(params,read_timeout_seconds=15,raise_exceptions=True) as c:
            names=[t.name for t in (await c.list_tools()).tools]
            if set(names)!={'get_user_context','get_ticket_context'}:raise ValueError('Unexpected support MCP capabilities')
            user=unpack(await c.call_tool('get_user_context',{}))
            ticket_result=unpack(await c.call_tool('get_ticket_context',{'ticket_id':ticket}))
            for result in (user,ticket_result):
                if any(result.get(k)!=v for k,v in expected.items()):raise ValueError('MCP snapshot provenance mismatch')
            if user['user']['id']!=owner or ticket_result['ticket']['owner']!=owner or ticket_result['ticket']['id']!=ticket:raise ValueError('MCP scope mismatch')
            return {'transport':'stdio','protocol':'MCP SDK','discovered_tools':names,'calls':['get_user_context','get_ticket_context'],
                    'user':user['user'],**ticket_result}
    except asyncio.CancelledError:raise
    except Exception:raise ValueError('Support MCP failed; no direct JSON context fallback was used') from None

def main():
    p=argparse.ArgumentParser();p.add_argument('--snapshot',required=True);p.add_argument('--owner',required=True);a=p.parse_args()
    make_server(a.snapshot,a.owner).run(transport='stdio')

if __name__=='__main__':main()
