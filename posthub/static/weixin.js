'use strict';
let wxFlow = null;
function stopWeixinFlow(){if(wxFlow){clearTimeout(wxFlow.timer);wxFlow.controller.abort();wxFlow=null;}}
function wxCurrent(flow){return wxFlow===flow&&$('#dialog').open&&$('#dialog').dataset.bindingId===flow.id;}
function wxStatus(message,warning=false){const box=$('#wx-auth-status');if(box){box.textContent=message;box.className='notice'+(warning?' warning':'');}}
function wxSchedule(flow,delay=1800){clearTimeout(flow.timer);if(wxCurrent(flow))flow.timer=setTimeout(()=>pollWeixinFlow(flow),delay);}
function watchWeixinBinding(id){stopWeixinFlow();wxFlow={id,phase:'session',busy:false,controller:new AbortController()};updateWeixinBinding();wxSchedule(wxFlow);}
function weixinBindingView(t){return `<div class="stepper"><span>1 扫码绑定</span><span>2 测试发送</span><span>3 收件验证</span></div><h3>扫码绑定</h3><p>使用接收通知的本人微信扫码，并在手机上确认。授权完成后，向微信中的 ClawBot 发送一句“文思邮差测试”。</p><div id="wx-auth-status" class="notice" role="status">${t.bound?'已扫码绑定。':'尚未扫码。点击下方按钮生成二维码。'}</div>${btn(t.bound?'重新扫码绑定':'扫码绑定','wx-qr',`data-id="${t.id}" ${t.enabled?'':'disabled'}`,'outline wide')}<div id="qr-area"></div><hr><h3>收件会话与测试</h3><div id="wx-session-status" class="notice" role="status"></div>${btn('刷新微信会话','wx-sync',`data-id="${t.id}" ${t.bound&&t.enabled?'':'disabled'}`,'outline')}<div id="wx-test-status" class="notice" role="status" hidden></div>`;}
async function startWeixinQR(id){
  const t=state.targets.find(t=>t.id===id);if(!t)return;
  if(t.bound){const choice=await askChoice('重新扫码绑定？','<p>扫码确认新授权后，需要重新发送并填写收件验证码。尚未完成新授权时保留原有绑定。</p>',[['cancel','取消','outline'],['scan','开始扫码','primary']]);if(choice!=='scan')return;}
  stopWeixinFlow();const flow={id,phase:'qr',busy:false,controller:new AbortController()};wxFlow=flow;
  wxStatus('正在获取微信二维码…');$('#qr-area').innerHTML='';
  try{
    const r=await api('/admin/targets/'+id+'/weixin/qr',{method:'POST',signal:flow.controller.signal});if(!wxCurrent(flow))return;
    if(!/^data:image\/png;base64,/.test(r.image))throw new Error('二维码格式异常，请重新获取');
    $('#qr-area').innerHTML=`<div class="wx-qr-panel"><img class="qr-image" src="${esc(r.image)}" alt="微信绑定二维码"><p>请使用接收通知的微信扫码，状态会自动更新。</p><div id="wx-pin-box" hidden>${inputField('wx-pin','手机授权验证码','','text',false,'id="wx-pin" inputmode="numeric" pattern="[0-9]{4,12}" maxlength="12" autocomplete="one-time-code"')}<small>这是手机授权验证码，与下方的收件验证码不同。</small>${btn('提交手机授权验证码','wx-poll',`data-id="${id}"`,'primary wide')}</div>${btn('检查扫码状态','wx-poll',`data-id="${id}"`,'text-button')}</div>`;
    flow.phase='poll';wxStatus('等待扫码 · 扫码后请在手机上确认');wxSchedule(flow,100);
  }catch(error){if(!wxCurrent(flow)||error.name==='AbortError')return;flow.phase='error';wxStatus('获取二维码失败：'+error.message+'。请检查网络设置后点击“扫码绑定”重试。',true);}
}
async function pollWeixinFlow(flow,manual=false){
  if(!wxCurrent(flow)||flow.busy)return;
  if(flow.phase==='pin'&&!manual)return;
  if(flow.phase==='error')return;
  flow.busy=true;clearTimeout(flow.timer);
  try{
    if(['poll','pin'].includes(flow.phase)){
      const pin=manual?($('#wx-pin')?.value.trim()||''):'';
      if(flow.phase==='pin'&&!/^\d{4,12}$/.test(pin)){wxStatus('请填写手机上显示的 4–12 位授权验证码。',true);return;}
      const r=await api('/admin/targets/'+flow.id+'/weixin/poll',{method:'POST',body:{pin},signal:flow.controller.signal});if(!wxCurrent(flow))return;
      const labels={wait:'等待扫码 · 扫码后请在手机上确认',scaned:'已扫码，请在手机上确认授权。',scaned_but_redirect:'正在连接微信授权服务…',expired:'二维码已过期，请点击“扫码绑定”重新获取。',verify_code_blocked:'手机验证码尝试次数过多，请重新扫码绑定。',binded_redirect:'微信提示已有绑定，请检查原有连接后重新扫码。'};
      if(r.status==='confirmed'){
        flow.phase='session';$('#qr-area').innerHTML='';wxStatus('已扫码绑定。请向微信中的 ClawBot 发送一句“文思邮差测试”。');
        await refresh(false);if(!wxCurrent(flow))return;updateWeixinBinding();
      }else if(String(r.status).includes('need_verifycode')){
        flow.phase='pin';$('#wx-pin-box').hidden=false;wxStatus('请填写手机上显示的授权验证码。');$('#wx-pin').focus();
      }else if(['expired','verify_code_blocked','binded_redirect'].includes(r.status)){
        flow.phase='error';$('#qr-area').innerHTML='';wxStatus(labels[r.status],true);
      }else if(labels[r.status]){flow.phase='poll';wxStatus(labels[r.status]);}
      else{flow.phase='error';wxStatus('微信返回未识别的授权状态，请重新扫码。',true);}
    }else if(flow.phase==='session'){
      const t=state.targets.find(t=>t.id===flow.id);
      if(t?.enabled&&t.bound&&!t.has_session)await api('/admin/targets/'+flow.id+'/weixin/sync',{method:'POST',signal:flow.controller.signal});
      if(!wxCurrent(flow))return;await refresh(false);if(!wxCurrent(flow))return;updateWeixinBinding();
    }
  }catch(error){if(!wxCurrent(flow)||error.name==='AbortError')return;flow.phase='error';wxStatus('检查失败：'+error.message+'。可重新扫码或刷新微信会话。',true);}
  finally{flow.busy=false;if(wxCurrent(flow)&&['poll','session'].includes(flow.phase))wxSchedule(flow);}
}
function updateWeixinBinding(){
  const t=state?.targets.find(t=>t.id===$('#dialog').dataset.bindingId),box=$('#wx-session-status');if(!t||!box)return;
  box.textContent=!t.enabled?'渠道已停用。':!t.bound?'尚未扫码绑定，请先完成上方扫码。':!t.has_session?'已绑定，等待本人微信会话。请向 ClawBot 发一句消息，平台会自动获取。':`已取得收件会话：${t.config.address||t.name}。测试验证码将发送到此微信，其他会话不会自动替换收件人。`;
  const sync=$('[data-action="wx-sync"]');if(sync)sync.disabled=!t.bound||!t.enabled;
  const test=$('#wx-test-status'),r=t.wx_test;if(test){test.hidden=!r;test.textContent=!r?'':r.waiting_for_context?'等待微信会话：请在收件微信中向机器人发一条消息，验证码仍有效时会自动续发。':r.receipt_confirmed&&r.state!=='accepted'?'收件验证码已核验成功，该微信已确认收件。此前接口回执未能识别，原记录保留供核查。':r.state==='accepted'?(t.verified?'测试发送通过，收件目标已确认。':r.code_active?'微信接口已接受测试消息。请在微信中查看渠道名称并填回收件验证码。':'此前测试已发送，收件验证码已失效，请重新发送。'):['queued','sending','retry'].includes(r.state)?'测试消息正在发送，等待微信回执…':'测试发送未成功：'+(r.last_error||statuses[r.state]?.[0]||r.state);}
  updateReceiptVerification();
}
