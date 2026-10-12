"""Real SDK v2 stdio server; only read-only project capabilities."""
import argparse
import subprocess
from mcp.server import MCPServer
from .scope import ProjectScope

def git_context(root):
    def git(*args):
        return subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=8)
    repo=git('rev-parse','--is-inside-work-tree')
    if repo.returncode or repo.stdout.strip()!='true':
        return {'state':'not_repository','branch':None}
    branch=git('symbolic-ref','--quiet','--short','HEAD')
    head=git('rev-parse','--verify','HEAD')
    if branch.returncode==0:
        return {'state':'branch' if head.returncode==0 else 'unborn','branch':branch.stdout.strip()}
    sha=git('rev-parse','--short','HEAD')
    return {'state':'detached','branch':None,'commit':sha.stdout.strip()}

def make_server(root):
    scope=ProjectScope(root)
    server=MCPServer('Developer Assistant Project',version='1.0.0')
    @server.tool()
    def git_branch() -> dict:
        """Get the actual current branch; distinguish detached/unborn/nonrepository."""
        return git_context(scope.root)
    @server.tool()
    def project_files() -> dict:
        """List bounded nonsecret project text files."""
        return {'files':scope.files(), 'limit':500}
    @server.tool()
    def git_status() -> dict:
        """Read Git status for permitted documentation paths only."""
        p=subprocess.run(['git','-C',str(scope.root),'status','--short','--','README.md','docs'],capture_output=True,text=True,timeout=8)
        return {'exit_code':p.returncode,'entries':[l for l in p.stdout.splitlines() if scope.allowed(l[3:].strip('"'))][:100]}
    return server

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True)
    args=parser.parse_args()
    make_server(args.root).run(transport='stdio')

if __name__=='__main__':
    main()
