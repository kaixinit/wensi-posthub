'use strict';
const $ = (q, root=document) => root.querySelector(q);
const $$ = (q, root=document) => [...root.querySelectorAll(q)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = (name, cls='icon') => `<img class="${cls}" src="/icons/${name}.svg" alt="">`;
function newRequestId(){
  const rng=globalThis.crypto;
  if(typeof rng?.randomUUID==='function')return rng.randomUUID();
  // LAN HTTP is not a secure context, but getRandomValues is available there.
  if(typeof rng?.getRandomValues!=='function')return ''; // The required field can be filled manually.
  const bytes=rng.getRandomValues(new Uint8Array(16));
  bytes[6]=(bytes[6]&0x0f)|0x40;
  bytes[8]=(bytes[8]&0x3f)|0x80;
  const hex=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
const channels = {
  qq: {name:'QQ', icon:'qq', tag:'官方机器人', desc:'校验凭据并连接，再指定接收通知的个人或群。', button:'连接与绑定'},
  weixin: {name:'个人微信', icon:'wechat', tag:'ClawBot 扫码', desc:'扫码授权，再验证接收会话与收件人。', button:'扫码绑定'},
  wecom: {name:'企业微信', icon:'wecom', tag:'群机器人', desc:'通过官方 Webhook 连接需要通知的群。', button:'配置连接'},
  dingtalk: {name:'钉钉', icon:'dingtalk', tag:'应用机器人', desc:'授权应用，配置成员或群会话目标。', button:'配置应用'},
  feishu: {name:'飞书', icon:'feishu', tag:'群机器人', desc:'添加机器人，支持 Webhook 签名校验。', button:'添加机器人'},
  email: {name:'邮箱', icon:'mail', tag:'SMTP', desc:'授权发件账号，单独验证收件邮箱。', button:'配置邮箱'},
  webhook: {name:'自定义 HTTP', icon:'link', tag:'Webhook', desc:'接入可返回接收回执的 HTTP 服务。', button:'添加接口'},
  apprise: {name:'扩展渠道', icon:'app', tag:'Apprise', desc:'通过 Apprise 连接其他通知服务。', button:'添加扩展'}
};
const statuses = {waiting_context:['等待微信会话','warn'],awaiting_routing:['待配置分发','warn'],queued:['等待发送','blue'],sending:['正在发送','blue'],retry:['等待重试','warn'],accepted:['渠道已接受','good'],acknowledged:['收件人已确认','good'],failed:['发送失败','bad'],unknown:['结果待核实','warn'],blocked:['投递受阻','bad'],expired:['已过期',''],cancelled:['已取消',''],ack_overdue:['确认超时','warn']};
let state=null, csrf='', page='channels', channelTab='catalog', selected=new Set(), search='', channelFilter='', offset=0, toastTimer, currentTarget=null, appChannelFlow=null;
const badge=(text,color='')=>`<span class="badge ${color}">${esc(text)}</span>`;
const stateBadge=s=>badge(...(statuses[s]||[s,'']));
const date=t=>t ? new Date(t*1000).toLocaleString('zh-CN',{hour12:false}) : '—';
const verified=t=>!!(t.enabled&&t.verified);
const targetBadge=t=>!t.enabled?badge('已停用'):t.verified?badge('已验证','good'):t.kind==='qq'?badge(t.bound?'待收件验证':t.qq_connection?.state==='connected'?'已连接 · 待收件人':'待连接 / 收件人','warn'):t.kind==='weixin'&&!t.bound?badge('草稿 · 待扫码','warn'):badge('草稿 · 待验证','warn');
function hideToast(){clearTimeout(toastTimer);const n=$('#toast');if(!n)return;if(typeof n.hidePopover==='function'&&n.matches(':popover-open'))n.hidePopover();n.hidden=true;document.body.append(n);}
function toast(message,error=false){hideToast();const n=$('#toast');const host=$('#choice-dialog').open?$('#choice-dialog'):$('#dialog').open?$('#dialog'):document.body;host.append(n);n.textContent=message;n.className=error?'error':'';n.setAttribute('role',error?'alert':'status');n.hidden=false;if(typeof n.showPopover==='function')n.showPopover();else{n.classList.add('toast-fallback');n.scrollIntoView({block:'nearest'});}toastTimer=setTimeout(hideToast,6500);}

async function api(path,{method='GET',body,signal}={}){
  const headers={'X-PostHub-Request':'1'};
  if(csrf) headers['X-CSRF-Token']=csrf;
  if(body!==undefined) headers['Content-Type']='application/json';
  const response=await fetch('/api'+path,{method,headers,signal,body:body===undefined?undefined:JSON.stringify(body)});
  const data=await response.json();
  if(!response.ok){if(response.status===401 && path!=='/login') showLogin();throw new Error((data.detail||'请求失败')+(data.issues?'：'+data.issues.map(i=>i.field+' '+i.message).join('；'):''));}
  return data;
}
function showLogin(){clearSetupSecrets();closeDialog();$('#shell').hidden=true;$('#login').hidden=false;$('#login-error').textContent='';$('#login-form').reset();csrf='';}
async function refresh(renderPage=true){state=await api('/admin/state?offset='+offset);updateReceiptVerification();$('#worker-status').dataset.healthy=String(Date.now()/1000-state.heartbeat<180);$('#worker-status').textContent=Date.now()/1000-state.heartbeat<180?'分发服务运行中':'分发服务心跳异常';$('#clock').textContent=new Date().toLocaleDateString('zh-CN')+' · '+Intl.DateTimeFormat().resolvedOptions().timeZone;const n=state.alerts.filter(a=>!a.resolved).length;$('#alert-count').textContent=n||'';if(renderPage) render();}
function heading(title,sub,action=''){const topic={'下游渠道':'channels','上游分发':'upstream','网络通信':'network','账号设置':'account','系统分发':'delivery','投递记录':'delivery','告警中心':'troubleshooting','分发工作台':'quickstart'}[title];return `<div class="page-heading"><div><h1>${title}</h1><p>${sub}</p></div><div class="heading-actions">${topic?`<a class="help-link" href="#help/${topic}">使用说明</a>`:''}${action}</div></div>`;}
function empty(text,button=''){return `<div class="empty">${icon('records')}<p>${text}</p>${button}</div>`;}
function btn(label,action,extra='',cls='primary'){return `<button class="${cls}" data-action="${action}" ${extra}>${label}</button>`;}
function go(name){if(location.hash==='#'+name){page=name;render();}else location.hash=name;}
function render(){
  if(!state)return;
  page=(location.hash.slice(1)||(state.apps.length?'channels':'setup')).split('/')[0];
  if(!['setup','dashboard','channels','compose','records','alerts','apps','network','account','help'].includes(page))page='channels';
  if(state.account?.must_change_password&&!['account','help'].includes(page)){page='account';history.replaceState(null,'','#account');}
  $$('.sidebar nav a').forEach(a=>{a.classList.toggle('active',a.dataset.page===page);if(a.dataset.page===page)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
  const views={setup:setupView,dashboard:dashboardView,channels:channelsView,compose:composeView,records:recordsView,alerts:alertsView,apps:appsView,network:networkView,account:accountView,help:helpView};
  $('#content').innerHTML=views[page]();
  document.title=($('.sidebar nav a.active')?.textContent.trim()||'文思邮差')+' · 文思邮差';
}
function channelsView(){
  return `<div class="channels-page ${channelTab==='catalog'?'is-catalog':''}">`+heading('下游渠道','先连接渠道，再验证目标，最后勾选分发',btn('添加渠道','add-target'))+
    `<div class="channel-navigation"><div class="tabs"><button data-action="tab-catalog" class="${channelTab==='catalog'?'active':''}">可接入渠道</button><button data-action="tab-targets" class="${channelTab==='targets'?'active':''}">渠道管理<span class="count">${state.targets.length}</span></button></div>`+
    '<ol class="flow" aria-label="接入流程"><li><span>1</span>选择渠道</li><li><span>2</span>绑定与验证</li><li><span>3</span>下游配置</li></ol></div>'+
    (channelTab==='catalog'?catalogView():targetsView())+'</div>';
}
function catalogView(){
  const cards=Object.entries(channels).slice(0,6).map(([key,c])=>`<section class="channel-card ${key}"><div class="channel-title">${icon(c.icon)}<h2>${c.name}</h2></div><span class="tag">${c.tag}</span><p>${c.desc}</p>${btn(c.button,'add-target',`data-kind="${key}"`,'outline')}</section>`).join('');
  const recent=state.targets.slice(-5).reverse().map(t=>`<div class="recent-row">${icon(channels[t.kind].icon)}<div class="recent-target"><span class="target-name">${esc(t.name)}</span>${targetBadge(t)}</div>${btn(t.verified?'管理':'绑定','bind',`data-id="${t.id}"`,'text-button')}</div>`).join('');
  return `<div class="catalog-layout"><div><div class="catalog-grid">${cards}</div><div class="catalog-extra">${icon('link')}<h3>自定义 HTTP 渠道</h3><span class="muted">接入其他消息接口</span>${btn('添加接口','add-target','data-kind="webhook"','text-button')}</div><div class="catalog-extra">${icon('app')}<h3>更多通知渠道</h3><span class="muted">使用 Apprise 扩展连接</span>${btn('添加扩展','add-target','data-kind="apprise"','text-button')}</div></div><aside class="recent-panel"><h2>最近添加的渠道</h2>${recent||empty('还没有收件目标<br>从左侧选择一个渠道开始绑定。')}<div class="notice">已验证表示完成绑定与收件测试。每次消息的投递结果会单独跟踪。</div><div class="bottom-action"><small>仅已验证且启用的目标可加入分发方案</small>${btn('选择渠道并发送','tab-targets','','primary wide')}</div></aside></div>`;
}
function targetsView(){
  const targets=state.targets.filter(t=>t.name.toLowerCase().includes(search.toLowerCase())&&(!channelFilter||t.kind===channelFilter));
  return `<div class="toolbar"><input id="target-search" aria-label="搜索渠道名称" placeholder="搜索渠道名称" value="${esc(search)}"><select id="target-filter" aria-label="筛选渠道"><option value="">全部渠道</option>${Object.entries(channels).map(([k,c])=>`<option value="${k}" ${channelFilter===k?'selected':''}>${c.name}</option>`).join('')}</select>${btn('刷新','refresh','','text-button')}</div>`+
    (targets.length?`<div class="table-wrap"><table><thead><tr><th>选择</th><th>渠道名称</th><th>渠道类型</th><th>验证状态</th><th>操作</th></tr></thead><tbody>${targets.map(t=>`<tr><td><input type="checkbox" data-target="${t.id}" aria-label="选择 ${esc(t.name)}" ${selected.has(t.id)?'checked':''} ${verified(t)?'':'disabled'}></td><td class="name-cell">${esc(t.name)}<span class="muted">${esc(t.config.recipient||t.config.address||(t.verified?'收件验证通过':t.kind==='qq'||t.kind==='weixin'?'等待绑定收件目标':'等待收件验证'))}</span></td><td>${channels[t.kind].name}</td><td>${targetBadge(t)}</td><td>${btn('绑定 / 验证','bind',`data-id="${t.id}"`,'text-button')}${btn('编辑','edit-target',`data-id="${t.id}"`,'text-button')}${btn(t.enabled?'停用':'启用','toggle-target',`data-id="${t.id}"`,'text-button')}${!t.enabled||!t.verified?btn('删除','delete-target',`data-id="${t.id}"`,'text-button danger'):''}</td></tr>`).join('')}</tbody></table></div>`:empty('没有匹配的渠道',btn('添加渠道','add-target')))+
    `<div class="selection-bar"><b id="selected-count">已选择 ${[...selected].filter(id=>state.targets.some(t=>t.id===id&&verified(t))).length} 个目标</b><span class="muted">可同时选择不同渠道</span>${btn('用于系统分发','use-selected')}</div><small>待配置、未验证或已停用的目标暂不可选。</small>`;
}
function dashboardView(){const stats=state.stats;return heading('分发工作台','查看消息的接收、分发与确认情况',btn('新建分发','go-compose'))+`${stats.awaiting_routing?`<div class="notice warning">${stats.awaiting_routing} 条上游消息等待配置下游，尚未投递。${btn('查看待配置消息','go-records','','text-button')}</div>`:''}<div class="stats">${[['已接收消息',state.message_total,'持久化保存'],['等待处理',(stats.queued||0)+(stats.retry||0)+(stats.sending||0)+(stats.waiting_context||0),'包含预约与重试'],['渠道已接受',(stats.accepted||0)+(stats.acknowledged||0),'不等同于已读'],['待处理告警',state.alerts.filter(a=>!a.resolved).length,'需要关注与核实']].map(([n,v,s])=>`<div class="stat"><span>${n}</span><b>${v}</b><small>${s}</small></div>`).join('')}</div><div class="panel"><div class="page-heading"><h2>最近投递</h2>${btn('查看全部','go-records','','text-button')}</div>${messageTable(state.messages.slice(0,8))}</div><div class="panel"><h2>开始使用</h2><p>从创建上游接口到完成实际收件，接入向导会逐步检查配置和投递结果。</p>${btn('打开接入向导','go-setup')} ${btn('管理渠道','go-channels','','outline')} ${btn('上游分发','go-apps','','text-button')}</div>`;}
function targetOptions(ids=selected){return state.targets.length?state.targets.map(t=>`<label class="target-choice"><input type="checkbox" name="target_ids" value="${t.id}" ${ids.has(t.id)&&verified(t)?'checked':''} ${verified(t)?'':'disabled'}>${icon(channels[t.kind].icon)}<span>${esc(t.name)}<br><small>${channels[t.kind].name}</small></span>${targetBadge(t)}</label>`).join(''):empty('先添加并验证一个收件目标。',btn('前往下游渠道','go-channels'));}
function composeView(){return heading('系统分发','在平台内编辑消息，选择渠道后发送')+`<form id="compose-form"><div class="split"><section class="panel"><h2>消息内容</h2><label>消息标题<input name="title" maxlength="200" required placeholder="例如：服务器异常告警"></label><label>消息正文<textarea name="body" maxlength="8000" required placeholder="填写需要传递的信息…"></textarea></label><div class="field-grid"><label>发送时间<input type="datetime-local" name="scheduled_at"><small>留空立即发送，按当前浏览器时区填写。</small></label><label>投递截止时间<input type="datetime-local" name="expires_at"><small>留空为计划发送后 24 小时；到期停止待发送和重试。</small></label></div><label class="check-label"><input type="checkbox" name="require_ack">要求收件人点击链接确认收到</label><label>确认等待时间（分钟）<input name="ack_minutes" type="number" value="30" min="1" max="10080"></label><div class="notice">接收成功后异步分发。部分目标失败时，成功的目标不会重复发送；确认超时将产生告警。</div></section><section class="panel"><h2>选择收件目标</h2><label>从分发方案带入<select id="compose-profile"><option value="">手动勾选目标</option>${state.profiles.map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select></label><div class="target-options">${targetOptions()}</div><div class="actions">${btn('保存为分发方案','save-profile','','outline')}</div><hr><label>业务请求编号<input name="request_id" value="${newRequestId()}" required maxlength="128"><small>相同编号与相同内容只会创建一次消息。</small></label><button type="submit" class="primary wide">提交分发</button></section></div></form><section class="panel"><h2>已保存的分发方案</h2>${profileList()}</section>`;}
function messageTable(messages){if(!messages.length)return empty('暂无投递记录',btn('新建分发','go-compose'));return `<div class="table-wrap"><table><thead><tr><th>消息</th><th>创建时间</th><th>计划发送</th><th>分发状态</th><th></th></tr></thead><tbody>${messages.map(m=>`<tr><td class="name-cell">${esc(m.title)}<span class="muted">${m.purpose==='setup_test'?'平台接入试发':m.purpose==='verification'?'收件测试':m.purpose==='alert'?'系统告警':m.app_id==='admin'?'管理员提交':'应用提交'}</span></td><td>${date(m.created)}</td><td>${date(m.scheduled)}</td><td>${Object.entries(m.states).map(([s,n])=>stateBadge(s)+' '+n).join('<br>')}</td><td>${btn('详情','message',`data-id="${m.id}"`,'text-button')}</td></tr>`).join('')}</tbody></table></div>`;}
function recordsView(){return heading('投递记录','每个目标独立记录状态，保留每次尝试的结果',btn('刷新','refresh','','outline'))+messageTable(state.messages)+`<div class="pagination">${btn('上一页','previous',''+(offset===0?'disabled':''),'outline')}<span>第 ${Math.floor(offset/50)+1} 页 · 共 ${state.message_total} 条</span>${btn('下一页','next',offset+50>=state.message_total?'disabled':'','outline')}</div>`;}
function alertsView(){return heading('告警中心','处理发送失败、未知结果和收件确认超时',btn('刷新','refresh','','outline'))+`<div class="notice">建议在网络通信中指定独立的告警接收目标。告警目标自身失败时，平台保留站内告警，避免循环发送。</div>`+(state.alerts.length?`<div class="table-wrap"><table><thead><tr><th>问题</th><th>收件目标</th><th>发生时间</th><th>处理状态</th><th>操作</th></tr></thead><tbody>${state.alerts.map(a=>`<tr><td class="name-cell">${stateBadge(a.reason)}<span class="muted">${esc(a.detail)}</span></td><td>${esc(a.target_name)}</td><td>${date(a.created)}</td><td>${badge(a.resolved?'已处理':'待处理',a.resolved?'':'warn')}</td><td>${btn('查看原因与处理','message',`data-id="${a.message_id}"`,'text-button')}${a.resolved?'':btn('标记已处理','resolve',`data-id="${a.id}"`,'text-button')}</td></tr>`).join('')}</tbody></table></div>`:empty('暂无告警。运行中的投递异常会出现在这里。'));}
function profileReady(profile){return !!(profile?.target_ids.length&&profile.target_ids.every(id=>state.targets.some(t=>t.id===id&&verified(t))));}
function profileList(){return state.profiles.length?`<div class="table-wrap"><table><thead><tr><th>方案名称</th><th>收件目标</th><th>状态</th><th>操作</th></tr></thead><tbody>${state.profiles.map(p=>`<tr><td class="name-cell">${esc(p.name)}<span class="muted">${p.require_ack?'要求收件确认':'无需收件确认'}</span></td><td class="name-cell">${p.target_ids.map(id=>esc(state.targets.find(t=>t.id===id)?.name||'已删除目标')).join('、')}</td><td>${profileReady(p)?badge('可用','good'):badge('需重新配置','warn')}</td><td>${btn('编辑方案','edit-profile',`data-id="${p.id}"`,'text-button')}</td></tr>`).join('')}</tbody></table></div>`:`<p class="muted">可保存常用的手动发送渠道组合；不会关联上游。</p>`;}
function appRouteLabel(app){return app.route_error?badge('需核对下游','warn'):app.target_ids.length?[...new Set(state.targets.filter(t=>app.target_ids.includes(t.id)).map(t=>channels[t.kind].name))].join('、'):badge('待配置分发','warn');}
function appsView(){return heading('上游分发','统一接收上游消息，按各自配置分发',btn('接入向导','go-setup','','outline')+btn('创建上游接口','create-app'))+`
  <section class="panel integration-guide"><h2>上游程序 → 中转台 API → 下游渠道</h2><ol class="integration-steps"><li>创建接口并复制地址</li><li>上游推送并确认接收</li><li>绑定下游收件渠道</li><li>配置允许分发的目标</li></ol><div class="endpoint-row"><span class="method-label">POST</span><span>创建上游接口后自动生成完整推送地址，已包含接入密钥</span></div><small>上游只需填写专属地址并 POST JSON，无需另填鉴权请求头。已有接口可在“接入配置”重新生成地址；遗失则重置密钥。</small></section>
  <section class="panel"><h2>已接入上游</h2>${state.apps.length?`<div class="table-wrap"><table><thead><tr><th>上游名称</th><th>上游通信</th><th>下游分发</th><th>状态</th><th>操作</th></tr></thead><tbody>${state.apps.map(a=>`<tr><td class="name-cell">${esc(a.name)}</td><td class="name-cell">${a.received_count?badge('已收到推送','good'):badge('等待首次推送')}<span class="muted">${a.last_received?date(a.last_received):'上游填写专属地址后推送测试消息'}</span></td><td class="name-cell">${appRouteLabel(a)}<span class="muted">${a.target_ids.length} 个授权渠道${a.awaiting_count?' · '+a.awaiting_count+' 条待配置消息':''}</span></td><td>${badge(a.enabled?'已启用':'已停用',a.enabled?'good':'')}</td><td>${btn('接入配置','app-integration',`data-id="${a.id}"`,'text-button')}${a.awaiting_count?btn('待配置消息','go-records','','text-button'):''}${btn('下游配置','edit-app',`data-id="${a.id}"`,'text-button')}${btn(a.enabled?'停用密钥':'启用密钥','toggle-app',`data-id="${a.id}"`,'text-button')}${!a.enabled?btn('删除','delete-app',`data-id="${a.id}"`,'text-button danger'):''}</td></tr>`).join('')}</tbody></table></div>`:`<p class="muted">尚未创建上游接口。只填写上游名称，即可生成专属接入地址，下游渠道可稍后配置。</p>`}</section>
  <section class="panel"><h2>接入约定</h2><p>中转台提供 HTTP JSON API。上游主动推送；QQ、微信、邮箱的连接由中转台管理。服务器地址和监听端口要能被上游访问。</p><p>HTTP 202 表示已保存。响应 routing_state 为 awaiting_routing 表示等待配置下游，routed 表示已安排投递。使用返回的消息编号查询每个目标的实际状态；同一业务事件重试时保持 request_id 和内容不变。</p><a href="/api/admin/openapi" target="_blank" rel="noopener">查看 OpenAPI 接口定义</a></section>`;}
function networkView(){return networkPanel();}
function openDialog(title,body,drawer=false){stopEmailWatch();stopWeixinFlow();hideToast();const d=$('#dialog');delete d.dataset.bindingId;delete d.dataset.returnBindingId;d.className=drawer?'drawer':'';$('#dialog-body').innerHTML=`<div class="dialog-header"><h2 id="dialog-title">${esc(title)}</h2>${btn(icon('close'),'close','aria-label="关闭对话框"','icon-button')}</div><div class="dialog-content">${body}</div>`;if(!d.open)d.showModal();}
function closeDialog(){stopEmailWatch();stopWeixinFlow();hideToast();delete $('#dialog').dataset.bindingId;delete $('#dialog').dataset.returnBindingId;$('#dialog').close();$('#dialog-body').innerHTML='';currentTarget=null;appChannelFlow=null;}

function askChoice(title,body,choices){const d=$('#choice-dialog');if(d.open)return Promise.resolve('cancel');hideToast();d.innerHTML=`<div class="dialog-header"><h2 id="choice-title">${esc(title)}</h2></div><div class="dialog-content">${body}<div class="actions">${choices.map(([value,label,cls])=>`<button type="button" class="${cls}" data-choice="${value}">${label}</button>`).join('')}</div></div>`;return new Promise(resolve=>{const finish=value=>{hideToast();d.removeEventListener('click',click);d.removeEventListener('cancel',cancel);d.close();d.innerHTML='';resolve(value);};const click=e=>{const b=e.target.closest('[data-choice]');if(b)finish(b.dataset.choice);};const cancel=e=>{e.preventDefault();finish('cancel');};d.addEventListener('click',click);d.addEventListener('cancel',cancel);d.showModal();});}
async function requestCloseDialog(){
  const d=$('#dialog');
  if(appChannelFlow&&d.dataset.returnBindingId){bindTarget(d.dataset.returnBindingId);return false;}
  const t=state?.targets.find(t=>t.id===(d.dataset.bindingId||appChannelFlow?.targetId));
  if(t&&!t.verified){
    const choice=await askChoice(t.kind==='qq'?'收件目标尚未验证':'尚未完成绑定与验证',`<p><strong>${esc(t.name)}</strong> ${t.kind==='qq'?'机器人配置已保存，收件目标尚未验证，暂不能用于分发。':'当前是待绑定草稿，尚不能用于分发。'}</p><p>是否保留配置，以便下次继续？无用的草稿可以删除。</p>`,[['cancel','继续绑定','outline'],['delete','删除草稿','danger-button'],['keep','保留草稿并关闭','primary']]);
    if(choice==='cancel')return false;
    if(choice==='delete'){await deleteItem('targets',t.id);return false;}
  }
  if(appChannelFlow){returnToAppFromChannel();return false;}
  closeDialog();return true;
}
function accessBase(){return state?.settings.public_base_url||location.origin;}
function accessSource(){return `当前浏览器：${esc(location.origin)}。${state?.settings.public_base_url?'默认优先使用网络通信中的固定对外地址。':'默认沿用当前网址的协议、域名和端口。'}修改示例不会修改代理映射或服务监听端口。`;}
function showNewKey(result){rememberSetupKey(result);const app=state.apps.find(a=>a.id===result.id);const base=accessBase();openDialog('专属接入地址已生成',`<div class="notice">上游填写下面的完整地址即可，无需单独配置 Authorization 请求头。${app&&!app.enabled?'此应用仍为停用状态，配置后请启用。':''}</div><label>中转台访问地址<div class="copy-row"><input id="new-hook-base" type="url" value="${esc(base)}">${btn('使用当前网址','use-current-origin','data-field="new-hook-base"','outline')}</div><small>${accessSource()}</small></label><label>完整接入地址（已含密钥）<div class="copy-row"><input id="new-hook-url" readonly value="${esc(base+'/api/v1/hooks/'+result.api_key)}">${btn('复制完整地址','copy-value','data-copy="new-hook-url"','outline')}</div></label><p id="new-hook-error" class="error" role="alert"></p><p>请求方式：POST · 内容格式：application/json</p><pre>{
  "title": "文思量化通知",
  "body": "这里填写策略产生的消息",
  "request_id": "wensi-event-001"
}</pre><small>新事件使用新编号；省略 request_id 时自动生成，但上游重试可能重复发送。使用本上游独立配置的渠道。尚未配置下游时先保存为“待配置分发”，上游仍可完成接入测试。</small><div class="notice warning">完整地址包含发送凭据，请保存到上游配置。仅在本次页面内临时保留，刷新或退出后不再显示。不要公开分享；重置密钥后旧地址立即失效。</div><small>接入密钥（也可用于标准 API）</small><div id="new-api-key" class="secret-result">${esc(result.api_key)}</div><div class="actions">${btn('继续接入向导','setup-step',`data-id="${result.id}" data-step="2"`)}${btn('复制密钥','copy-key','','outline')}${btn('已保存，查看接入配置','app-integration',`data-id="${result.id}"`)}${btn('关闭','close','','outline')}</div>`);$('#dialog').classList.add('integration-dialog');}
function updateNewHook(){try{const u=new URL($('#new-hook-base').value.trim());if(!['http:','https:'].includes(u.protocol)||u.username||u.password||u.pathname!=='/'||u.search||u.hash)throw new Error();$('#new-hook-url').value=u.origin+'/api/v1/hooks/'+$('#new-api-key').textContent;$('#new-hook-error').textContent='';}catch{$('#new-hook-url').value='';$('#new-hook-error').textContent='请填写有效的 HTTP(S) 主机地址，不包含路径、账号或参数。';}}

function inputField(name,label,value='',type='text',required=true,extra=''){return `<label>${label}<input name="${name}" type="${type}" value="${esc(value)}" ${required?'required':''} ${extra}></label>`;}
function selectField(name,label,options,value){return `<label>${label}<select name="${name}">${options.map(([k,v])=>`<option value="${k}" ${k===value?'selected':''}>${v}</option>`).join('')}</select></label>`;}
function editTarget(kind='email',id=null){
  const target=state.targets.find(t=>t.id===id);currentTarget=id;
  if(target)kind=target.kind;
  const c=target?.config||{}, secrets=target?.configured_secrets||[];
  const field=(name,label,type='text',required=true)=>inputField(name,label,c[name]||'',type,required&&!(type==='password'&&secrets.includes(name)),type==='password'?`autocomplete="new-password" placeholder="${secrets.includes(name)?'已保存；留空保持不变':''}"`:'');
  let body='';
  if(kind==='email')body=emailForm(c,secrets,!!target);
  if(['wecom','feishu'].includes(kind))body=field('webhook_url','官方机器人 Webhook','password')+(kind==='feishu'?field('secret','签名密钥（如已开启签名）','password',false):'');
  if(kind==='dingtalk')body=field('app_key','应用 AppKey')+field('app_secret','应用 AppSecret','password')+field('robot_code','机器人 RobotCode')+selectField('target_type','收件类型',[['user','成员'],['group','群聊']],c.target_type||'user')+field('address','成员 UserID / 群 OpenConversationID')+'<div class="notice">需要在钉钉开放平台为应用开通相应的机器人发送权限。</div>';
  if(kind==='qq')body=field('app_id','机器人 AppID')+field('app_secret','机器人 AppSecret','password')+selectField('event_mode','机器人接入方式',[['websocket','主动连接（适合局域网）'],['webhook','公网回调（高级）']],c.event_mode||'websocket')+selectField('target_type','收件类型',[['user','个人'],['group','群聊']],c.target_type||'user')+'<div class="notice">先向 QQ 校验凭据，校验成功后保存并连接机器人；失败不会新增配置。主动连接无需填写公网回调地址。接入后，在 QQ 会话中指定收件人，再验证投递。</div><small>同一机器人统一收发连接方式。已有机器人沿用已配置连接方式，可在“网络通信”中统一调整。QQ 主动通知仍受平台权限和额度限制。</small>';
  if(kind==='weixin')body='<div class="stepper"><span>1 扫码绑定</span><span>2 测试发送</span><span>3 收件验证</span></div><div class="notice"><b>绑定方式：微信扫码</b><p>保存后直接显示二维码。用接收通知的本人微信扫码，再向 ClawBot 发一句消息，即可测试发送并填写收件验证码。</p></div>';
  if(kind==='webhook')body=field('url','接收端 HTTP(S) 地址','password')+field('bearer_token','Bearer Token（可选）','password',false)+'<div class="notice">接收端需要返回 2xx 和 JSON <code>{"accepted": true}</code>。平台会发送 delivery_id 作为幂等标识。</div>';
  if(kind==='apprise')body=field('url','Apprise 通知 URL','password')+'<div class="notice">填写一个 Apprise 通知连接，避免将多个收件人合并成一个目标。使用系统路由。<a href="https://appriseit.com/services/" target="_blank" rel="noopener">查看支持的通知服务</a></div>';
  if(!['email','apprise'].includes(kind)){
    if(kind==='qq'&&target){body+=`<div class="notice">机器人连接方式：${routeLabel(netData().routes[id])}。如需修改，请前往“网络通信”，将统一调整此机器人的收发连接。</div>`;}
    else{body+=selectField('network_mode','连接方式',[['default','跟随默认连接方式'],['system','直接连接（受系统 VPN / TUN 路由影响）'],['profile','使用代理（选择已保存配置）'],...(c.network_mode==='custom'?[['custom','保留原单独代理']]:[])],c.network_mode||'default')+`<label id="target-proxy-choice" ${c.network_mode!=='profile'?'hidden':''}>选择代理<select name="proxy_id" ${c.network_mode!=='profile'?'disabled':''}><option value="">请选择代理</option>${netData().proxies.map(p=>`<option value="${p.id}" ${c.proxy_id===p.id?'selected':''}>${esc(p.name)}</option>`).join('')}</select><small>可在“网络通信 → 连接方式 → 代理设置（可选）”中添加代理。</small></label>`;}
  }
  openDialog(kind==='email'?'邮箱渠道配置':kind==='weixin'?'微信渠道配置':target?'编辑渠道':kind==='qq'?'接入 QQ 机器人':'添加'+channels[kind].name+'渠道',`<form id="target-form" data-kind="${kind}" data-id="${id||''}">${inputField('name','渠道名称',target?.name||'', 'text',true,`maxlength="80" placeholder="${kind==='email'?'例如：运维值班邮箱':kind==='webhook'?'例如：监控回调接口':'例如：'+channels[kind].name+'运维通知'}"`)}${body}<div class="actions">${btn('取消','close','','outline')}<button class="primary" type="submit">${kind==='qq'?'校验并保存机器人':kind==='weixin'?(target?.bound?'保存配置':'保存并进入扫码'):'继续绑定'}</button></div></form>`,true);
  if(kind==='email')updateEmailForm();
}
function chooseChannel(){openDialog('选择渠道',`${appChannelFlow?'<p class="muted">选择渠道并完成绑定后，将返回上游配置。未保存的应用内容会保留。</p>':''}<div class="catalog-grid">${Object.entries(channels).map(([key,c])=>`<button class="channel-card" data-action="add-target" data-kind="${key}">${icon(c.icon)}<b>${c.name}</b><small>${c.tag}</small></button>`).join('')}</div>${appChannelFlow?`<div class="actions">${btn('返回上游配置','close','','outline')}</div>`:''}`);}
function bindTarget(id){const t=state.targets.find(t=>t.id===id);if(!t)return;currentTarget=id;const canVerify=!!(t.enabled&&(t.kind==='qq'?t.bound:t.kind==='weixin'?t.bound&&t.has_session:t.kind==='email'&&t.config.provider==='outlook'?t.email_authorized:true));
  let special='';
  if(t.kind==='qq')special=`<div class="stepper"><span class="active">1 接入机器人</span><span>2 选择收件会话</span><span>3 确认收件目标</span></div><h3>机器人连接</h3><p>机器人用于发送通知；接收通知的是下方选择的 QQ 用户或群。</p><p>机器人 AppID：${esc(t.config.app_id)}</p><div id="qq-connection-status" class="notice" role="status"></div><div class="actions">${btn('重新校验并连接','qq-connect',`data-id="${id}" ${t.enabled?'':'disabled'}`,'text-button')}</div><h3>选择收件会话</h3><p>${t.config.event_mode==='webhook'?'请先在 QQ 开放平台配置回调并订阅消息事件。':'在 QQ 开放平台点击“扫码聊天”，用接收通知的 QQ 打开机器人会话。'}发送一句便于识别的文字，例如“文思量化通知”；群内请 @机器人。下方自动列出最近 10 分钟的会话，由你选择。</p>${t.config.event_mode==='webhook'?qqCallbackNotice('/api/qq/'+t.config.app_id):''}<div id="qq-conversations" aria-live="polite"><p class="muted">正在读取最近会话…</p></div><div id="qq-recipient-status" class="notice" role="status" data-ready="${canVerify}"></div>${btn('刷新会话与状态','reload-binding',`data-id="${id}"`,'text-button')}<div id="qq-test-status" class="notice" role="status"></div>`;
  if(t.kind==='weixin')special=weixinBindingView(t);
  if(t.kind==='email')special=emailBindingView(t);
  openDialog((t.kind==='email'?'邮箱渠道配置 · ':t.kind==='qq'?'QQ 渠道配置 · ':t.kind==='weixin'?'微信渠道配置 · ':'绑定与验证 · ')+t.name,`<div id="verification-draft-notice" class="notice warning" ${t.verified?'hidden':''}>${['qq','weixin','email'].includes(t.kind)?'完成收件目标验证后，即可用于分发。':'待绑定草稿：完成收件验证后才可分发。'}退出时可保留或删除配置。</div>${special}<hr><section id="receipt-verification"></section><hr><small>目标 ID：<code>${id}</code></small><div class="actions">${btn('编辑连接配置','edit-target',`data-id="${id}"`,'text-button')}${!t.verified||!t.enabled?btn('删除目标','delete-target',`data-id="${id}"`,'text-button danger'):''}${btn('关闭','close','','outline')}</div>`,true);$('#dialog').dataset.bindingId=id;updateReceiptVerification();if(appChannelFlow)appChannelFlow.targetId=id;if(t.kind==='weixin')watchWeixinBinding(id);if(t.kind==='email')watchEmailBinding(id);if(t.kind==='qq'){updateQQBinding();loadQQConversations().catch(error=>toast(error.message,true));}
}


function updateReceiptVerification(force=false){
  const pane=$('#receipt-verification');
  const t=state?.targets.find(t=>t.id===$('#dialog').dataset.bindingId);
  if(!pane||!t)return;
  const signature=JSON.stringify([t.id,t.revision,!!t.verified,!!t.enabled,!!t.bound,!!t.has_session,!!t.email_authorized]);
  if(!force&&pane.dataset.signature===signature)return;
  if(pane.dataset.revision!==String(t.revision))delete pane.dataset.reverify;
  pane.dataset.signature=signature;pane.dataset.revision=String(t.revision);
  const notice=$('#verification-draft-notice');if(notice)notice.hidden=!!t.verified;
  const redo=pane.dataset.reverify==='true';
  if(t.verified&&!redo){
    pane.innerHTML=`<div class="verification-complete" role="status">${badge('收件目标已确认','good')}<h3>已完成验证</h3><p>渠道“${esc(t.name)}”已完成收件验证。${t.enabled?'可在上游应用的分发配置中勾选使用，无需重复发送验证码。':'当前已停用，启用后可用于分发。'}</p>${btn('重新验证收件目标','reverify-target',`data-id="${t.id}" ${t.enabled?'':'disabled'}`,'outline')}</div>`;
    return;
  }
  const ready=!!(t.enabled&&(t.kind==='qq'?t.bound:t.kind==='weixin'?t.bound&&t.has_session:t.kind==='email'&&t.config.provider==='outlook'?t.email_authorized:true));
  pane.innerHTML=`<h3>${t.verified?'重新验证收件目标':'确认收件目标'}</h3><p>${t.verified?'发送新的验证码，核对当前收件目标。只有更换收件会话或连接配置时，原验证才会失效。':'向此收件目标发送验证码，确认实际收到后填入。通过验证后才可用于分发。'}</p>${btn('发送收件验证码','verify-start',`data-id="${t.id}" ${ready?'':'disabled'}`,'primary wide')}<div id="verify-result" class="inline-result"></div><form id="verify-form" data-id="${t.id}">${inputField('code','收件验证码','','text',true,'inputmode="numeric" pattern="[0-9]{6}" minlength="6" maxlength="6" autocomplete="one-time-code" placeholder="请输入6位数字验证码" title="请输入6位数字收件验证码"')}<button type="submit" class="outline wide" ${ready?'':'disabled'}>完成验证</button></form>${t.verified?`<div class="actions">${btn('返回验证结果','cancel-reverify-target',`data-id="${t.id}"`,'text-button')}</div>`:''}`;
}

function updateQQBinding(){
  const box=$('#qq-connection-status'),recipient=$('#qq-recipient-status');
  const t=state.targets.find(t=>t.id===$('#dialog').dataset.bindingId);
  if(!box||!recipient||!t)return;
  const c=t.qq_connection||{state:'pending',detail:'等待连接机器人'};
  const labels={connected:'机器人已连接',connecting:'正在连接机器人',pending:'等待连接',reconnecting:'正在重连',error:'连接失败',webhook:'公网回调模式',stopped:'连接已停止'};
  box.textContent=(t.enabled?(labels[c.state]||'等待连接')+' · '+c.detail:'目标已停用');
  box.className='notice'+(['error','reconnecting'].includes(c.state)?' warning':'');
  recipient.textContent=t.verified?'收件目标已确认：'+t.name+'。可在上游应用的分发配置中勾选使用。':t.bound?'已选择收件会话：'+t.config.address+'。测试消息会带上渠道名称和验证码，方便区分。':'尚未选择收件会话。请先在 QQ 发一句话，再从上方列表确认收件人。';
  const test=$('#qq-test-status'),r=t.qq_test;
  if(test){test.hidden=!r;test.textContent=r?(r.state==='accepted'?(t.verified&&$('#receipt-verification')?.dataset.reverify!=='true'?'发送测试通过 · QQ 已返回成功回执，收件目标已确认。':r.code_active?'发送测试通过 · QQ 已返回成功回执。请查看 QQ 中的渠道名称并填回验证码，完成收件目标确认。':'QQ 曾返回成功回执；验证码已失效，请重新发送收件验证码。'):['queued','sending','retry'].includes(r.state)?'测试消息正在发送，等待 QQ 回执…':'发送测试未通过：'+(r.last_error||statuses[r.state]?.[0]||r.state)):'';test.className='notice'+(r&&!['accepted','queued','sending','retry'].includes(r.state)?' warning':'');}
  updateReceiptVerification();
}
function qqCallbackNotice(path){
  try{
    const u=new URL(accessBase()),h=u.hostname,parts=h.split('.').map(Number);
    const local=h==='localhost'||h.endsWith('.localhost')||h.endsWith('.local')||h.includes(':')||h.startsWith('127.')||h.startsWith('10.')||h.startsWith('192.168.')||h.startsWith('169.254.')||h==='0.0.0.0'||(parts[0]===172&&parts[1]>=16&&parts[1]<=31);
    if(u.protocol!=='https:'||local||!['','80','443','8080','8443'].includes(u.port))throw new Error();
    return `<p>QQ 官方回调地址（请确保腾讯服务器可访问）：</p><div class="secret-result">${esc(u.origin+path)}</div>`;
  }catch{return '<div class="notice warning">当前访问地址不能作为 QQ 公网回调。请在网络通信中设置公网 HTTPS 地址及 QQ 允许的端口（80、443、8080、8443），或编辑连接配置，改用“主动连接”。</div>';}
}
async function loadQQConversations(){
  const id=$('#dialog').dataset.bindingId;
  if(!id||!$('#qq-conversations'))return;
  const result=await api('/admin/targets/'+id+'/qq-conversations');
  const box=$('#qq-conversations'),t=state.targets.find(t=>t.id===id);
  if(!box||$('#dialog').dataset.bindingId!==id||!t)return;
  const signature=JSON.stringify([result,t.verified,t.enabled]);if(box.dataset.signature===signature)return;box.dataset.signature=signature;
  box.innerHTML=result.conversations.length?result.conversations.map(c=>`<div class="qq-conversation"><div><strong>${esc(c.label)}</strong><small>${date(c.last_seen)}</small><p>${esc(c.preview||'（无文字内容）')}</p></div>${c.current&&t.verified?badge('当前收件目标','good'):btn('确认并发送验证码','qq-select',`data-id="${id}" data-conversation="${c.id}" data-current="${c.current}" ${t.enabled?'':'disabled'}`,'outline')}</div>`).join(''):'<p class="muted">暂未收到最近会话。请在 QQ 发一句话；已有收件目标保持不变。</p>';
}

async function showMessage(id){const m=await api('/admin/messages/'+id);openDialog(m.title,`<div class="detail-summary"><span>创建：${date(m.created)}</span><span>计划发送：${date(m.scheduled)}</span><span>投递截止：${date(m.expires)}</span></div><p class="muted message-expiry-note">仅限制本条消息的投递时间，不是密钥有效期。到期停止待发送和重试；已发内容与记录保留。</p><p class="notice">${esc(m.body).replace(/\n/g,'<br>')}</p><small>消息编号：${m.id}</small>${m.routing_state==='awaiting_routing'?`<div class="notice warning">消息已接收保存，上游通信已验证。尚未安排下游投递。请先在上游分发中配置目标，再确认分发本条消息。</div><div class="actions">${btn('下游配置','edit-app',`data-id="${m.app_id}"`,'outline')}${btn('按当前配置分发','route-message',`data-id="${id}"`,'primary')}</div>`:!m.deliveries.length?`<p>${stateBadge(m.routing_state)}</p>`:''}${m.deliveries.map(d=>`<section class="delivery-card"><div class="delivery-head"><b>${esc(d.target_name)}</b>${stateBadge(d.waiting_for_context?'waiting_context':d.state)}</div><small>${channels[d.kind].name} · 已尝试 ${d.attempts} 次</small>${d.last_error?`<p class="${d.waiting_for_context?'muted':'error'}">${esc(d.last_error)}</p>`:''}${deliveryAdvice(d,m)}${d.acked_at?`<p class="success">收件确认：${date(d.acked_at)}</p>`:''}<div class="history">${d.history.map(h=>`<div>${date(h.at)} · ${statuses[h.state]?.[0]||esc(h.state)} ${esc(h.detail)}</div>`).join('')||'等待首次发送'}</div>${['accepted','unknown'].includes(d.state)?btn(d.state==='accepted'?'我已收到，登记确认':'核实已收到','confirm-received',`data-id="${d.id}" data-message="${id}"`,'primary'):''} ${!d.waiting_for_context&&['failed','blocked','unknown'].includes(d.state)?btn('重新发送此目标','retry',`data-id="${d.id}" data-message="${id}" data-state="${d.state}"`,'outline'):''}</section>`).join('')}<div class="actions">${btn('刷新状态','message',`data-id="${id}"`,'outline')}${m.routing_state==='awaiting_routing'||m.deliveries.some(d=>d.waiting_for_context||['queued','retry'].includes(d.state))?btn('取消待发送任务','cancel-message',`data-id="${id}"`,'text-button danger'):''}${btn('关闭','close','','primary')}</div>`);if(appChannelFlow?.targetId)$('#dialog').dataset.returnBindingId=appChannelFlow.targetId;}
function updateAppDownstreamSummary(){
  const form=$('#app-form');if(!form)return;
  const ids=new Set($$('input[name=target_ids]:checked',form).map(input=>input.value));
  const kinds=[...new Set(state.targets.filter(t=>ids.has(t.id)).map(t=>channels[t.kind].name))];
  $('#app-downstream-summary').textContent=ids.size?`已选 ${ids.size} 个目标 · ${kinds.join('、')}`:'暂未添加 · 可稍后配置';
  const note=$('#app-channel-feedback');
  const added=state.targets.find(t=>t.id===note?.dataset.targetId);
  if(added){
    note.hidden=!ids.has(added.id);
    note.textContent=`“${added.name}”已验证并勾选，保存上游配置后生效。`;
  }
}
function addChannelFromApp(){
  const form=$('#app-form');if(!form)return;
  // Keep the form nodes while the existing channel wizard owns the dialog.
  // This retains unsaved names, selected targets and the chosen default profile.
  appChannelFlow={form,nodes:[...$('#dialog-body').childNodes],className:$('#dialog').className,targetId:null};
  chooseChannel();
}
function returnToAppFromChannel(targetId=null){
  const flow=appChannelFlow;if(!flow)return;
  const ids=new Set($$('input[name=target_ids]:checked',flow.form).map(input=>input.value));
  const target=state.targets.find(t=>t.id===targetId&&verified(t));
  if(target)ids.add(target.id);
  hideToast();appChannelFlow=null;currentTarget=null;
  const d=$('#dialog');delete d.dataset.bindingId;delete d.dataset.returnBindingId;d.className=flow.className;
  $('#dialog-body').replaceChildren(...flow.nodes);
  $('#app-downstream-options .target-options').innerHTML=state.targets.length?targetOptions(ids):'<p class="muted">暂无收件目标，请点击“添加渠道”新增并绑定。</p>';
  if(target){
    $('#app-downstream').open=true;
    $('#app-channel-feedback').dataset.targetId=target.id;
  }
  updateAppDownstreamSummary();
  const addButton=$('[data-action=app-add-channel]',flow.form);
  addButton.disabled=false;addButton.focus({preventScroll:true});
}
function createApp(id=null){
  const app=state.apps.find(a=>a.id===id);
  openDialog(app?'下游配置 · '+app.name:'创建上游接口',`<form id="app-form" data-id="${id||''}">
    ${inputField('name','上游名称',app?.name||'','text',true,'maxlength="80" placeholder="例如：文思量化"')}
    <p class="muted app-key-hint">密钥无自动到期时间，可随时停用或重置。</p>
    <div class="notice">${app?'配置下游不会更换上游地址和密钥。保存后新消息自动分发；此前待配置消息请在投递记录确认后分发。':'只填写上游名称即可生成专属接入地址。可以先与上游建立通信，再回来配置下游。'}</div>
    ${app?.route_error?`<div class="notice warning">${esc(app.route_error)}</div>`:''}
    <div id="app-channel-feedback" class="notice" hidden></div>
    <section class="app-downstream-panel">
      <div class="app-downstream-header"><span class="downstream-summary-copy"><b>下游分发</b><small id="app-downstream-summary"></small></span>${btn('＋ 添加渠道','app-add-channel','type="button"','outline')}</div>
      <details id="app-downstream" class="app-downstream">
      <summary aria-controls="app-downstream-options"><span>选择已有目标</span><span class="downstream-toggle"><span class="downstream-open">展开选项</span><span class="downstream-close">收起选项</span></span></summary>
      <div id="app-downstream-options" class="app-downstream-body">
        <h3>允许发送到的目标</h3><p class="muted">渠道勾选仅对当前上游生效，不会改动其他上游的授权。勾选已绑定并验证的目标，可同时选择不同渠道。尚无目标时，消息保留为“待配置分发”，直至投递截止。</p>
        <div class="target-options">${state.targets.length?targetOptions(new Set(app?.target_ids||[])):'<p class="muted">暂无收件目标，请点击“添加渠道”新增并绑定。</p>'}</div>
      </div>
      </details>

    </section>
    <details class="app-receipt-settings" ${app?.require_ack?'open':''}><summary>收件确认设置（仅当前上游）</summary>
      <label class="check-label"><input type="checkbox" name="require_ack" ${app?.require_ack?'checked':''}>要求收件人点击链接确认收到</label>
      ${inputField('ack_minutes','确认等待时间（分钟）',app?.ack_minutes||30,'number',true,'min="1" max="10080"')}
      <small>仅影响当前上游后续消息。确认超时产生告警；启用前需在网络通信中填写收件人可访问的平台地址。</small>
    </details>
    <div class="actions"><button type="submit" class="primary wide">${app?'保存上游配置':'创建接口并生成地址'}</button></div>
  </form>`);
  updateAppDownstreamSummary();
}

function editProfile(id=null){const profile=state.profiles.find(p=>p.id===id);openDialog(profile?'编辑分发方案':'新建分发方案',`<form id="route-form" data-id="${id||''}">${inputField('name','方案名称',profile?.name||'','text',true,'maxlength="80" placeholder="例如：交易告警通知"')}<h3>收件目标（可多选）</h3>${profile&&!profileReady(profile)?'<div class="notice warning">原手动方案包含已删除、停用或未验证的渠道，请重新勾选。此方案仅供系统分发使用。</div>':''}<div class="target-options">${targetOptions(new Set(profile?.target_ids||[]))}</div><label class="check-label"><input type="checkbox" name="require_ack" ${profile?.require_ack?'checked':''}>要求收件确认</label>${inputField('ack_minutes','确认等待时间（分钟）',profile?.ack_minutes||30,'number',true,'min="1" max="10080"')}<div class="notice">方案仅供系统分发手动发送时使用，不关联任何上游。修改方案不会改变上游的下游配置。</div><div class="actions"><button type="submit" class="primary">保存分发方案</button></div></form>`);}
async function deleteItem(table,id){const info=await api('/admin/'+table+'/'+id+'/deletion');const kind=table==='targets'?'收件目标':'上游应用';let detail=table==='targets'?'连接凭据、验证码和绑定信息会清除；该目标会从上游授权中移除。':'接入密钥会永久失效，授权配置会清除。';if(info.profiles?.length)detail+=` 以下分发方案将需要重新配置：${info.profiles.map(esc).join('、')}。`;if(info.alert_target)detail+=' 此目标也是告警接收渠道，删除后改为仅保留站内告警。';openDialog('删除'+kind,`<p>确认删除 <strong>${esc(info.name)}</strong>？</p><div class="notice warning">${detail}</div><p>将取消 <strong>${info.pending}</strong> 个尚未发送的任务${info.awaiting_messages?'，以及 '+info.awaiting_messages+' 条待配置消息':''}。历史投递记录保留，已发送的消息无法撤回。删除后不能直接恢复此连接。</p>${info.sending?'<p class="error">仍有正在发送的任务，请等待结果后再操作。</p>':''}<div class="actions">${btn('取消','close','','outline')}${btn('确认删除','delete-confirmed',`data-table="${table}" data-id="${id}" ${(info.enabled&&!(table==='targets'&&!info.verified))||info.sending?'disabled':''}`,'danger-button')}</div>`);if(appChannelFlow&&table==='targets')$('#dialog').dataset.returnBindingId=id;}
async function copyText(text){if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(text);return;}const input=document.createElement('textarea');input.className='clipboard-buffer';input.value=text;const focus=document.activeElement;($('#dialog')?.open?$('#dialog'):document.body).append(input);try{input.select();if(!document.execCommand('copy'))throw new Error('浏览器未允许自动复制，请手动选择并复制内容');}finally{input.value='';input.remove();focus?.focus();}}
function integrationExample(app,base,mode){const payload={title:'上游测试通知',body:'这里填写上游程序产生的消息',request_id:'your-event-id-001'};if(mode==='targets')payload.target_ids=app.target_ids;const json=JSON.stringify(payload,null,2);const pyPayload=JSON.stringify(payload,null,4).replace('"your-event-id-001"','str(uuid.uuid4())');const python=`import json
import os
import uuid
from urllib.request import Request, urlopen

base = os.environ.get("POSTHUB_BASE_URL", ${JSON.stringify(base)}).rstrip("/")
headers = {
    "Authorization": "Bearer " + os.environ["POSTHUB_API_KEY"],
    "Content-Type": "application/json",
}
# 同一业务事件重试时，请复用 payload 和 request_id。
payload = ${pyPayload}
request = Request(base + "/api/v1/messages",
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers=headers, method="POST")
with urlopen(request, timeout=10) as response:
    ticket = json.load(response)
print(ticket)

# 查询异步投递状态；刚提交时可能仍在排队。
query = Request(base + "/api/v1/messages/" + ticket["id"], headers=headers)
with urlopen(query, timeout=10) as response:
    print(json.load(response))`;return {json,python};}
function renderIntegrationExample(){const root=$('#integration-content');if(!root)return;const app=state.apps.find(a=>a.id===root.dataset.id);const raw=$('#integration-base').value.trim();let base;try{const url=new URL(raw);if(!['http:','https:'].includes(url.protocol)||url.username||url.password||url.pathname!=='/'||url.search||url.hash)throw new Error();base=url.origin;}catch{$('#integration-error').textContent='请填写完整的 HTTP(S) 主机地址和端口，不包含路径、账号或参数。';for(const id of ['integration-endpoint','integration-json','integration-python']){const e=$('#'+id);if('value' in e)e.value='';else e.textContent='';}$('#saved-hook-url').value='';return;}$('#integration-error').textContent='';const mode=$('#integration-mode').value;const sample=integrationExample(app,base,mode);$('#integration-endpoint').value=base+'/api/v1/messages';$('#integration-json').textContent=sample.json;$('#integration-python').textContent=sample.python;}
function showIntegration(id){const app=state.apps.find(a=>a.id===id);if(!app)return;openDialog('上游接入配置 · '+app.name,`<div id="integration-content" data-id="${id}">${!app.enabled?'<div class="notice warning">此应用已停用。上游配置完成后，请回到上游列表启用密钥。</div>':''}${app.route_error?`<div class="notice warning">${esc(app.route_error)}</div>`:''}<label>中转台访问地址<div class="copy-row"><input type="url" id="integration-base" value="${esc(accessBase())}">${btn('使用当前网址','use-current-origin','data-field="integration-base"','outline')}</div><small>${accessSource()}</small></label><p id="integration-error" class="error" role="alert"></p><h3>专属推送地址（上游只填这一条）</h3><p>创建上游接口时已生成带密钥的完整地址。此前未保存地址，可输入已保存的密钥重新拼接；遗失密钥则重置生成。</p><label>已保存的接入密钥<input id="existing-app-key" type="password" autocomplete="off" placeholder="ph_ 开头"></label><div class="actions">${btn('生成完整接入地址','generate-hook','','outline')}${btn('重置密钥并生成地址','rotate-key',`data-id="${id}"`,'text-button')}</div><label>完整接入地址<div class="copy-row"><input id="saved-hook-url" readonly placeholder="输入已有密钥生成，或重置密钥">${btn('复制完整地址','copy-value','data-copy="saved-hook-url"','outline')}</div></label><small>POST JSON，无需 Authorization 请求头。按本上游独立的下游配置发送；没有下游目标时保存为“待配置分发”。完整地址包含凭据，请妥善保存。</small><hr><h3>标准 API（支持请求头的程序也可使用）</h3><label>上游 API 地址<div class="copy-row"><input id="integration-endpoint" readonly>${btn('复制地址','copy-value','data-copy="integration-endpoint"','outline')}</div></label><div class="request-facts"><span><b>请求方式</b> POST</span><span><b>内容格式</b> application/json</span></div><h3>请求头</h3><pre id="integration-headers">Authorization: Bearer &lt;创建上游接口时保存的密钥&gt;
Content-Type: application/json</pre><small>将占位内容替换为本接入密钥（ph_ 开头）。平台不会再次显示旧密钥。</small><label class="integration-mode">分发方式<select id="integration-mode"><option value="default">按本上游下游配置发送（推荐）</option><option value="targets">本次指定已授权渠道</option></select></label><p class="muted">接口默认使用本上游独立配置的渠道和确认设置。没有下游时先保存为“待配置分发”。</p><div class="sample-heading"><h3>JSON 请求体</h3>${btn('复制 JSON','copy-value','data-copy="integration-json"','text-button')}</div><pre id="integration-json"></pre><small>每条新业务事件使用唯一 request_id；重试同一事件时，编号和内容保持不变。</small><details><summary>Python 调用示例</summary><div class="actions">${btn('复制 Python','copy-value','data-copy="integration-python"','outline')}</div><pre id="integration-python"></pre></details><div class="notice">返回 HTTP 202 表示已保存，随后异步分发。用返回的 id 调用 GET /api/v1/messages/{id} 查询结果，请携带相同鉴权头。401：密钥无效或停用；403：目标未授权；409：业务编号冲突。</div><p class="muted">中转台须监听上游能访问的地址，并放行相应端口；公网接入请使用 HTTPS。上游无需知道 QQ、微信或 SMTP 的凭据。</p></div>`);$('#dialog').classList.add('integration-dialog');renderIntegrationExample();}
async function onAction(button){const a=button.dataset.action,id=button.dataset.id;
  if(a.startsWith('setup-'))return setupAction(a,button);
  if(a.startsWith('recover-'))return recoveryAction(a,button);
  if(a.startsWith('net-')&&await networkAction(a,id))return;
  if(a.startsWith('go-'))return go(a.slice(3));
  if(a==='close')return requestCloseDialog();
  if(a.startsWith('email-'))return emailAction(a,button);
  if(a==='toggle-new-password')return toggleNewPassword(button);
  if(a==='refresh')return refresh();
  if(a==='tab-catalog'||a==='tab-targets'){channelTab=a==='tab-catalog'?'catalog':'targets';return render();}
  if(a==='add-target')return button.dataset.kind?editTarget(button.dataset.kind):chooseChannel();
  if(a==='edit-target')return editTarget('email',id);
  if(a==='bind')return bindTarget(id);
  if(a==='reverify-target'){
    const pane=$('#receipt-verification');if(!pane)return;
    pane.dataset.reverify='true';updateReceiptVerification(true);updateQQBinding();
    pane.scrollIntoView({block:'nearest'});return;
  }
  if(a==='cancel-reverify-target'){
    const pane=$('#receipt-verification');if(!pane)return;
    delete pane.dataset.reverify;updateReceiptVerification(true);updateQQBinding();return;
  }
  if(a==='use-selected'){selected=new Set([...selected].filter(id=>state.targets.some(t=>t.id===id&&verified(t))));return go('compose');}
  if(a==='logout'){await api('/logout',{method:'POST'});return showLogin();}
  if(a==='toggle-target'){const t=state.targets.find(t=>t.id===id);await api('/admin/targets/'+id+'/enabled',{method:'POST',body:{enabled:!t.enabled}});return refresh();}
  if(a==='verify-start'){const result=await api('/admin/targets/'+id+'/verify',{method:'POST'});$('#verify-result').innerHTML='已加入发送队列。'+btn('查看发送结果','message',`data-id="${result.id}"`,'text-button');return toast('请在收件渠道查看验证码');}
  if(a==='reload-binding'){await refresh(false);if($('#qq-connection-status')){updateQQBinding();return loadQQConversations();}return bindTarget(id);}
  if(a==='qq-select'){
    const t=state.targets.find(t=>t.id===id);
    if(t.bound&&button.dataset.current!=='true'){
      const choice=await askChoice('更换收件会话？','<p>将把此目标改为选中的 QQ 会话。更换后需重新确认验证码，才能继续用于分发。</p>',[['cancel','取消','outline'],['confirm','更换并测试','primary']]);
      if(choice!=='confirm')return;
    }
    await api('/admin/targets/'+id+'/qq-recipient',{method:'POST',body:{conversation_id:button.dataset.conversation}});
    await refresh(false);updateQQBinding();$('#verify-form').reset();
    const result=await api('/admin/targets/'+id+'/verify',{method:'POST'});
    $('#verify-result').innerHTML='测试验证码已加入发送队列。'+btn('查看发送结果','message',`data-id="${result.id}"`,'text-button');
    await refresh(false);updateQQBinding();await loadQQConversations();
    $('#verify-form input').focus();return toast('会话已选定，请查看 QQ 中的渠道名称和验证码');
  }
  if(a==='qq-connect'){await api('/admin/targets/'+id+'/qq-connect',{method:'POST'});await refresh(false);updateQQBinding();return toast('QQ 凭据校验通过，正在重新连接');}
  if(a==='wx-qr')return startWeixinQR(id);
  if(a==='wx-poll'){if(wxFlow?.id===id)return pollWeixinFlow(wxFlow,true);return;}
  if(a==='wx-sync'){watchWeixinBinding(id);return pollWeixinFlow(wxFlow,true);}
  if(a==='confirm-received'){const choice=await askChoice('核实此目标已收到？','<p>仅在收件人明确确认收到后登记。此操作不会重新发送，会保留原始尝试记录并处理对应告警。</p>',[['cancel','取消','outline'],['confirm','已核实，登记收到','primary']]);if(choice!=='confirm')return;await api('/admin/deliveries/'+id+'/confirm-received',{method:'POST',body:{confirmed:true}});await refresh(false);await showMessage(button.dataset.message);return toast('已登记收件确认，未重复发送');}
  if(a==='message')return showMessage(id);
  if(a==='retry'){if(button.dataset.state==='unknown'){return openDialog('核实后重新发送',`<div class="notice warning">上次投递结果未知，收件人可能已经收到。重新发送可能造成重复通知。</div><p>请先向收件人核实；只有确定需要再次发送时，再继续。</p>${btn('已核实，允许可能重复并重新发送','retry-confirmed',`data-id="${id}" data-message="${button.dataset.message}"`,'primary wide')}`);}await api('/admin/deliveries/'+id+'/retry',{method:'POST',body:{accept_duplicate_risk:false}});await refresh();return showMessage(button.dataset.message);}
  if(a==='retry-confirmed'){await api('/admin/deliveries/'+id+'/retry',{method:'POST',body:{accept_duplicate_risk:true}});await refresh();return showMessage(button.dataset.message);}
  if(a==='route-message'){const choice=await askChoice('分发这条待配置消息？','<p>将按该上游应用当前的下游配置分发，仅处理这条消息。请确认消息内容仍然有效。</p>',[['cancel','取消','outline'],['route','确认分发','primary']]);if(choice!=='route')return;await api('/admin/messages/'+id+'/route',{method:'POST'});await refresh();await showMessage(id);return toast('已按当前下游配置安排投递');}
  if(a==='cancel-message'){const r=await api('/admin/messages/'+id+'/cancel',{method:'POST'});toast('已取消 '+r.cancelled+' 个投递任务'+(r.cancelled_messages?'、'+r.cancelled_messages+' 条待配置消息':''));await refresh();return showMessage(id);}
  if(a==='resolve'){await api('/admin/alerts/'+id+'/resolve',{method:'POST'});return refresh();}
  if(a==='app-add-channel')return addChannelFromApp();
  if(a==='create-app')return createApp();
  if(a==='edit-app')return createApp(id);
  if(a==='edit-profile')return editProfile(id||null);
  if(a==='app-integration')return showIntegration(id);
  if(a==='delete-target'||a==='delete-app')return deleteItem(a==='delete-target'?'targets':'apps',id);
  if(a==='delete-confirmed'){const table=button.dataset.table;const result=await api('/admin/'+table+'/'+id,{method:'DELETE'});selected.delete(id);if(appChannelFlow&&table==='targets'){await refresh();returnToAppFromChannel();return toast('草稿已删除，已返回上游配置');}closeDialog();await refresh();return toast('已删除，取消 '+result.cancelled+' 个待发送任务'+(result.cancelled_messages?'、'+result.cancelled_messages+' 条待配置消息':'')+'；历史记录保留');}
  if(a==='use-current-origin'){const field=$('#'+button.dataset.field);field.value=location.origin;if(field.id==='new-hook-base')updateNewHook();else{$('#saved-hook-url').value='';renderIntegrationExample();}return toast('已采用当前网址；服务监听和代理映射保持原配置');}
  if(a==='copy-value'){const element=document.getElementById(button.dataset.copy);const value=element.value??element.textContent;if(!value)throw new Error('请先生成完整内容再复制');await copyText(value);return toast('已复制');}
  if(a==='toggle-app'){const v=state.apps.find(v=>v.id===id);await api('/admin/apps/'+id+'/enabled',{method:'POST',body:{enabled:!v.enabled}});return refresh();}
  if(a==='rotate-key'){const choice=await askChoice('重置接入密钥？','<p>将生成新的专属接入地址。旧地址和旧密钥立即失效，已入队消息继续处理；应用启停状态保持不变。</p>',[['cancel','取消','outline'],['rotate','重置并生成地址','primary']]);if(choice!=='rotate')return;const result=await api('/admin/apps/'+id+'/rotate-key',{method:'POST'});return showNewKey(result);}
  if(a==='generate-hook'){const key=$('#existing-app-key').value.trim();if(!/^ph_[A-Za-z0-9_-]{40,100}$/.test(key))throw new Error('请输入创建上游接口时保存的完整 ph_ 密钥');renderIntegrationExample();if($('#integration-error').textContent)throw new Error($('#integration-error').textContent);$('#saved-hook-url').value=new URL($('#integration-base').value).origin+'/api/v1/hooks/'+key;toast('已按所填密钥生成地址；实际请求仍会校验密钥和应用权限');return;}
  if(a==='copy-key'){await copyText($('#new-api-key').textContent);return toast('已复制密钥');}
  if(a==='network-test'){const r=await api('/admin/network/test',{method:'POST'});$('#network-result').innerHTML=netResult(r);return;}
  if(a==='clear-proxy'){await api('/admin/settings',{method:'PUT',body:{public_base_url:state.settings.public_base_url,alert_target_id:state.settings.alert_target_id,default_proxy:''}});await refresh();return toast('已清除默认代理');}
  if(a==='save-profile'){const f=$('#compose-form'),ids=$$('input[name=target_ids]:checked',f).map(i=>i.value);if(!ids.length)throw new Error('请先勾选已验证的目标');selected=new Set(ids);openDialog('保存分发方案',`<form id="profile-form">${inputField('name','方案名称','','text',true,'maxlength="80"')}<p>包含 ${ids.length} 个目标；仅用于系统分发，不关联上游。</p><label class="check-label"><input type="checkbox" name="require_ack" ${f.require_ack.checked?'checked':''}>要求收件确认</label>${inputField('ack_minutes','确认等待时间（分钟）',f.ack_minutes.value,'number',true,'min="1" max="10080"')}<button class="primary wide" type="submit">保存方案</button></form>`);return;}
  if(a==='previous'){offset=Math.max(0,offset-50);return refresh();}
  if(a==='next'){offset+=50;return refresh();}
}
document.addEventListener('click',async e=>{const b=e.target.closest('button[data-action]');if(!b||b.disabled)return;e.preventDefault();b.disabled=true;try{await onAction(b);}catch(error){toast(error.message,true);}finally{if(b.isConnected)b.disabled=false;}});
document.addEventListener('submit',async e=>{const f=e.target;e.preventDefault();const submit=$('button[type=submit]',f);if(submit)submit.disabled=true;try{
  const data=Object.fromEntries(new FormData(f));
  if(f.id==='setup-create-form'){await setupSubmit(f);return;}
  if(f.id==='account-password-form'){await changeAccountPassword(f);return;}
  if(f.id.startsWith('net-')&&await networkSubmit(f))return;
  if(f.id==='login-form'){const r=await api('/login',{method:'POST',body:{username:data.username,password:data.password}});csrf=r.csrf;f.reset();$('#login').hidden=true;$('#shell').hidden=false;await refresh();return;}
  if(f.id==='target-form'&&f.dataset.kind==='email'){await saveEmailChannel(f);return;}
  if(f.id==='target-form'){const {name,...config}=data;const id=f.dataset.id;for(const key of Object.keys(config)){if(config[key]===''&&id)delete config[key];}if(config.port)config.port=Number(config.port);if(!id&&!['qq','weixin'].includes(f.dataset.kind)){const choice=await askChoice('保存待绑定草稿？','<p>当前尚未完成绑定与收件验证。继续将保存连接配置为草稿，以便进行扫码或发送验证码。</p><p>草稿不能用于分发，之后可以继续绑定或直接删除。</p>',[['cancel','返回修改','outline'],['save','保存草稿并继续绑定','primary']]);if(choice!=='save')return;}if(config.network_mode==='custom'&&!config.proxy&&!state.targets.find(t=>t.id===id)?.has_proxy)throw new Error('单独代理模式需要填写代理地址');if(f.dataset.kind==='qq'){const r=await api('/admin/qq/connect'+(id?'?target_id='+encodeURIComponent(id):''),{method:'POST',body:{name,kind:'qq',config}});await refresh();bindTarget(r.id);toast('QQ 凭据校验通过，配置已保存；收件人和投递状态请查看下方');return;}const r=await api('/admin/targets'+(id?'/'+id:''),{method:id?'PUT':'POST',body:{name,kind:f.dataset.kind,config}});await refresh();bindTarget(r.id);if(f.dataset.kind==='weixin'&&!state.targets.find(t=>t.id===r.id)?.bound){await startWeixinQR(r.id);}else toast('配置已保存，请完成收件验证');return;}
  if(f.id==='verify-form'){const id=f.dataset.id;await api('/admin/targets/'+id+'/confirm',{method:'POST',body:{code:data.code}});if(appChannelFlow){await refresh();returnToAppFromChannel(id);toast('收件验证完成，已返回上游配置');}else{closeDialog();await refresh();toast('收件验证完成，可以勾选用于分发');}return;}
  if(f.id==='compose-form'){const payload={title:data.title,body:data.body,request_id:data.request_id,target_ids:new FormData(f).getAll('target_ids'),require_ack:f.require_ack.checked,ack_minutes:Number(data.ack_minutes)};for(const key of ['scheduled_at','expires_at'])if(data[key])payload[key]=new Date(data[key]).toISOString();if(!payload.target_ids.length)throw new Error('请选择至少一个已验证的目标');const r=await api('/admin/messages',{method:'POST',body:payload});toast(r.deduplicated?'该消息已存在，未重复创建':'消息已保存，正在等待分发');await refresh(false);go('records');await showMessage(r.id);return;}
  if(f.id==='profile-form'){await api('/admin/profiles',{method:'POST',body:{name:data.name,target_ids:[...selected],require_ack:f.require_ack.checked,ack_minutes:Number(data.ack_minutes)}});closeDialog();await refresh(false);toast('分发方案已保存');return;}
  if(f.id==='route-form'){const ids=new FormData(f).getAll('target_ids');if(!ids.length)throw new Error('请选择至少一个已验证的目标');const id=f.dataset.id;const result=await api('/admin/profiles'+(id?'/'+id:''),{method:id?'PUT':'POST',body:{name:data.name,target_ids:ids,require_ack:f.require_ack.checked,ack_minutes:Number(data.ack_minutes)}});await refresh();closeDialog();toast('分发方案已保存，新消息按此方案分发');return;}
  if(f.id==='app-form'){const id=f.dataset.id;const r=await api('/admin/apps'+(id?'/'+id:''),{method:id?'PUT':'POST',body:{name:data.name,target_ids:new FormData(f).getAll('target_ids'),require_ack:f.require_ack.checked,ack_minutes:Number(data.ack_minutes)}});await refresh();if(id){closeDialog();toast('上游配置已保存，现有密钥保持有效');return;}showNewKey(r);return;}
  if(f.id==='settings-form'){const payload={public_base_url:data.public_base_url,alert_target_id:data.alert_target_id};if(data.default_proxy)payload.default_proxy=data.default_proxy;await api('/admin/settings',{method:'PUT',body:payload});await refresh();toast('设置已保存');return;}
}catch(error){if(f.id==='login-form')$('#login-error').textContent=error.message;else toast(error.message,true);}finally{if(submit?.isConnected)submit.disabled=false;}});
document.addEventListener('change',e=>{const el=e.target;if(el.name==='network_mode'&&el.closest('#target-form')){const box=$('#target-proxy-choice');if(box){box.hidden=el.value!=='profile';$('select',box).disabled=box.hidden;}return;}if(el.id==='new-hook-base'){updateNewHook();return;}if(el.id==='integration-base'&&$('#saved-hook-url'))$('#saved-hook-url').value='';if(el.id==='integration-mode'||el.id==='integration-base'){renderIntegrationExample();return;}if(el.name==='target_ids'&&el.closest('#app-form')){updateAppDownstreamSummary();return;}if(el.dataset.target){if(el.checked)selected.add(el.dataset.target);else selected.delete(el.dataset.target);$('#selected-count').textContent='已选择 '+selected.size+' 个目标';}if(el.id==='target-filter'){channelFilter=el.value;render();}if(el.id==='compose-profile'){const p=state.profiles.find(v=>v.id===el.value);if(p){const f=$('#compose-form');$$('input[name=target_ids]',f).forEach(i=>i.checked=!i.disabled&&p.target_ids.includes(i.value));f.require_ack.checked=p.require_ack;f.ack_minutes.value=p.ack_minutes;}}});
document.addEventListener('input',e=>{if(e.target.id==='target-search'){const pos=e.target.selectionStart;search=e.target.value;render();const input=$('#target-search');input.focus();input.setSelectionRange(pos,pos);}});
window.addEventListener('hashchange',async()=>{try{if(await requestCloseDialog())render();else history.replaceState(null,'','#'+page);}catch(error){history.replaceState(null,'','#'+page);toast(error.message,true);}});
// Backdrop clicks intentionally do nothing; close through controls or Escape.
$('#dialog').addEventListener('cancel',e=>{e.preventDefault();requestCloseDialog().catch(error=>toast(error.message,true));});
async function boot(){try{csrf=(await api('/session')).csrf;$('#login').hidden=true;$('#shell').hidden=false;await refresh();}catch{showLogin();}}
setInterval(async()=>{if(!csrf||document.hidden)return;try{await refresh(!$('#dialog').open&&(['dashboard','records','alerts','apps'].includes(page)||(page==='setup'&&!document.activeElement?.matches('input,select,textarea,button'))));}catch(error){if(csrf)toast('刷新失败：'+error.message,true);}},10000);
let qqPollBusy=false;
setInterval(async()=>{if(!csrf||document.hidden||!$('#qq-connection-status')||qqPollBusy)return;qqPollBusy=true;try{await refresh(false);updateQQBinding();await loadQQConversations();}catch{const box=$('#qq-connection-status');if(box)box.textContent='暂时无法刷新连接状态，请检查中转台连接';}finally{qqPollBusy=false;}},2000);
boot();
