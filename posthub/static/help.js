'use strict';
let helpQuery='';
const helpDestinations={quickstart:['上游分发','apps'],upstream:['上游分发','apps'],channels:['下游渠道','channels'],delivery:['投递记录','records'],network:['网络通信','network'],troubleshooting:['告警中心','alerts']};
function helpArticle(section){
  const template=document.createElement('template');template.innerHTML=section.html;
  template.content.querySelectorAll('table').forEach(table=>{const wrap=document.createElement('div');wrap.className='help-table';table.replaceWith(wrap);wrap.append(table);});
  template.content.querySelectorAll('pre').forEach((pre,i)=>{pre.id=`help-code-${section.id}-${i}`;const button=document.createElement('button');button.className='text-button help-copy';button.type='button';button.dataset.action='copy-value';button.dataset.copy=pre.id;button.textContent='复制示例';button.setAttribute('aria-label','复制示例 '+(i+1));pre.before(button);});
  const destination=helpDestinations[section.id];
  return `<article class="help-article panel" aria-labelledby="help-article-title"><div class="help-article-heading"><h2 id="help-article-title">${esc(section.title)}</h2>${destination?`<a class="help-action" href="#${destination[1]}">前往${destination[0]} →</a>`:''}</div><div class="help-prose">${template.innerHTML}</div></article>`;
}
function helpContents(){
  const terms=helpQuery.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  if(terms.length){
    const matches=helpSections.filter(s=>terms.every(term=>(s.title+' '+s.text).toLocaleLowerCase().includes(term)));
    return `<div class="help-results"><p role="status" aria-live="polite">找到 ${matches.length} 篇相关说明</p>${matches.length?matches.map(s=>{const at=Math.max(0,s.text.toLocaleLowerCase().indexOf(terms[0])-30);return `<a class="help-result panel" href="#help/${s.id}" data-help-topic="${s.id}"><h2>${esc(s.title)}</h2><p>${esc(s.text.slice(at,at+180))}…</p><span>阅读说明 →</span></a>`;}).join(''):'<div class="panel"><p>没有找到匹配内容。可尝试“扫码”“密钥”“回执”或“代理”。</p><button class="outline" type="button" id="help-clear">清除搜索</button></div>'}</div>`;
  }
  const slug=location.hash.split('/')[1],section=helpSections.find(s=>s.id===slug)||helpSections[0];
  return `<aside class="help-nav"><nav aria-label="帮助目录">${helpSections.map((s,i)=>`<a href="#help/${s.id}" data-help-topic="${s.id}" ${s.id===section.id?'aria-current="page"':''}><span>${String(i+1).padStart(2,'0')}</span>${esc(s.title)}</a>`).join('')}</nav><small>手册随程序发布<br>网页内容与仓库文档同步</small></aside>${helpArticle(section)}`;
}
function helpView(){return heading('使用帮助','从接入到确认，按步骤完成消息分发')+`<div class="help-toolbar"><label for="help-search" class="sr-only">搜索操作文档</label><input id="help-search" type="search" placeholder="搜索：微信扫码、密钥、回执、代理…" value="${esc(helpQuery)}"><a class="help-action" href="/user-guide.md" download="文思邮差操作手册.md">下载操作手册</a></div><div id="help-layout" class="help-layout">${helpContents()}</div>`;}
document.addEventListener('input',event=>{if(event.target.id==='help-search'){helpQuery=event.target.value;$('#help-layout').innerHTML=helpContents();}});
document.addEventListener('click',event=>{
  const link=event.target.closest('[data-help-topic]');
  if(link){event.preventDefault();helpQuery='';const hash='#help/'+link.dataset.helpTopic;if(location.hash===hash)render();else location.hash=hash;return;}
  if(event.target.id==='help-clear'){helpQuery='';$('#help-search').value='';$('#help-layout').innerHTML=helpContents();$('#help-search').focus();}
});
