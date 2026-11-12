"""Offline release checks. Does not claim GitHub CI or external account success."""
import argparse,json,re,sys,tomllib
from pathlib import Path
from urllib.parse import unquote,urlsplit
import yaml
from release_files import ROOT_FILES,selected_files

def anchors(text):
    found=set();counts={}
    for title in re.findall(r'^#{1,6}\s+(.+?)\s*#*$',text,re.M):
        title=re.sub(r'\[([^]]+)\]\([^)]*\)',r'\1',title).strip().lower()
        slug=re.sub(r'[^\w\-\s]','',title);slug=re.sub(r'\s','-',slug)
        n=counts.get(slug,0);counts[slug]=n+1;found.add(slug+(f'-{n}' if n else ''))
    return found

def check(root):
    errors=[];files=list(selected_files(root));selected={p.resolve() for p in files}
    def require(condition,message):
        if not condition:errors.append(message)
    for name in ROOT_FILES:require((root/name).is_file(),'Missing release file: '+name)
    for p in files:
        rel=p.relative_to(root).as_posix()
        require(p.suffix not in {'.sqlite3','.db','.pyc','.log','.pem'} and p.name not in {'config.json','.env'},'Private/generated file in export: '+rel)
        if p.suffix=='.sh':require(b'\r' not in p.read_bytes(),'Shell script must use LF line endings: '+rel)
        if p.suffix in {'.py','.js','.md','.html','.css','.yml','.yaml','.toml','.txt','.ps1','.sh'} or p.name=='LICENSE':
            try:text=p.read_text(encoding='utf-8')
            except UnicodeDecodeError:errors.append('Not UTF-8: '+rel);continue
            require(not re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]',text),'Unexpected control character: '+rel)
            # Detect plausible credentials, never print matched secret values.
            require(not re.search(r'(?<![A-Za-z0-9_])ph_[A-Za-z0-9_-]{40,100}',text),'Possible live API key: '+rel)
            require(('-----BEGIN '+'PRIVATE KEY-----') not in text,'Private key in export: '+rel)
        if p.suffix!='.md':continue
        text=p.read_text(encoding='utf-8');without_code=re.sub(r'```.*?```','',text,flags=re.S)
        for target in re.findall(r'!?\[[^\]\n]*\]\(([^)\n]+)\)',without_code):
            target=target.strip().strip('<>');url=urlsplit(target)
            if url.scheme or url.netloc:continue
            local=(p.parent/unquote(url.path)).resolve() if url.path else p.resolve()
            require(local in selected or local.is_dir() and local.is_relative_to(root.resolve()),f'Broken/export-excluded link in {rel}: {target}')
            if local.is_file() and url.fragment and local.suffix=='.md':
                require(unquote(url.fragment) in anchors(local.read_text(encoding='utf-8')),f'Unknown anchor in {rel}: {target}')
        for block in re.findall(r'```json\s*\n(.*?)```',text,re.S):
            try:json.loads(block)
            except json.JSONDecodeError:errors.append('Invalid JSON example: '+rel)
    metadata=tomllib.loads((root/'pyproject.toml').read_text(encoding='utf-8'))['project']
    version=re.search(r'__version__\s*=\s*["\']([^"\']+)',(root/'posthub/__init__.py').read_text(encoding='utf-8'))[1]
    require(version==metadata['version'],'Package version mismatch')
    require(metadata.get('readme')=='README.md','Package README missing')
    require(metadata.get('license')=={'file':'LICENSE'},'Package license metadata missing')
    require('MIT License' in (root/'LICENSE').read_text(encoding='utf-8'),'MIT license missing')
    forms=list((root/'.github/ISSUE_TEMPLATE').glob('*.yml'))
    for f in forms:
        form=yaml.safe_load(f.read_text(encoding='utf-8'))
        if f.name=='config.yml':
            require(isinstance(form.get('blank_issues_enabled'),bool),'Issue config must use boolean');continue
        require(bool(form.get('name') and form.get('description') and form.get('body')),'Incomplete issue form: '+f.name)
        ids=set()
        for item in form.get('body',[]):
            kind=item.get('type');attrs=item.get('attributes',{})
            require(kind in {'markdown','input','textarea','dropdown','checkboxes'},'Unsupported issue input: '+f.name)
            if kind=='markdown':require(bool(attrs.get('value')),'Empty markdown: '+f.name);continue
            field=item.get('id');require(bool(field) and field not in ids,'Missing/duplicate field id: '+f.name);ids.add(field)
            require(bool(attrs.get('label')),'Missing field label: '+f.name)
            if kind=='dropdown':
                options=attrs.get('options',[]);require(bool(options) and all(isinstance(v,str) for v in options) and len(options)==len(set(options)),'Invalid dropdown: '+f.name)
            if kind=='checkboxes':require(bool(attrs.get('options')) and all(isinstance(v.get('label'),str) for v in attrs['options']),'Invalid checkboxes: '+f.name)
    workflow=yaml.safe_load((root/'.github/workflows/test.yml').read_text(encoding='utf-8'))
    require('on' in workflow,'Quote workflow on key for portable YAML parsing')
    require(workflow.get('permissions')=={'contents':'read'},'CI permissions must be read-only')
    for name,job in workflow.get('jobs',{}).items():
        require(bool(job.get('runs-on') and job.get('steps') and job.get('timeout-minutes')),'Incomplete CI job: '+name)
        for step in job.get('steps',[]):require(('run' in step)^('uses' in step),'CI step requires run or uses: '+name)
    return {'status':'failed' if errors else 'passed','program_version':version,'source_files':len(files),'issue_forms':len(forms)-1,'checks':['local Markdown links and anchors','JSON examples','UTF-8 and control characters','export file scope','API/private-key patterns','MIT/package metadata','Issue form structure','CI structure and read permissions'],'limits':['offline checks are not GitHub server validation','pattern scan is not a full secret or security audit','does not run external-account tests'], 'errors':errors}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);parser.add_argument('--json',type=Path);args=parser.parse_args()
    result=check(args.root)
    if args.json:args.json.parent.mkdir(parents=True,exist_ok=True);args.json.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=True,indent=2));sys.exit(0 if result['status']=='passed' else 1)
