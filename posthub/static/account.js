'use strict';
// Block navigation before it replaces an unfinished initial-password form.
document.addEventListener('click',event=>{
  const link=event.target.closest('a[href^="#"]');
  if(link&&state?.account?.must_change_password&&!['account','help'].includes(link.getAttribute('href').slice(1).split('/')[0])){
    event.preventDefault();toast('请先在账号设置中修改初始密码');
  }
});
function accountView(){
  const account=state.account||{username:'admin'};
  return heading('账号设置','管理管理员登录与密码')+`<section class="panel account-panel"><div class="account-summary"><span class="account-avatar">${icon('users')}</span><div><h2>${esc(account.username)}</h2><small>系统管理员 · 当前为单管理员账号</small></div></div>${account.must_change_password?'<div class="notice warning">你正在使用安装时生成的初始密码，请先设置自己的密码，再使用管理功能。</div>':''}<form id="account-password-form"><h2>修改登录密码</h2><label>当前密码<input name="current_password" type="password" autocomplete="current-password" required maxlength="256"></label><label>新密码<input id="account-new-password" name="new_password" aria-label="新密码" aria-describedby="new-password-hint" type="password" autocomplete="new-password" required minlength="12" maxlength="256"><small id="new-password-hint">12–256 个字符，建议使用长口令，避免与其他系统共用。</small></label><label>再次输入新密码<input id="account-confirm-password" name="confirm_password" type="password" autocomplete="new-password" required minlength="12" maxlength="256"></label><div class="password-tools">${btn('显示新密码','toggle-new-password','type="button" aria-pressed="false"','outline')}${btn('复制新密码','copy-value','type="button" data-copy="account-new-password"','outline')}</div><div class="notice">修改后所有管理端会话失效，需要重新登录。上游 API 密钥、已绑定渠道和正在进行的分发继续保留。</div><p id="account-error" class="error" role="alert"></p><button type="submit" class="primary">修改密码并重新登录</button></form></section>`;
}
async function changeAccountPassword(form){
  const data=Object.fromEntries(new FormData(form));
  const firstSetup=state.account?.must_change_password&&!state.apps.length;
  $('#account-error').textContent='';
  if(data.new_password!==data.confirm_password){$('#account-error').textContent='两次新密码不一致';return;}
  try{
    await api('/admin/account/password',{method:'POST',body:data});
    const password=data.new_password;
    if(firstSetup)history.replaceState(null,'','#setup');
    form.reset();showLogin();$('#login-form input[name=password]').value='';$('#login-error').textContent='';
    openDialog('密码已修改',`<p class="notice success">新密码已生效，请先保存，再重新登录。</p><label>本次设置的新密码<input id="changed-password" type="password" readonly autocomplete="off" aria-label="本次设置的新密码"></label><div class="password-tools">${btn('显示新密码','toggle-new-password','data-field="changed-password" aria-pressed="false"','outline')}${btn('复制新密码','copy-value','data-copy="changed-password"','outline')}</div><small>仅在本次修改后临时显示。关闭或刷新页面后清除，之后无法查看已保存的密码。</small><div class="actions">${btn('已保存，返回登录','close','','primary')}</div>`);
    $('#changed-password').value=password;
  }catch(error){if($('#account-error'))$('#account-error').textContent=error.message;else toast(error.message,true);}
  finally{data.current_password='';data.new_password='';data.confirm_password='';}
}

function toggleNewPassword(button){
  const fields=button.dataset.field?[document.getElementById(button.dataset.field)]:[$('#account-new-password'),$('#account-confirm-password')];
  const reveal=fields[0]?.type==='password';
  for(const field of fields)if(field)field.type=reveal?'text':'password';
  button.textContent=reveal?'隐藏新密码':'显示新密码';button.setAttribute('aria-pressed',String(reveal));
}
// Clear transient passwords before a page can enter the browser back/forward cache.
window.addEventListener('pagehide',()=>{
  for(const field of document.querySelectorAll('#account-password-form input,#changed-password'))field.value='';
});
