'use strict';
// Credentials live only in this page's memory, never browser storage.
const setupKeys=new Map(), setupRequests=new Map(), setupExamples=new Map();
function rememberSetupKey(result){setupKeys.set(result.id,result.api_key);}
function clearSetupSecrets(){setupKeys.clear();setupRequests.clear();setupExamples.clear();}
function setupLocation(){const parts=location.hash.slice(1).split('/');const app=state.apps.find(a=>a.id===parts[1]);return {app,step:Math.max(1,Math.min(4,Number(parts[2])||(!app?1:!app.setup.channels_ready?2:!app.setup.test?.confirmed?3:4))) };}
function setupNav(app,step){return 'setup'+(app?'/'+app.id+'/'+step:'');}
function setupStateText(record){return record?Object.entries(record.states).map(([s,n])=>stateBadge(s)+' '+n).join(' · '):'尚无记录';}
function setupView(){
  const {app,step}=setupLocation(), s=app?.setup;
  const done=[!!app,!!s?.channels_ready,!!s?.test?.confirmed,!!s?.upstream?.accepted];
  const names=['创建上游接口','配置下游渠道','试发并确认收件','接通上游程序'];
  let html=heading('接入向导','从创建接口到收到第一条消息，按实际结果检查进度',btn('刷新进度','setup-refresh','','outline'));
  html+=`<div class="setup-toolbar"><label>当前上游<select id="setup-app"><option value="">创建新的上游</option>${state.apps.map(a=>`<option value="${a.id}" ${app?.id===a.id?'selected':''}>${esc(a.name)}${a.enabled?'':'（已停用）'}</option>`).join('')}</select></label><p class="muted">每个上游独立选择下游。可随时离开，再从这里继续。</p></div>`;
  html+=`<ol class="setup-steps" aria-label="接入步骤">${names.map((n,i)=>`<li class="${step===i+1?'current':''} ${done[i]?'done':''}"><button data-action="setup-step" data-id="${app?.id||''}" data-step="${i+1}" ${!app&&i?'disabled':''} ${step===i+1?'aria-current="step"':''}><span>${done[i]?'✓':i+1}</span><b>${n}</b><small>${done[i]?'已完成':step===i+1?'当前步骤':'待完成'}</small></button></li>`).join('')}</ol>`;
  if(app&&!app.enabled)html+=`<div class="notice warning">此上游接口已停用，不能接收新消息或试发。${btn('启用此上游','toggle-app',`data-id="${app.id}"`,'outline')}</div>`;
  if(app?.enabled&&done.every(Boolean))html+='<div class="notice setup-success" role="status"><b>接入检查已完成</b><p>当前渠道已确认收到平台试发，接口也已收到上游推送并取得渠道回执。每条业务消息的实际结果仍以投递记录为准。</p></div>';
  html+='<section class="panel setup-panel">';
  if(step===1){html+=app?`<h2>1. 上游接口已创建</h2><p>${esc(app.name)} · 密钥无自动到期时间，可停用或重置。</p><p>先为这个上游选择自己的收件渠道；也可直接进入第 4 步配置上游，未配置下游的消息将先保存待分发。</p>${btn('下一步：配置下游','setup-step',`data-id="${app.id}" data-step="2"`)}`:`<h2>1. 给上游起个名称</h2><p>例如“文思量化”或“服务器告警”。创建后分配专属 API 和密钥，不需要先绑定收件渠道。</p><form id="setup-create-form">${inputField('name','上游名称','','text',true,'maxlength="80" placeholder="例如：文思量化"')}<button type="submit" class="primary">创建接口，进入下游配置</button></form>`;}
  if(step===2&&app){const ts=state.targets.filter(t=>app.target_ids.includes(t.id));html+=`<h2>2. 配置 ${esc(app.name)} 的下游渠道</h2><p>添加 QQ、微信或邮箱，完成收件验证码，再保存到此上游。勾选范围仅对当前上游生效。</p>${ts.length?`<ul class="setup-channel-list">${ts.map(t=>`<li>${icon(channels[t.kind].icon)}<span>${esc(t.name)}<small>${channels[t.kind].name}</small></span>${targetBadge(t)}</li>`).join('')}</ul>`:'<div class="notice">还没有选择下游渠道。已接收的上游消息会保存为“待配置分发”，不会自动丢弃。</div>'}${app.route_error?`<p class="error">${esc(app.route_error)}</p>`:''}<div class="actions setup-actions">${btn('配置此上游的下游渠道','setup-configure',`data-id="${app.id}"`)}${btn('下一步：测试收件','setup-step',`data-id="${app.id}" data-step="3" ${s.channels_ready?'':'disabled'}`,'outline')}</div><small>已有渠道可直接勾选；新增渠道在绑定完成后会返回配置窗口，记得保存。</small>`;}
  if(step===3&&app){const t=s.test;html+=`<h2>3. 试发并确认收件</h2><p>向当前勾选的 ${s.configured_count} 个渠道发送一条带编号的测试消息。这里只检查平台到下游，上游接通结果在下一步查看。</p>${!s.channels_ready?'<div class="notice warning">请先完成下游配置和收件验证。</div>':''}${t?`<div class="setup-result" role="status"><b>${t.confirmed?'当前渠道均已确认收件':t.accepted?'渠道已接受，请核对实际收件':'最近一次试发'}</b><p>${setupStateText(t)}</p><small>${date(t.created)}${!t.current_configuration?' · 下游配置已变化，当前配置需要重新试发确认':''}</small>${btn(t.confirmed?'查看试发记录':'查看结果并确认收件','message',`data-id="${t.id}"`,'outline')}</div>`:'<div class="notice">尚未试发。请准备查看各渠道的收件消息。</div>'}<div class="actions setup-actions">${btn(t?'重新试发一条':'发送测试消息','setup-send',`data-id="${app.id}" ${s.channels_ready&&app.enabled?'':'disabled'}`,t?.confirmed?'outline':'primary')}${btn('下一步：配置上游','setup-step',`data-id="${app.id}" data-step="4"`,t?.confirmed?'primary':'outline')}</div><small>渠道回执不等于实际收到。在结果中核对渠道后，逐个登记收件；不会自动替你确认。</small>`;}
  if(step===4&&app){
    const key=setupKeys.get(app.id),base=accessBase();if(!setupExamples.has(app.id))setupExamples.set(app.id,'first-'+newRequestId());
    const sample=JSON.stringify({title:'上游接入测试 · '+app.name,body:'这是从上游提交的接入测试，请核对实际收件。',request_id:setupExamples.get(app.id)},null,2);
    html+=`<h2>4. 在上游程序填写以下配置</h2><p>在你的上游程序中保存这些参数，再由上游主动发送一条测试消息。平台不会主动读取上游数据。</p>${key?`<label>完整 API 地址（包含密钥）<div class="copy-row"><input id="setup-url" readonly value="${esc(base+'/api/v1/hooks/'+key)}">${btn('复制 API 地址','copy-value','data-copy="setup-url"','outline')}</div></label><small>密钥仅临时保留在当前页面，刷新或退出后不再显示，请保存到上游配置。</small>`:`<div class="notice">请使用创建时保存的专属地址。${btn('填写已有密钥或重置生成','app-integration',`data-id="${app.id}"`,'text-button')}重置会使旧地址失效。</div>`}<p><b>请求方法</b> POST　<b>内容格式</b> application/json　<b>请求头鉴权</b> 专属地址无需另填</p><small>${accessSource()} 上游在另一台电脑时，不能使用 127.0.0.1；请用其能访问的局域网或公网地址。${btn('调整接入地址','app-integration',`data-id="${app.id}"`,'text-button')}</small><label>JSON 消息格式<pre id="setup-json">${esc(sample)}</pre></label>${btn('复制消息格式','copy-value','data-copy="setup-json"','outline')}<p class="muted">每个新业务事件使用新 request_id；同一事件重试保持编号和内容不变。HTTP 202 表示已保存，投递结果需继续查询。</p><div class="setup-result" role="status"><b>${app.received_count?'已收到该接口的上游推送':'等待上游首次推送'}</b><p>${app.received_count?'累计 '+app.received_count+' 条 · 最近接收 '+date(app.last_received):'平台试发不计入这里；请从上游程序发送。'}</p>${s.upstream?`<p>最近一条：${s.upstream.routing_state==='awaiting_routing'?stateBadge('awaiting_routing'):setupStateText(s.upstream)}</p>${!s.upstream.current_configuration?'<small>这条消息不对应当前下游配置；请核对记录，并从上游发送一个新的测试事件。</small>':''}${btn('查看上游消息结果','message',`data-id="${s.upstream.id}"`,'outline')}`:''}${btn('检查接入结果','setup-refresh','','outline')}</div><details class="setup-errors"><summary>上游提交失败，如何处理？</summary><dl><dt>连接超时 / 连接被拒绝</dt><dd>先从上游所在设备打开平台网址；检查 IP、端口、服务、防火墙与反向代理。</dd><dt>401 / 403</dt><dd>核对完整密钥是否有缺字、已重置或接口被停用；不要把管理页面登录密码填入 API。</dd><dt>409</dt><dd>检查响应中的原因。业务编号相同而内容变化时会拒绝；还可能是下游配置需核对。</dd><dt>422</dt><dd>按 JSON 示例填写 title、body、request_id，检查引号、必填字段及时间格式。</dd><dt>202，但没有收到</dt><dd>打开上方消息结果，按每个渠道的状态处理。不要另换业务编号反复提交同一条业务消息。</dd></dl></details>`;
  }
  return html+'</section><p class="setup-help"><a href="#help/quickstart">完整操作说明</a> · <a href="#help/troubleshooting">常见故障处理</a></p>';
}
async function setupAction(a,b){
  const id=b.dataset.id;
  if(a==='setup-step'){closeDialog();go('setup'+(id?'/'+id+'/'+b.dataset.step:''));return;}
  if(a==='setup-refresh'){await refresh();toast('已按保存的配置和投递记录更新进度');return;}
  if(a==='setup-configure'){createApp(id);return;}
  if(a==='setup-send'){
    const old=setupRequests.get(id);
    if(!old&&state.apps.find(x=>x.id===id)?.setup.test&&await askChoice('重新发送一条测试消息？','<p>这会创建新的测试消息。若只想核对刚才的结果，请返回查看结果；自动重试中的消息会继续处理。</p>',[['cancel','查看已有结果','outline'],['send','发送新的测试','primary']])!=='send')return;
    const request=old||newRequestId().replaceAll('-','');if(!request)throw new Error('浏览器无法生成测试编号，请升级浏览器后重试');setupRequests.set(id,request);
    const result=await api('/admin/apps/'+id+'/setup-test',{method:'POST',body:{request_id:request}});
    setupRequests.delete(id);await refresh();await showMessage(result.id);toast(result.deduplicated?'已找到同一次试发，没有重复创建':'测试消息已入队，请核对收件');
  }
}
async function setupSubmit(f){const result=await api('/admin/apps',{method:'POST',body:{name:f.elements.name.value,target_ids:[]}});rememberSetupKey(result);await refresh(false);go('setup/'+result.id+'/2');toast('上游接口已创建，接入地址可在第 4 步复制');}
document.addEventListener('change',e=>{if(e.target.id==='setup-app')go('setup'+(e.target.value?'/'+e.target.value:''));});
