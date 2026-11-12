'use strict';
function deliveryAdvice(d,m){
  const t=state.targets.find(t=>t.id===d.target_id),alive=Date.now()/1000-state.heartbeat<180;
  let title='',text='',actions='';
  const test=t?btn('检测此渠道通信','recover-network',`data-id="${t.id}" data-message="${m.id}"`,'outline'):'';
  const bind=t?btn('检查授权与收件配置','recover-bind',`data-id="${t.id}" data-message="${m.id}"`,'outline'):'';
  if(d.waiting_for_context){title='下一步：在微信向机器人发一句话';text='微信暂无法准备发送。请用绑定的收件微信发一句消息；平台收到新会话后会自动补发，到投递截止时停止。无需重新绑定或手动重发，已成功的渠道不会重复发送。';}
  else if(d.state==='retry'){title='等待自动重试，无需重新提交';text=`下次尝试：${date(d.next_at)}。投递截止：${date(m.expires)}。连接或服务恢复后继续发送，仅重试此未成功渠道。${alive?'':'当前分发服务心跳异常，请检查部署机器上的服务是否运行。'}`;actions=test;}
  else if(d.state==='unknown'){title='先核实实际收件，暂不要重发';text='请求可能已送达，但没有获得明确回执。先检查对应聊天或邮箱（包括垃圾邮件）；收到后登记确认。确定未收到且愿意承担重复风险时，再重新发送。';}
  else if(['failed','blocked'].includes(d.state)){title='先修复原因，再重试此渠道';text=!t?'渠道已删除，原任务不能继续。请为上游配置新的收件渠道。':!t.enabled?'渠道已停用，请到下游渠道启用后再回来重试。':!t.verified?'当前收件目标未验证，请完成授权和收件验证码验证后再回来重试。':t.revision!==d.revision?'渠道配置已改变，请核对当前收件人，再明确重试此渠道。':/授权|token|credential|权限|认证|secret|401|403|过期/i.test(d.last_error||'')?'请检查账号授权、凭据和发送权限；修复后再重试。网络可达不能证明授权有效。':'请先查看下方具体原因；可检测网络，或检查授权、发送额度和收件配置。修复后再重试此渠道。';actions=test+bind;}
  else if(d.state==='accepted'){title='渠道已接受，等待实际收件核对';text='接口返回成功回执，并不等于收件人已读。请核对正确的会话或邮箱（包括垃圾邮件），实际收到后可登记确认；无需重复发送。';}
  else if(d.state==='acknowledged'){title='已登记实际收到';text='此渠道投递已完成，不会自动重发。';}
  else if(['expired','cancelled'].includes(d.state)){title=d.state==='expired'?'已超过投递截止时间':'该投递已取消';text='平台不会继续重试。若业务仍然有效，请核对内容后作为新消息发送，不要反复提交旧业务编号。';}
  else if(d.state==='queued'){title=m.scheduled>Date.now()/1000?'等待计划发送时间':alive?'已入队，等待分发':'分发服务需要检查';text=m.scheduled>Date.now()/1000?`将在 ${date(m.scheduled)} 后安排发送。`:alive?'通常会很快发送，请刷新状态查看结果，无需重新提交。':'请检查部署机器上的文思邮差服务及日志，恢复服务后会处理截止时间内的任务。不要重复提交上游消息。';}
  else if(d.state==='sending'){title='请求正在发送';text='请稍后刷新结果，避免手动重复提交。';}
  return title?`<div class="recovery-advice"><h3>${title}</h3><p>${text}</p>${actions?`<div class="actions setup-actions">${actions}</div>`:''}</div>`:'';
}
async function recoveryAction(a,b){
  if(a==='recover-network'){
    openDialog('检测渠道通信',`<p>仅检查当前连接是否可达，不发送通知，也不改变投递结果。</p><div id="recovery-network-result" role="status"></div><div class="actions">${btn('返回投递详情','message',`data-id="${b.dataset.message}"`,'outline')}</div>`);
    await runNetTest({scope:'target',id:b.dataset.id},'#recovery-network-result');return;
  }
  if(a==='recover-bind'){
    bindTarget(b.dataset.id);
    $('.dialog-content').insertAdjacentHTML('afterbegin',`<div class="notice">检查或修复后回到原消息处理。${btn('返回投递详情','recover-return',`data-id="${b.dataset.message}"`,'text-button')}</div>`);return;
  }
  if(a==='recover-return'){if(await requestCloseDialog())await showMessage(b.dataset.id);}
}
