"""Build bundled Web help from the repository's Markdown manual."""
import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import re

import markdown

root = Path(__file__).resolve().parents[1]
class PlainText(HTMLParser):
    def __init__(self):super().__init__();self.parts=[]
    def handle_data(self,data):self.parts.append(data)

def build(check=False):
    sections=[];manual=['# 文思邮差操作手册\n']
    for path in sorted((root/'docs/guide').glob('*.md')):
        source=path.read_text(encoding='utf-8')
        title,body=source.split('\n',1)
        assert title.startswith('# '),path
        slug=re.sub(r'^\d+-','',path.stem)
        html=markdown.markdown(body,extensions=['fenced_code','tables'])
        plain=PlainText();plain.feed(html)
        sections.append({'id':slug,'title':title[2:],'html':html,'text':' '.join(plain.parts)})
        manual.append(source)
    assert sections
    files={'help-data.js':"'use strict';\nconst helpSections = "+json.dumps(sections,ensure_ascii=False,indent=2)+';\n',
           'user-guide.md':'\n\n'.join(manual)}
    for name,content in files.items():
        path=root/'posthub/static'/name
        if check:assert path.read_text(encoding='utf-8')==content,f'{name} is stale; run scripts/build_help.py'
        else:path.write_text(content,encoding='utf-8')
    print(f'{len(sections)} help chapters '+('checked.' if check else 'built.'))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');build(parser.parse_args().check)
