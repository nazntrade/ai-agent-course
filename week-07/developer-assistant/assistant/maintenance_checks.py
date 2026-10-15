"""Owner-fixed behavioral vectors and a closed, fuel-limited pure AST interpreter."""
import ast
import json
from pathlib import Path
from .maintenance import digest
from .config import MODULE

CHECKER_VERSION='client-oracle-wire-equivalence-v2'
PREVIOUS_CHECKER_SHA256='5c839601e46b0a990b9a35817df32669f291596390ac0d21145a26a6c598bbb2'

class Unsupported(ValueError):pass

class PureClient:
    """No Python evaluation, imports, object attributes or arbitrary callable execution."""
    def __init__(self,source):
        tree=ast.parse(source);self.functions={};self.fuel=20000
        for node in tree.body:
            if isinstance(node,ast.Expr) and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str):continue
            if not isinstance(node,ast.FunctionDef) or node.decorator_list:raise Unsupported('Only pure function definitions are permitted')
            if node.name not in {'build_resolution','parse_resolution','filter_tickets'}:raise Unsupported('Unknown client function')
            if node.args.vararg or node.args.kwarg or node.args.posonlyargs or node.args.kwonlyargs:raise Unsupported('Unsupported function arguments')
            self.functions[node.name]=node
    def tick(self):
        self.fuel-=1
        if self.fuel<0:raise Unsupported('Pure interpreter fuel exhausted')
    def call(self,name,*args,**kwargs):
        node=self.functions[name];names=[a.arg for a in node.args.args];defaults=[None]*(len(names)-len(node.args.defaults))+[self.expr(d,{}) for d in node.args.defaults]
        env=dict(zip(names,defaults))
        if len(args)>len(names) or set(kwargs)-set(names):raise Unsupported('Invalid call arguments')
        env.update(zip(names,args));env.update(kwargs)
        returned,value=self.block(node.body,env)
        return value if returned else None
    def block(self,nodes,env):
        for n in nodes:
            self.tick()
            if isinstance(n,ast.Return):return True,self.expr(n.value,env)
            if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name):env[n.targets[0].id]=self.expr(n.value,env)
            elif isinstance(n,ast.If):
                ret,value=self.block(n.body if self.expr(n.test,env) else n.orelse,env)
                if ret:return ret,value
            elif isinstance(n,ast.Raise):
                if not isinstance(n.exc,ast.Call) or not isinstance(n.exc.func,ast.Name) or n.exc.func.id!='ValueError':raise Unsupported('Only ValueError may be raised')
                raise ValueError(str(self.expr(n.exc.args[0],env)) if n.exc.args else 'Client error')
            elif isinstance(n,ast.Expr) and isinstance(n.value,ast.Constant):continue
            elif not isinstance(n,ast.Return):raise Unsupported('Unsupported pure statement: '+type(n).__name__)
        return False,None
    def expr(self,n,env):
        self.tick()
        if n is None:return None
        if isinstance(n,ast.Constant) and type(n.value) in {str,int,bool,type(None),float}:return n.value
        if isinstance(n,ast.Name):
            if n.id not in env:raise Unsupported('Unknown pure variable')
            return env[n.id]
        if isinstance(n,(ast.List,ast.Tuple)):return [self.expr(x,env) for x in n.elts] if isinstance(n,ast.List) else tuple(self.expr(x,env) for x in n.elts)
        if isinstance(n,ast.Dict):return {self.expr(k,env):self.expr(v,env) for k,v in zip(n.keys,n.values)}
        if isinstance(n,ast.Subscript):return self.expr(n.value,env)[self.expr(n.slice,env)]
        if isinstance(n,ast.JoinedStr):return ''.join(str(self.expr(x.value,env)) if isinstance(x,ast.FormattedValue) and not x.format_spec else str(self.expr(x,env)) for x in n.values)
        if isinstance(n,ast.BoolOp):
            if isinstance(n.op,ast.And):
                value=True
                for x in n.values:
                    value=self.expr(x,env)
                    if not value:return value
                return value
            value=False
            for x in n.values:
                value=self.expr(x,env)
                if value:return value
            return value
        if isinstance(n,ast.UnaryOp) and isinstance(n.op,ast.Not):return not self.expr(n.operand,env)
        if isinstance(n,ast.IfExp):return self.expr(n.body if self.expr(n.test,env) else n.orelse,env)
        if isinstance(n,ast.Compare):
            left=self.expr(n.left,env)
            for op,right_node in zip(n.ops,n.comparators):
                right=self.expr(right_node,env)
                if isinstance(op,ast.Eq):ok=left==right
                elif isinstance(op,ast.NotEq):ok=left!=right
                elif isinstance(op,ast.Is):ok=left is right
                elif isinstance(op,ast.IsNot):ok=left is not right
                elif isinstance(op,ast.In):ok=left in right
                elif isinstance(op,ast.NotIn):ok=left not in right
                elif isinstance(op,ast.GtE):ok=left>=right
                elif isinstance(op,ast.Lt):ok=left<right
                else:raise Unsupported('Unsupported comparison')
                if not ok:return False
                left=right
            return True
        if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute):
            owner=self.expr(n.func.value,env);args=[self.expr(x,env) for x in n.args]
            if n.keywords:raise Unsupported('Keyword method arguments unsupported')
            if type(owner)==dict and n.func.attr=='get' and 1<=len(args)<=2:return owner.get(*args)
            if type(owner)==str and n.func.attr=='strip' and not args:return owner.strip()
            if n.func.attr=='strip' and not args:raise TypeError('String normalization requires text')
            raise Unsupported('Method is outside the pure allowlist')
        if isinstance(n,ast.ListComp) and len(n.generators)==1:
            g=n.generators[0];values=self.expr(g.iter,env)
            if type(values) not in {list,tuple} or len(values)>100 or not isinstance(g.target,ast.Name) or g.is_async:raise Unsupported('Unbounded comprehension denied')
            out=[]
            for value in values:
                local=dict(env);local[g.target.id]=value
                if all(self.expr(x,local) for x in g.ifs):out.append(self.expr(n.elt,local))
            return out
        raise Unsupported('Unsupported pure expression: '+type(n).__name__)

def ticketdesk_checks(run):
    identity={'checker_version':CHECKER_VERSION,'checker_sha256':digest(Path(__file__).read_bytes()),'previous_checker_sha256':PREVIOUS_CHECKER_SHA256}
    fixture=MODULE/'examples'/'maintenance_demo'
    original_release=digest((fixture/'RELEASE_NOTES.md').read_bytes())
    baseline_release=run.m['inventory']['files'].get('RELEASE_NOTES.md',{}).get('sha256')
    identity['release_provenance']={'baseline_sha256':baseline_release,'original_fixture_sha256':original_release,'matches_original':baseline_release==original_release,'authority':'Optional provenance only; documentation truth independently NOT_ASSESSED'}
    locked=['ticket_api.py','tests/test_contract.py','PROJECT_GUIDE.md']
    for name in locked:
        trusted=fixture/name
        expected=digest(trusted.read_bytes()) if trusted.is_file() else None
        if expected is None or run.m['inventory']['files'].get(name,{}).get('sha256')!=expected:
            return identity|{'status':'NOT_ASSESSED','type':'trusted_behavior','limitations':['Selected project does not match immutable TicketDesk baseline.']}
        try:effective,_=run.blob(name)
        except (ValueError,OSError):
            return identity|{'status':'FAIL','type':'trusted_behavior','limitations':['Effective trusted contract bytes unavailable or integrity-conflicting: '+name]}
        if digest(effective)!=expected:
            return identity|{'status':'FAIL','type':'trusted_behavior','limitations':['Model changes to trusted contract source/tests/rules are not accepted.']}
    failures=[];count=0
    try:
        blob,_=run.blob('ticket_client.py');client=PureClient(blob.decode('utf-8'))
        signatures={'build_resolution':['ticket_id','note','version'],'parse_resolution':['response'],'filter_tickets':['tickets','status','assignee']}
        for name,args in signatures.items():
            if name not in client.functions or [arg.arg for arg in client.functions[name].args.args]!=args:failures.append('Stable public function signature: '+name)
        if failures:return identity|{'status':'FAIL','type':'trusted_behavior','failures':failures,'revision':run.m['revision']}
        for ticket_id,note,version in [('T-7','  completed safely  ',4),('T-9','keep  internal  spaces',9),('T-α',' δ text ',21)]:
            count+=1;request=client.call('build_resolution',ticket_id=ticket_id,note=note,version=version)
            expected={'method':'POST','path':f'/api/tickets/{ticket_id}/resolve','json':{'resolution':note,'expected_version':version}}
            trimmed=dict(expected,json={'resolution':note.strip(),'expected_version':version})
            if request not in (expected,trimmed) or type(request['json']['expected_version']) is not int:failures.append('resolve request method/route/body/content/displayed expected_version')
        for note in ('   ',None,7):
            count+=1
            try:
                request=client.call('build_resolution','T-7',note,4)
                if not isinstance(request,dict) or request.get('method')!='POST' or request.get('path')!='/api/tickets/T-7/resolve' or set(request.get('json',{}))!={'resolution','expected_version'} or request['json']['resolution'] not in ((note,note.strip()) if isinstance(note,str) else (note,)) or request['json']['expected_version']!=4:failures.append('Invalid input must fail or reach server unchanged, never synthetic resolution')
            except Unsupported:raise
            except (ValueError,TypeError):pass
        for body in ({'id':'T-7','status':'resolved','resolution':'done','version':5},{'id':'T-α','status':'resolved','resolution':'kept  text','version':22}):
            count+=1;response={'status_code':200,'body':dict(body)};value=client.call('parse_resolution',response)
            legacy={'id':body['id'],'state':body['status'],'note':body['resolution'],'version':body['version']}
            if value not in (body,legacy) or type(value['version']) is not int:failures.append('Consume new wire into coherent public response')
            if response!={'status_code':200,'body':body}:failures.append('Response input mutation')
        for code,error in ((409,'version_conflict'),(409,'already_resolved'),(400,'invalid_resolution'),(400,'invalid_expected_version'),(400,'invalid_body'),(404,'ticket_not_found'),(500,'unavailable')):
            count+=1
            try:client.call('parse_resolution',{'status_code':code,'body':{'error':error}});failures.append('Non-success must remain explicit: '+str(code)+' '+error)
            except Unsupported:raise
            except ValueError:pass
        rows=[{'id':'1','status':'open','assignee':'alex'},{'id':'2','status':'resolved','assignee':'alex'},{'id':'3','status':'open','assignee':'sam'}]
        unchanged=json.loads(json.dumps(rows))
        vectors=[({},rows),({'status':'open'},[rows[0],rows[2]]),({'assignee':'alex'},rows[:2]),({'status':'open','assignee':'alex'},[rows[0]]),({'status':'missing'},[])]
        for filters,wanted in vectors:
            count+=1
            if client.call('filter_tickets',rows,**filters)!=wanted:failures.append('unchanged list/filter behavior '+str(filters))
        if rows!=unchanged:failures.append('input mutation')
    except Unsupported as e:
        return identity|{'status':'NOT_ASSESSED','type':'trusted_behavior','limitations':[str(e)],'execution':'Closed pure AST interpreter; no Python execution'}
    except (ValueError,KeyError,TypeError,SyntaxError) as e:failures.append('Client behavioral exception: '+type(e).__name__)
    return identity|{'status':'FAIL' if failures else 'PASS','type':'trusted_behavior','failures':failures,'vectors':count,
            'checker_sha256':digest(Path(__file__).read_bytes()),'client_sha256':digest(blob),'revision':run.m['revision'],
            'execution':'Fixed immutable vectors in a closed fuel-limited AST interpreter; no project Python execution',
            'limitations':['Pure adapter subset only; no network integration test.','Documentation/release truth and goal/content quality remain independently NOT_ASSESSED.']}
