'use strict';
let networkTab='diagnostics',proxySettingsOpen=false;
document.addEventListener('toggle',event=>{if(event.target.id==='net-proxy-settings')proxySettingsOpen=event.target.open;},true);
function netData(){return state.network||{proxies:[],routes:{},diagnostics:[],default_proxy_id:''};}
function proxyOptions(value='',includeDefault=false){return `${includeDefault?'<option value="default">跟随默认连接方式</option>':''}<option value="system" ${value==='system'?'selected':''}>直接连接</option>${netData().proxies.map(p=>`<option value="proxy:${p.id}" ${value==='proxy:'+p.id?'selected':''}>使用代理 · ${esc(p.name)}</option>`).join('')}`;}
function netEndpoint(p){return `${p.scheme}://${p.host.includes(':')?'['+p.host+']':p.host}:${p.port}`;}
function routeLabel(r){if(r.system_only)return '系统网络<small>由系统路由决定，可能经过 VPN / TUN</small>';return `${r.mode==='default'?'跟随默认 · ':''}${r.proxy_id||r.mode==='custom'?'使用代理 · ':''}${esc(r.label)}${r.system_only?'（系统网络）':''}`;}
function networkPanel(){
  const n=netData(),defaultProxy=n.proxies.find(p=>p.id===n.default_proxy_id);
  const defaultLabel=defaultProxy?'使用代理 · '+defaultProxy.name:'直接连接';
  const tabs=[['diagnostics','通信检测'],['external','平台访问地址'],['outlets','连接方式']];
  let html=heading('网络通信','配置平台访问地址、检测渠道通信，按需使用代理')+`<div class="tabs net-tabs">${tabs.map(([id,name])=>`<button type="button" data-action="net-tab" data-id="${id}" class="${networkTab===id?'active':''}" aria-pressed="${networkTab===id}">${name}</button>`).join('')}</div>`;
  if(networkTab==='outlets'){
    html+=`<section class="panel"><form id="net-default-form" class="net-default" data-previous-proxy-id="${esc(n.default_proxy_id)}" data-expected-targets="${esc(JSON.stringify(Object.keys(n.routes).filter(id=>n.routes[id].mode==='default')))}"><div><h2>默认连接方式</h2><small>可直接连接；需要时选择已保存的代理。仅影响“跟随默认连接方式”的渠道。</small></div><label class="net-default-select">默认连接方式<select name="route">${proxyOptions(n.default_proxy_id?'proxy:'+n.default_proxy_id:'system')}</select></label><button type="submit" class="primary">保存默认连接方式</button></form></section>`;
    html+=`<section class="panel"><div class="net-panel-heading"><h2>渠道连接方式</h2><small>调整连接方式会保留收件验证和上游分发范围</small></div>${networkTargets()}</section><div id="net-inline-result" role="status"></div>`;
    html+=`<details id="net-proxy-settings" class="panel net-proxy-settings" ${proxySettingsOpen?'open':''}><summary><strong>代理设置（可选）</strong><small>${n.proxies.length} 个已保存</small></summary><p class="muted">仅在需要经过 Clash 等代理访问下游时配置。添加代理后，还需在默认或渠道连接方式中选择使用。</p><div class="net-panel-heading"><h2>已保存的代理</h2>${btn('添加代理','net-proxy-add','','outline')}</div>${n.proxies.length?`<div class="table-wrap"><table class="net-table"><thead><tr><th>代理名称</th><th>代理地址</th><th>使用范围</th><th>操作</th></tr></thead><tbody>${n.proxies.map(p=>`<tr><td><strong>${esc(p.name)}</strong>${p.is_default?'<small>默认连接方式</small>':''}</td><td><code>${esc(netEndpoint(p))}</code><small>${p.username?'用户名：'+esc(p.username)+(p.has_password?' · 密码已保存':''):'无需认证'}</small></td><td>${p.target_ids.length} 个渠道<small>${esc(p.target_names.join('、')||'暂未引用')}</small></td><td><div class="net-row-actions">${btn('检测','net-test-proxy',`data-id="${p.id}"`,'text-button')}${btn('编辑','net-proxy-edit',`data-id="${p.id}"`,'text-button')}${btn('删除','net-proxy-delete',`data-id="${p.id}"`,'text-button danger')}</div></td></tr>`).join('')}</tbody></table></div>`:empty('未配置代理，可直接连接。需要代理时再添加。')}</details>`;
    html+=`<details class="net-notes panel"><summary>直接连接、代理与系统网络说明</summary><p>直接连接使用系统网络，仍受系统 VPN / TUN 路由影响。程序不会自动读取 Windows 系统代理设置；需要应用内代理时，请在本页明确选择。</p><p>邮箱和 Apprise 扩展使用系统网络，需要时可配合系统层 TUN。它们不使用此处保存的应用内代理。</p><p>Clash 与本平台在同机时，可填写 127.0.0.1 和实际 HTTP / 混合 / SOCKS5 端口。容器中的 127.0.0.1 指向容器本身。</p><p>连接尚未建立或代理协商失败时，首次等待 30 秒，后续每 60 秒自动重试，至投递截止后停止。成功渠道不会重发；已使用的代理发生故障时不会自动切换为直连。请求发出后回执不明，标记“结果待核实”。</p></details>`;
  }else if(networkTab==='diagnostics'){
    html+=`<section class="panel"><h2>下游通信检测</h2><form id="net-test-form" class="net-test-form"><label>检测使用的连接<select name="proxy_id"><option value="">默认：${esc(defaultLabel)}</option>${n.proxies.map(p=>`<option value="${p.id}">使用代理 · ${esc(p.name)}</option>`).join('')}</select></label><label>检测服务<select name="endpoint">${['qq','weixin','wecom','dingtalk','feishu'].map(k=>`<option value="${k}">${esc(channels[k].name)}</option>`).join('')}</select></label><button type="submit" class="primary">开始检测</button></form><small>仅检测中转台到下游服务的网络可达性，不登录账号、不发送通知，也不修改连接方式。查看上游是否提交消息，请到<a href="#apps">上游分发</a>。</small><div id="net-inline-result" role="status"></div></section>`;
    html+=`<section class="panel net-meaning"><h2>如何理解通信与投递状态</h2><dl class="net-state-guide"><div><dt>网络可达</dt><dd>检测时收到服务响应，尚未核验账号或消息投递。</dd></div><div><dt>账号已授权</dt><dd>渠道凭据校验或扫码授权完成，尚未确认实际收件。</dd></div><div><dt>渠道已接受</dt><dd>发送接口返回接受结果，尚未确认收件人收到。</dd></div><div><dt>收件人已确认</dt><dd>通过收件确认操作或明确反馈，登记实际收到。</dd></div></dl></section>`;
    html+=`<section class="panel"><h2>按渠道检测</h2><small>使用各渠道实际连接方式；账号授权与收件验证请进入“绑定与测试”。</small>${networkTargets(true)}</section><section class="panel"><h2>最近检测</h2><small>记录仅代表当次检测结果，并非持续在线状态。修改配置后请重新检测。</small><div id="net-history">${n.diagnostics.length?n.diagnostics.slice(0,8).map(netResult).join(''):empty('尚无检测记录')}</div></section>`;
  }else{
    html+=`<form id="settings-form" class="panel net-external"><h2>平台访问地址</h2><label>上游与收件人可访问的平台地址<input name="public_base_url" type="url" value="${esc(state.settings.public_base_url)}" placeholder="https://notify.example.com"><small>可填局域网 IP 或公网域名，包含协议及实际端口。此地址用于生成上游接入示例、QQ 回调和收件确认链接。</small></label><p class="muted">留空时，上游接入示例采用浏览器当前网址；后台收件确认链接仍需设置固定地址。</p><label>告警接收渠道<select name="alert_target_id"><option value="">仅在站内告警中心显示</option>${state.targets.filter(verified).map(t=>`<option value="${t.id}" ${t.id===state.settings.alert_target_id?'selected':''}>${esc(t.name)} · ${esc(channels[t.kind].name)}</option>`).join('')}</select></label><div class="notice">此处配置上游与收件人访问中转台的入口。中转台向下游发送消息时，使用各渠道的接口地址和连接方式。填写地址不会自动配置 frp、Nginx、监听端口或防火墙，也不会验证公网访问是否正常。</div><button type="submit" class="primary">保存访问设置</button></form>`;
  }
  return html;
}
function networkTargets(diagnostics=false){
  if(!state.targets.length)return empty('添加下游渠道后，可在这里选择连接方式和检测通信。');
  return `<div class="table-wrap"><table class="net-table"><thead><tr><th>渠道名称</th><th>实际连接方式</th><th>操作</th></tr></thead><tbody>${state.targets.map(t=>{const r=netData().routes[t.id];return `<tr><td><strong>${esc(t.name)}</strong><small>${esc(channels[t.kind].name)}${!t.enabled?' · 已停用':''}</small></td><td>${routeLabel(r)}${t.kind==='qq'?`<small>机器人 ${esc(t.config.app_id)} · ${r.shared_ids.length} 个渠道共用连接方式</small>`:''}</td><td><div class="net-row-actions">${!r.system_only&&!diagnostics?btn('调整连接方式','net-route',`data-id="${t.id}"`,'text-button'):''}${btn('检测通信','net-test-target',`data-id="${t.id}"`,'text-button')}${diagnostics?btn('绑定与测试','bind',`data-id="${t.id}"`,'text-button'):''}</div></td></tr>`;}).join('')}</tbody></table></div>`;
}
function netResult(r){
  const status={reachable:'网络可达',failed:'检测失败',warning:'服务需检查',unsupported:'需渠道测试'}[r.status]||'检测结果';
  const t=state.targets.find(t=>t.id===r.id);
  const systemOnly=['email','apprise'].includes(r.endpoint)||r.route==='系统网络';
  const connection=systemOnly?'系统网络（遵循系统 VPN / TUN 路由）':r.route==='直接连接'?r.route:'使用代理 · '+r.route;
  return `<article class="net-result ${r.status==='failed'?'net-failed':''}"><div class="net-panel-heading"><strong>${esc(t?t.name+' · ':r.route+' · ')}${status}</strong><small>${date(r.tested_at)} · ${r.latency_ms} ms</small></div><p>${esc(r.detail)}</p><small>${esc(r.hint)}</small><small class="net-result-route">检测连接：${esc(connection)}</small></article>`;
}
async function runNetTest(payload,output='#net-inline-result'){
  const box=$(output);if(box)box.innerHTML='<div class="notice">正在检测通信，请稍候…</div>';
  try{const r=await api('/admin/network/test',{method:'POST',body:payload});await refresh(false);if(box?.isConnected&&box===$(output))box.innerHTML=netResult(r);if($('#net-history'))$('#net-history').innerHTML=netData().diagnostics.slice(0,8).map(netResult).join('');return r;}
  catch(error){if(box?.isConnected&&box===$(output))box.innerHTML=`<div class="notice warning">${esc(error.message)}</div>`;throw error;}
}
function editProxy(id){
  const p=netData().proxies.find(p=>p.id===id);
  openDialog(p?'编辑代理':'添加代理',`<form id="net-proxy-form" data-id="${id||''}" data-revision="${p?.revision||''}">${inputField('name','代理名称',p?.name||'','text',true,'maxlength="80" placeholder="例如：本机 Clash"')}<details><summary>粘贴完整代理地址（可选）</summary><label>完整代理地址<input name="url" type="password" autocomplete="new-password" placeholder="http://用户名:密码@主机:端口"><small>填写后优先使用完整地址；留空则使用下面的独立字段。</small></label></details><div class="net-fields">${selectField('scheme','代理协议',[['http','HTTP'],['https','HTTPS'],['socks5','SOCKS5'],['socks5h','SOCKS5h']],p?.scheme||'http')}${inputField('host','代理主机',p?.host||'','text',false,'placeholder="127.0.0.1"')}${inputField('port','代理端口',p?.port||'','number',false,'min="1" max="65535" placeholder="填写实际监听端口"')}</div><details class="net-proxy-auth" ${p?.username?'open':''}><summary>代理认证（可选）${p?.username?' · 已配置':''}</summary><div class="net-fields">${inputField('username','代理用户名（可选）',p?.username||'','text',false,'autocomplete="off"')}${inputField('password','代理密码（可选）','','password',false,`autocomplete="new-password" placeholder="${p?.has_password?'已保存；留空保持不变':'未设置'}"`)}</div>${p?.has_password?'<label class="check-label"><input name="clear_password" type="checkbox">清除已保存的密码</label>':''}</details><small>HTTP / 混合端口常用于 Clash。这里的协议仅描述如何连接代理，与目标网站是否 HTTPS 无关。端口以 Clash 实际设置为准。</small>${p?`<div class="notice">影响范围：${p.is_default?'默认连接方式；':''}${p.target_ids.length} 个渠道。${esc(p.target_names.join('、'))}</div>`:''}<div id="net-preview-result" role="status"></div><div class="actions">${!p?btn('填入 Clash 本机示例','net-clash-example','','text-button'):''}${btn('检测当前配置','net-preview','','outline')}<button type="submit" class="primary">保存代理</button></div></form>`);
  $('#net-proxy-form')._proxySnapshot=p;
}
function proxyPayload(form){
  const data=Object.fromEntries(new FormData(form));const p=form._proxySnapshot;
  return {name:data.name||'代理检测',url:data.url||null,scheme:data.scheme,host:data.host,port:data.port?Number(data.port):null,username:data.username,password:data.clear_password?'':data.password||null,revision:form.dataset.revision?Number(form.dataset.revision):null,expected_targets:p?.target_ids||[],expected_default:p?.is_default||false};
}
function editNetworkRoute(id){
  const t=state.targets.find(t=>t.id===id),r=netData().routes[id];
  openDialog('调整连接方式 · '+t.name,`<form id="net-route-form" data-id="${id}" data-signature="${r.signature}" data-expected-targets="${esc(JSON.stringify(r.shared_ids))}"><label>连接方式<select name="route">${proxyOptions(r.mode==='default'?'default':r.mode==='system'?'system':'proxy:'+r.proxy_id,true)}</select></label><small>直接连接仍受系统 VPN / TUN 路由影响，不会自动读取 Windows 系统代理。</small>${t.kind==='qq'?`<div class="notice">同一机器人统一收发连接方式。此修改将应用到 ${r.shared_ids.length} 个渠道：${esc(r.shared_names.join('、'))}。</div>`:''}<p>保留已验证的收件人、消息记录和各上游的下游选择。连接变化后，QQ 将自动重新连接。</p><div class="actions">${btn('取消','close','','outline')}<button type="submit" class="primary">保存连接方式</button></div></form>`);
}
async function networkAction(action,id){
  if(action==='net-tab'){networkTab=id;render();return true;}
  if(action==='net-proxy-add'||action==='net-proxy-edit'){editProxy(id);return true;}
  if(action==='net-clash-example'){const f=$('#net-proxy-form');f.elements.scheme.value='http';f.elements.host.value='127.0.0.1';f.elements.port.value='7890';toast('已填入示例；请核对 Clash 实际端口');return true;}
  if(action==='net-preview'){const f=$('#net-proxy-form');await runNetTest({scope:'preview',id:f.dataset.id||'',proxy:proxyPayload(f)},'#net-preview-result');return true;}
  if(action==='net-route'){editNetworkRoute(id);return true;}
  if(action==='net-test-proxy'||action==='net-test-target'){
    openDialog('通信检测',`<div id="net-dialog-result" role="status"></div><div class="actions">${btn('关闭','close','','outline')}</div>`);
    await runNetTest({scope:action==='net-test-proxy'?'proxy':'target',id},'#net-dialog-result');return true;
  }
  if(action==='net-proxy-delete'){
    const p=netData().proxies.find(p=>p.id===id);
    if(p.is_default||p.target_ids.length)throw new Error('此代理仍被默认连接方式或渠道引用，请先调整连接方式再删除');
    if(await askChoice('删除代理？',`<p>将删除 ${esc(p.name)} 的代理配置。</p>`,[['cancel','取消','outline'],['delete','删除代理','danger-button']])!=='delete')return true;
    await api('/admin/network/proxies/'+id,{method:'DELETE'});await refresh();toast('代理已删除');return true;
  }
  return false;
}
async function networkSubmit(f){
  if(f.id==='net-test-form'){await runNetTest({scope:f.elements.proxy_id.value?'proxy':'default',id:f.elements.proxy_id.value,endpoint:f.elements.endpoint.value});return true;}
  if(f.id==='net-proxy-form'){
    const id=f.dataset.id,p=f._proxySnapshot;
    if(p&&(p.is_default||p.target_ids.length)&&await askChoice('确认修改共享代理？',`<p>影响 ${p.target_ids.length} 个渠道${p.is_default?'及默认连接方式':''}：${esc(p.target_names.join('、'))}。保存后新请求使用新配置，QQ 连接会自动重连。</p>`,[['cancel','返回修改','outline'],['save','确认保存','primary']])!=='save')return true;
    await api('/admin/network/proxies'+(id?'/'+id:''),{method:id?'PUT':'POST',body:proxyPayload(f)});closeDialog();await refresh();toast('代理已保存；可在渠道连接方式中选择使用');return true;
  }
  if(f.id==='net-route-form'||f.id==='net-default-form'){
    const v=f.elements.route.value,id=f.dataset.id,r=id?netData().routes[id]:null;
    const affected=JSON.parse(f.dataset.expectedTargets);
    if(affected.length>1||!id){const names=state.targets.filter(t=>affected.includes(t.id)).map(t=>t.name).join('、');if(await askChoice('确认修改连接方式？',`<p>此次影响 ${affected.length} 个渠道：${esc(names||'暂无')}。</p>`,[['cancel','取消','outline'],['save','确认保存','primary']])!=='save')return true;}
    await api('/admin/network/'+(id?'targets/'+id:'default'),{method:'PUT',body:{mode:v.startsWith('proxy:')?'profile':v,proxy_id:v.startsWith('proxy:')?v.slice(6):'',expected_targets:affected,signature:f.dataset.signature||'',previous_proxy_id:f.dataset.previousProxyId||''}});if(id)closeDialog();await refresh();toast('连接方式已保存，收件验证保持有效');return true;
  }
  return false;
}
