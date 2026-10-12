"""Canonical, bounded project access shared by RAG, MCP and System Tools."""
from pathlib import Path
import re
import stat

EXCLUDED = {'.git','.venv','venv','node_modules','local-data','__pycache__','.pytest_cache','build','dist','.opencode','.bootstrap'}
SECRET = re.compile(r'(^\.env($|\.)|credential|secret|id_rsa|id_ed25519|\.pem$|\.key$|token)',re.I)
TEXT_EXT = {'.md','.txt','.json','.yaml','.yml','.py','.js','.css','.html','.toml'}

class ScopeError(ValueError):
    pass

class ProjectScope:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ScopeError('Project root must be a directory')

    def allowed(self, relative):
        p = Path(relative)
        if p.is_absolute() or not p.parts or any(x in {'..','.'} for x in p.parts):
            return False
        return not any(x.lower() in EXCLUDED or SECRET.search(x) for x in p.parts)

    def checked(self, relative):
        if not self.allowed(relative):
            raise ScopeError('Path is excluded or outside the project')
        path = self.root / relative
        for part in [path, *list(path.parents)[:len(Path(relative).parts)-1]]:
            if part.exists():
                info = part.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
                    raise ScopeError('Linked paths are not permitted')
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise ScopeError('Path escapes the project')
        return resolved

    def files(self, limit=500):
        result = []
        def walk(directory):
            for p in sorted(directory.iterdir(),key=lambda p:p.name.lower()):
                rel = p.relative_to(self.root).as_posix()
                if len(result) >= limit:
                    return
                if not self.allowed(rel):
                    continue
                try:
                    path = self.checked(rel)
                except (OSError,ScopeError):
                    continue
                if path.is_dir():
                    walk(path)
                elif path.is_file() and path.suffix.lower() in TEXT_EXT:
                    result.append(rel)
        walk(self.root)
        return result

    def mkdir(self, relative):
        if not self.allowed(relative):
            raise ScopeError('Directory is excluded')
        current=self.root
        for part in Path(relative).parts:
            current=current/part
            rel=current.relative_to(self.root).as_posix()
            if current.exists():
                existing=self.checked(rel)
                if not existing.is_dir():
                    raise ScopeError('Expected a directory')
            else:
                # Each existing ancestor was validated before any write.
                current.mkdir()
                self.checked(rel)
        return current

    def read(self, relative, start=1, count=160):
        if not 1 <= start <= 100000 or not 1 <= count <= 200:
            raise ScopeError('Invalid line range')
        p = self.checked(relative)
        if not p.is_file() or p.suffix.lower() not in TEXT_EXT or p.stat().st_size > 512000:
            raise ScopeError('Only bounded UTF-8 text files are supported')
        text = p.read_text(encoding='utf-8-sig')
        if '\x00' in text:
            raise ScopeError('Binary data is not permitted')
        lines = text.splitlines()
        selected = '\n'.join(lines[start-1:start-1+count])[:12000]
        return {'path':relative,'start_line':start,'end_line':min(len(lines),start+count-1),'text':selected,
                'truncated':start+count-1<len(lines) or len('\n'.join(lines[start-1:start-1+count]))>12000}

    def docs(self):
        return [p for p in self.files() if Path(p).suffix.lower() in {'.md','.txt','.json','.yaml','.yml'} and
                (p.lower().startswith('readme') or p.startswith('docs/') and not p.startswith(('docs/evidence/','docs/specs/')))]
