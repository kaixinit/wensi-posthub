'use strict';
let emailWatch=0;
function emailProviders(){return state.email?.providers||{};}
function emailProvider(sender){const domain=sender.trim().split('@').pop().toLowerCase();return Object.entries(emailProviders()).find(([,p])=>p.domains.includes(domain))?.[0]||'custom';}
function emailCredentialHelp(provider){
  const note='保存后还会发送收件验证码，用于确认收件地址。';
  if(provider==='qq')return {note:'填写 QQ 邮箱生成的 16 位授权码。'+note,html:`<b>获取 QQ 邮箱 SMTP 授权码</b><ol><li>登录 QQ 邮箱网页版，点击右上角头像。</li><li>进入“设置 → 账号与安全 → 安全设置”。</li><li>找到 POP3 / IMAP / SMTP 服务，点击“开启服务”；已开启可继续。</li><li>点击“生成授权码”，按页面提示用手机 QQ 扫码或输入 QQ 密码验证身份。</li><li>复制生成的 16 位授权码，粘贴到上方输入框；备注可填写“文思邮差”。</li></ol><p>SMTP 授权码用于连接发件账号；收件验证码用于确认实际收件。QQ 登录密码只在 QQ 官方页面完成身份验证时使用。</p><div class="email-guide-links"><a href="https://mail.qq.com/" target="_blank" rel="noopener noreferrer">打开 QQ 邮箱 ↗</a><a href="https://help.mail.qq.com/detail/106/985" target="_blank" rel="noopener noreferrer">QQ 官方图文说明 ↗</a></div>`};
  if(provider==='gmail')return {note:'填写 Google 生成的应用专用密码。'+note,html:`<b>获取 Gmail 应用专用密码</b><ol><li>登录发件邮箱对应的 Google 账号，先开启“两步验证”。</li><li>打开“应用专用密码”页面，为文思邮差创建一个应用密码。</li><li>复制生成的 16 位应用专用密码，粘贴到上方输入框。</li></ol><p>部分组织账号、高级保护账号或仅使用安全密钥的账号不提供此选项，请按 Google 官方说明检查。</p><div class="email-guide-links"><a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener noreferrer">打开应用专用密码 ↗</a><a href="https://support.google.com/accounts/answer/185833?hl=zh-Hans" target="_blank" rel="noopener noreferrer">Google 官方说明 ↗</a></div>`};
  return {note:'填写发件服务商提供的 SMTP 授权码或应用密码。'+note,html:`<b>获取其他邮箱的 SMTP 凭据</b><ol><li>登录发件邮箱的官方网站，在账号或安全设置中查找“SMTP”“第三方客户端”或“应用密码”。</li><li>按服务商说明启用发件服务并生成凭据；企业邮箱可向管理员获取。</li><li>将凭据填入上方，并在“服务器设置”填写对应服务器、端口和加密方式。</li></ol><p>各服务商的凭据规则不同，请以其官方说明为准。收件人只需提供邮箱地址。</p>`};
}
function emailForm(c,secrets,existing){
  const provider=existing?(c.provider||'custom'):'auto';
  return `<div class="stepper"><span>1 配置发件邮箱</span><span>2 测试发送</span><span>3 收件验证</span></div>${inputField('sender','发件邮箱',c.sender||'','email',true,'id="email-sender" autocomplete="email" placeholder="例如：123456@qq.com"')}${selectField('provider','邮箱服务商',[['auto','根据发件邮箱自动识别'],...Object.entries(emailProviders()).map(([k,p])=>[k,p.name])],provider)}<div id="email-provider-hint" class="notice" role="status"></div><div id="email-credential"><div class="email-credential-heading"><label id="email-credential-label" for="email-password">邮箱授权码 / 应用专用密码</label><button type="button" class="text-button" data-action="email-credential-help" aria-expanded="false" aria-controls="email-credential-guide">如何获取？</button></div><input id="email-password" name="password" type="password" maxlength="256" autocomplete="new-password" ${secrets.includes('password')?'data-saved="true" placeholder="已保存；留空保持不变"':''} aria-label="邮箱授权凭据" aria-describedby="email-credential-note"><small id="email-credential-note"></small><section id="email-credential-guide" class="email-credential-guide" aria-label="获取邮箱授权凭据说明" hidden></section></div><details id="email-advanced" class="email-advanced"><summary>服务器设置（高级）</summary>${inputField('host','SMTP 服务器',c.host||'','text',false)}${inputField('port','端口',c.port||587,'number',false,'min="1" max="65535"')}${selectField('security','连接安全',[['ssl','SSL / TLS'],['starttls','STARTTLS'],['insecure','仅本机测试中继']],c.security||'starttls')}${inputField('username','SMTP 登录账号',c.username||'','text',false,'placeholder="通常与发件邮箱一致"')}<small>常用邮箱由系统自动填写；其他邮箱请按服务商说明填写。SMTP 使用系统网络，可配合 VPN / Clash TUN。</small></details><hr><label class="check-label"><input type="checkbox" id="email-self" ${!existing||c.sender===c.recipient?'checked':''}>发给自己（收件邮箱与发件邮箱相同）</label>${inputField('recipient','收件邮箱',c.recipient||c.sender||'','email',true,'id="email-recipient"')}<small>收件人只需邮箱地址，无需提供密码。一个渠道对应一个收件地址，验证后才能用于分发。</small><p id="email-form-error" class="error" role="alert"></p>`;
}
function updateEmailForm(){
  const form=$('#target-form');if(form?.dataset.kind!=='email')return;
  form.elements.provider.setAttribute('aria-label','邮箱服务商');
  form.elements.security.setAttribute('aria-label','连接安全');
  const sender=form.elements.sender.value.trim(),chosen=form.elements.provider.value;
  const provider=chosen==='auto'?emailProvider(sender):chosen,p=emailProviders()[provider];
  if(!p)return;
  const outlook=provider==='outlook',preset=['qq','gmail'].includes(provider),advanced=$('#email-advanced');
  const credentialHelp=emailCredentialHelp(provider);
  const guide=$('#email-credential-guide'),toggle=$('[data-action="email-credential-help"]');
  if(guide.dataset.provider!==provider){guide.hidden=true;toggle.setAttribute('aria-expanded','false');guide.innerHTML=credentialHelp.html;guide.dataset.provider=provider;}
  $('#email-credential-note').textContent=credentialHelp.note;
  $('#email-provider-hint').innerHTML=`<b>${chosen==='auto'&&sender.includes('@')?'已识别：':''}${esc(p.name)}</b><p>${esc(p.hint)}</p>${p.help?`<a href="${esc(provider==='qq'?'https://help.mail.qq.com/detail/106/985':p.help)}" target="_blank" rel="noopener noreferrer">查看官方设置说明 ↗</a>`:''}`;
  $('#email-credential').hidden=outlook;form.elements.password.disabled=outlook;
  form.elements.password.required=preset&&!form.elements.password.dataset.saved;
  $('#email-credential-label').textContent=p.credential;
  advanced.hidden=outlook;
  for(const field of ['host','port','security','username'])form.elements[field].disabled=outlook;
  if(preset){for(const field of ['host','port','security'])form.elements[field].value=p[field];form.elements.username.value=sender;}
  for(const field of ['host','port','username'])form.elements[field].readOnly=preset;
  form.elements.security.disabled=outlook||preset;
  form.elements.host.required=!outlook;form.elements.port.required=!outlook;
  advanced.querySelector('summary').textContent=preset?`服务器设置 · ${p.host}:${p.port}（已自动配置）`:'服务器设置（需要填写）';
  if(!outlook&&!preset&&sender.includes('@'))advanced.open=true;
  if(preset)advanced.open=false;
  const self=$('#email-self').checked;form.elements.recipient.readOnly=self;if(self)form.elements.recipient.value=sender;
  form.querySelector('button[type="submit"]').textContent=outlook?'保存并连接微软邮箱':'保存并发送验证码';
  form.dataset.emailProvider=provider;
}
function emailConfig(form){
  const data=Object.fromEntries(new FormData(form));
  const provider=data.provider==='auto'?emailProvider(data.sender):data.provider;
  const config={provider,sender:data.sender.trim(),recipient:data.recipient.trim(),network_mode:'system'};
  if(provider!=='outlook'){
    for(const key of ['host','username','security'])config[key]=form.elements[key].value;
    config.port=Number(form.elements.port.value);
    if(form.elements.password.value)config.password=form.elements.password.value;
  }
  return config;
}
async function saveEmailChannel(form){
  $('#email-form-error').textContent='';
  try{
    const id=form.dataset.id,result=await api('/admin/targets'+(id?'/'+id:''),{method:id?'PUT':'POST',body:{name:form.elements.name.value,kind:'email',config:emailConfig(form)}});
    await refresh(false);bindTarget(result.id);
    const target=state.targets.find(t=>t.id===result.id);
    if(target.verified)return toast('邮箱配置已保存，收件验证保持有效');
    if(target.config.provider==='outlook'){
      if(!target.email_authorized&&state.email.microsoft_client_id)await startEmailAuthorization(result.id);
      else if(target.email_authorized)await sendEmailVerification(result.id);
    }else await sendEmailVerification(result.id);
  }catch(error){const box=$('#email-form-error');if(box)box.textContent=error.message;else toast(error.message,true);}
}
function emailBindingView(t){
  const c=t.config,p=emailProviders()[c.provider||'custom'];
  return `<div class="stepper"><span>1 ${c.provider==='outlook'?'微软授权':'发件配置'}</span><span>2 测试发送</span><span>3 收件验证</span></div><div class="email-summary"><div><small>发件邮箱</small><b>${esc(c.sender)}</b></div><div><small>收件邮箱</small><b>${esc(c.recipient)}</b></div><small>${esc(p?.name||'通用 SMTP')} · ${c.provider==='outlook'?'微软官方邮件接口':esc(c.host+':'+c.port)}</small></div>${c.provider==='outlook'?`<div id="email-auth-status" class="notice ${t.email_authorized?'':'warning'}">${t.email_authorized?'微软邮箱已授权。可发送收件验证码确认这个收件地址。':'先连接微软邮箱，完成授权后再发送收件验证码。'}</div><details id="email-ms-setup" class="email-advanced" ${state.email?.microsoft_client_id?'':'open'}><summary>微软授权设置（管理员一次性配置）</summary><p>使用你自己登记的微软应用，支持个人 Microsoft 账号，并开启公共客户端流。此处无需客户端密钥。</p>${inputField('email_client_id','应用客户端 ID',state.email?.microsoft_client_id||'','text',false,'id="email-client-id" maxlength="36" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"')}<button type="button" class="outline" data-action="email-save-client" data-id="${t.id}">保存客户端 ID</button><p><a href="#help/channels">查看完整登记步骤</a> · <a href="https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade" target="_blank" rel="noopener noreferrer">打开微软应用管理 ↗</a></p><small>登录授权与邮件发送使用系统网络；网络受限时请启用 VPN / Clash TUN。</small></details><div class="actions">${btn(t.email_authorized?'重新连接微软邮箱':'连接微软邮箱','email-connect',`data-id="${t.id}"`,'primary')}${t.email_authorized?btn('解除微软授权','email-disconnect',`data-id="${t.id}"`,'text-button danger'):''}</div><div id="email-auth-flow"></div>`:''}<div id="email-test-status" class="notice" role="status" hidden></div>`;
}
async function sendEmailVerification(id){
  const result=await api('/admin/targets/'+id+'/verify',{method:'POST'});
  if($('#verify-result'))$('#verify-result').innerHTML='验证邮件已加入队列。'+btn('查看投递详情','message',`data-id="${result.id}"`,'text-button');
  toast('请在收件邮箱中查看验证码');await refreshEmailTest(id);
}
async function refreshEmailTest(id){
  const box=$('#email-test-status');if(!box||$('#dialog').dataset.bindingId!==id)return;
  const r=await api('/admin/targets/'+id+'/verification-status');
  if(!box.isConnected||!r)return;
  box.hidden=false;
  const messages={queued:'验证邮件等待发送。',sending:'正在发送验证邮件…',retry:'连接暂时不可用，验证邮件正在自动重试。',accepted:'邮件服务已接受验证邮件，请在收件邮箱查看验证码（也请检查垃圾邮件）。',acknowledged:'已确认收到验证邮件。'};
  box.textContent=messages[r.state]||'验证邮件未完成：'+(r.last_error||statuses[r.state]?.[0]||r.state);
  box.className='notice'+(['failed','blocked','unknown','expired'].includes(r.state)?' warning':'');
}
function stopEmailWatch(){emailWatch++;}
function watchEmailBinding(id){
  const generation=++emailWatch;
  async function check(){
    if(generation!==emailWatch||!$('#dialog').open||$('#dialog').dataset.bindingId!==id)return;
    try{await refreshEmailTest(id);}catch(error){if(csrf)toast(error.message,true);}
    if(generation===emailWatch)setTimeout(check,2500);
  }
  check();
}
async function startEmailAuthorization(id){
  const flow=await api('/admin/targets/'+id+'/email/connect',{method:'POST'});
  if($('#dialog').dataset.bindingId!==id)return;
  const box=$('#email-auth-flow');box.dataset.flow=flow.id;
  box.innerHTML=`<div class="notice"><b>在微软页面登录并授权</b><p>请确认登录的是上方填写的发件邮箱。输入以下授权码，并允许文思邮差读取账号基本资料和发送邮件。</p><label>微软授权码<div class="copy-row"><input id="email-device-code" readonly value="${esc(flow.user_code)}">${btn('复制授权码','copy-value','data-copy="email-device-code"','outline')}</div></label><a class="primary email-login-link" href="${esc(flow.verification_uri)}" target="_blank" rel="noopener noreferrer">打开微软授权页面 ↗</a><p id="email-flow-status" role="status">等待授权，页面将自动检查。有效期至 ${date(flow.expires_at)}。</p>${btn('取消本次连接','email-cancel',`data-id="${id}" data-flow="${flow.id}"`,'text-button')}</div>`;
  const button=$('button[data-action="email-connect"]');if(button)button.disabled=true;
  async function poll(){
    if(!box.isConnected||box.dataset.flow!==flow.id||$('#dialog').dataset.bindingId!==id)return;
    try{
      const result=await api('/admin/targets/'+id+'/email/poll',{method:'POST',body:{flow_id:flow.id}});
      if(!box.isConnected||box.dataset.flow!==flow.id)return;
      if(result.state==='authorized'){
        delete box.dataset.flow;await refresh(false);bindTarget(id);await sendEmailVerification(id);return;
      }
      if(result.state!=='pending'){
        delete box.dataset.flow;$('#email-flow-status').textContent=result.detail;if(button)button.disabled=false;return;
      }
      if(result.detail)$('#email-flow-status').textContent=result.detail;
      setTimeout(poll,flow.interval*1000);
    }catch(error){if(box.isConnected){delete box.dataset.flow;$('#email-flow-status').textContent=error.message;if(button)button.disabled=false;}}
  }
  setTimeout(poll,flow.interval*1000);
}
async function emailAction(action,button){
  if(action==='email-credential-help'){const guide=$('#email-credential-guide');guide.hidden=!guide.hidden;button.setAttribute('aria-expanded',String(!guide.hidden));return;}
  const id=button.dataset.id;
  if(action==='email-save-client'){
    await api('/admin/email/microsoft',{method:'PUT',body:{client_id:$('#email-client-id').value.trim()}});await refresh(false);$('#email-ms-setup').open=false;toast('客户端 ID 已保存，可以连接微软邮箱');
  }
  if(action==='email-connect')await startEmailAuthorization(id);
  if(action==='email-cancel'){
    const box=$('#email-auth-flow');delete box.dataset.flow;
    await api('/admin/targets/'+id+'/email/cancel',{method:'POST',body:{flow_id:button.dataset.flow}});bindTarget(id);
  }
  if(action==='email-disconnect'){
    const choice=await askChoice('解除微软邮箱授权？','<p>此渠道将不能继续发送，并需要重新授权和验证收件邮箱。其他渠道不受影响。</p>',[['cancel','取消','outline'],['confirm','解除授权','danger']]);
    if(choice!=='confirm')return;
    await api('/admin/targets/'+id+'/email/disconnect',{method:'POST'});await refresh(false);bindTarget(id);
  }
}
document.addEventListener('change',event=>{
  const f=event.target.closest('#target-form');if(f?.dataset.kind!=='email')return;
  if(['provider','sender'].includes(event.target.name)){
    const password=f.elements.password;
    if(password.value||password.dataset.saved){delete password.dataset.saved;password.placeholder='邮箱或服务商已变化，请重新填写授权凭据';password.value='';}
  }
  if(['provider','sender'].includes(event.target.name)||event.target.id==='email-self')updateEmailForm();
});
document.addEventListener('input',event=>{
  if(event.target.id==='email-sender')updateEmailForm();
});
