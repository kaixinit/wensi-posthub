"""Email presets, standard-library SMTP, and Microsoft-authorized sending."""
from __future__ import annotations

from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import formatdate
import json
import re
import smtplib
import ssl
import threading
from uuid import uuid4

import httpx
import msal

from .adapters import DeliveryError

PROVIDERS = {
    'qq': {'name':'QQ 邮箱', 'domains':['qq.com'], 'host':'smtp.qq.com', 'port':465, 'security':'ssl', 'credential':'邮箱 SMTP 授权码', 'hint':'先在 QQ 邮箱设置中开启 SMTP 服务并生成授权码。这里填写授权码，不是 QQ 登录密码。', 'help':'https://service.mail.qq.com/'},
    'gmail': {'name':'Gmail', 'domains':['gmail.com','googlemail.com'], 'host':'smtp.gmail.com', 'port':465, 'security':'ssl', 'credential':'Google 应用专用密码', 'hint':'开启两步验证后生成应用专用密码。部分组织或高级保护账号不提供此功能，需遵循账号管理员的设置。', 'help':'https://support.google.com/accounts/answer/185833?hl=zh-Hans'},
    'outlook': {'name':'Outlook / Hotmail', 'domains':['outlook.com','hotmail.com','live.com','msn.com'], 'credential':'微软网页登录授权', 'hint':'保存后连接微软账号，无需在文思邮差填写微软密码。使用微软官方邮件接口发送。', 'help':'https://support.microsoft.com/en-us/outlook/pop-imap-and-smtp-settings-for-outlook-com'},
    'custom': {'name':'其他邮箱 / 通用 SMTP', 'domains':[], 'credential':'SMTP 授权码 / 应用密码', 'hint':'填写邮箱服务商提供的 SMTP 参数；企业自定义域名可以手动选择实际服务商。', 'help':''},
}
INPUT_FIELDS = {'provider','host','port','username','password','sender','recipient','security','network_mode','proxy','proxy_id'}
SCOPES = ['https://graph.microsoft.com/Mail.Send','https://graph.microsoft.com/User.Read']
AUTHORITY = 'https://login.microsoftonline.com/common'
GRAPH = 'https://graph.microsoft.com/v1.0'


def provider_for(address):
    domain=address.rsplit('@',1)[-1].lower()
    return next((key for key,p in PROVIDERS.items() if domain in p['domains']), 'custom')


def normalize(config, previous=None):
    previous=previous or {}
    if set(config)-INPUT_FIELDS:raise ValueError('邮箱配置包含不支持的字段')
    for key,value in config.items():
        if key=='port':
            if not isinstance(value,int) or isinstance(value,bool):raise ValueError('SMTP 端口必须是数字')
        elif not isinstance(value,str):raise ValueError('邮箱连接信息必须填写文本')
    supplied=dict(config);result=previous|supplied
    for field in ('sender','recipient'):
        if field in result:result[field]=str(result[field]).strip()
    provider=result.get('provider','custom')
    if provider=='auto':provider=provider_for(result.get('sender',''))
    if provider not in PROVIDERS:raise ValueError('邮箱服务商无效')
    result['provider']=provider
    identity_changed=previous and (previous.get('sender','').casefold()!=result.get('sender','').casefold() or previous.get('provider','custom')!=provider or (provider=='custom' and previous.get('host')!=result.get('host')))
    if identity_changed:
        for key in list(result):
            if key.startswith('oauth_'):result.pop(key)
        if 'password' not in supplied:result.pop('password',None)
    if provider=='outlook':
        for key in ('host','port','security','username','password'):result.pop(key,None)
        result['network_mode']='system'
    else:
        for key in list(result):
            if key.startswith('oauth_'):result.pop(key)
        if provider in {'qq','gmail'}:
            result.update({k:PROVIDERS[provider][k] for k in ('host','port','security')})
            result['username']=result.get('sender','')
            password=result.get('password','').strip()
            if provider=='gmail':password=password.replace(' ','')
            if not password:raise ValueError('请填写发件邮箱的授权码 / 应用专用密码')
            result['password']=password
        else:
            result.setdefault('port',587);result.setdefault('security','starttls')
            if result.get('password') and not result.get('username'):result['username']=result.get('sender','')
    return result


def signature(config):
    """Provider labels alone do not invalidate a legacy SMTP verification."""
    if config.get('provider')=='outlook':
        return ('outlook',config.get('sender'),config.get('recipient'),config.get('oauth_account_id'),bool(config.get('oauth_cache')))
    return tuple(config.get(k,default) for k,default in [('host',''),('port',587),('security','starttls'),('username',''),('password',''),('sender',''),('recipient','')])


def smtp_send(config,message,delivery_id):
    mail=EmailMessage();mail['Subject']=message['title'];mail['From']=config['sender'];mail['To']=config['recipient']
    mail['Date']=formatdate(localtime=False);mail['Message-ID']=f'<{delivery_id}@posthub.local>'
    text=message['body']+ ('\n\n请确认收件：'+message['ack_url'] if message.get('ack_url') else '')
    mail.set_content(text)
    connection=None;data_started=False
    try:
        security=config.get('security','starttls');context=ssl.create_default_context()
        if security=='ssl':connection=smtplib.SMTP_SSL(config['host'],int(config.get('port',465)),timeout=25,context=context)
        else:
            connection=smtplib.SMTP(config['host'],int(config.get('port',587)),timeout=25)
            connection.ehlo()
            if security=='starttls':connection.starttls(context=context);connection.ehlo()
        if config.get('username'):connection.login(config['username'],config.get('password',''))
        data_started=True
        refused=connection.send_message(mail)
        if refused:raise smtplib.SMTPRecipientsRefused(refused)
        return mail['Message-ID']
    except smtplib.SMTPAuthenticationError:
        raise DeliveryError('邮箱授权失败，请检查 SMTP 服务、授权码或应用专用密码','blocked') from None
    except smtplib.SMTPRecipientsRefused as error:
        temporary=all(400<=int(value[0])<500 for value in error.recipients.values())
        raise DeliveryError('收件邮箱被邮件服务器拒绝，请核对地址或稍后重试','retry' if temporary else 'failed') from None
    except smtplib.SMTPResponseException as error:
        raise DeliveryError(f'邮件服务器拒绝本次操作（SMTP {error.smtp_code}）','retry' if 400<=error.smtp_code<500 else 'failed') from None
    except (smtplib.SMTPServerDisconnected,OSError):
        raise DeliveryError('邮件发送中连接中断，结果待核实' if data_started else '邮件服务器连接失败，等待自动重试','unknown' if data_started else 'retry') from None
    except smtplib.SMTPException:
        raise DeliveryError('邮件协议或加密配置不匹配，请检查服务商设置','unknown' if data_started else 'blocked') from None
    finally:
        if connection:
            try:connection.close()
            except OSError:pass


class EmailService:
    def __init__(self,hub):
        self.hub=hub;self.store=hub.store;self.flows={};self.lock=threading.RLock();self.http_cache={}

    def public_settings(self):
        return {'providers':PROVIDERS,'microsoft_client_id':self.store.setting('email_microsoft_client_id','')}

    def configure_microsoft(self,client_id):
        if not re.fullmatch(r'[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}',client_id):raise ValueError('请输入有效的微软应用客户端 ID（UUID），不是密码或客户端密钥')
        self.store.set_setting('email_microsoft_client_id',client_id)
        self.store.audit('email.microsoft.configure',client_id,self.hub.clock())
        return {'ok':True}

    def target(self,tid):
        t=self.hub.target(tid)
        if t['kind']!='email' or t['config'].get('provider')!='outlook':raise ValueError('该渠道不是微软授权邮箱')
        if not t['enabled']:raise ValueError('请先启用邮箱渠道')
        return t

    @contextmanager
    def application(self,config,client_id,serialized=''):
        cache=msal.SerializableTokenCache()
        if serialized:cache.deserialize(serialized)
        with httpx.Client(timeout=20,follow_redirects=False,trust_env=False) as http:
            # MSAL accepts a requests-compatible get/post client; httpx responses
            # provide status_code, headers, text and json without logging secrets.
            app=msal.PublicClientApplication(client_id,authority=AUTHORITY,token_cache=cache,http_client=http,http_cache=self.http_cache,enable_pii_log=False)
            yield app,cache,http

    def save_tokens(self,t,updates,*,rebind=False):
        with self.store.connect(True) as conn:
            row=conn.execute('SELECT * FROM targets WHERE id=? AND deleted IS NULL AND enabled=1',(t['id'],)).fetchone()
            if not row or row['revision']!=t['revision']:raise ValueError('渠道配置已变化，请重新连接邮箱')
            current=self.store.decrypt(row['config']);current.update(updates)
            conn.execute('UPDATE targets SET config=?,revision=revision+?,verified=CASE WHEN ? THEN 0 ELSE verified END,verify_hash=CASE WHEN ? THEN NULL ELSE verify_hash END WHERE id=?',(self.store.encrypt(current),int(rebind),rebind,rebind,t['id']))

    def start(self,tid):
        with self.lock:
            now=self.hub.clock();self.flows={k:v for k,v in self.flows.items() if v['expires']>now}
            t=self.target(tid);client_id=self.store.setting('email_microsoft_client_id','')
            if not client_id:raise ValueError('请先在邮箱配置中填写微软应用客户端 ID')
            if self.store.limited('email.oauth.start:'+tid,5,300,now):raise ValueError('授权请求过于频繁，请稍后再试')
            try:
                with self.application(t['config'],client_id) as (app,cache,http):flow=app.initiate_device_flow(scopes=SCOPES)
            except (httpx.HTTPError,ValueError,RuntimeError):raise ValueError('无法连接微软授权服务，请检查网络、客户端 ID 与公共客户端设置') from None
            if not isinstance(flow,dict) or not flow.get('user_code') or not flow.get('device_code'):raise ValueError('微软未创建授权请求，请检查应用支持的账号类型和公共客户端设置')
            record={'id':uuid4().hex,'revision':t['revision'],'client_id':client_id,'flow':flow,'expires':now+min(int(flow.get('expires_in',900)),900),'next_at':now+max(5,int(flow.get('interval',5))),'sender':t['config']['sender']}
            self.flows[tid]=record
            return {'id':record['id'],'user_code':flow['user_code'],'verification_uri':'https://microsoft.com/devicelogin','expires_at':record['expires'],'interval':max(5,int(flow.get('interval',5)))}

    def poll(self,tid,flow_id):
        with self.lock:
            t=self.target(tid);record=self.flows.get(tid);now=self.hub.clock()
            if not record or record['id']!=flow_id:raise ValueError('授权会话已关闭或服务已重启，请重新连接')
            if record['revision']!=t['revision']:self.flows.pop(tid,None);raise ValueError('渠道配置已变化，请重新授权')
            if now>=record['expires']:self.flows.pop(tid,None);return {'state':'expired','detail':'授权已过期，请重新连接'}
            if now<record['next_at']:return {'state':'pending'}
            record['next_at']=now+max(5,int(record['flow'].get('interval',5)))
            try:
                with self.application(t['config'],record['client_id'],record.get('cache','')) as (app,cache,http):
                    result=record.get('result') or app.acquire_token_by_device_flow(record['flow'],exit_condition=lambda _:True)
                    if result.get('error') in {'authorization_pending','slow_down'}:
                        record['next_at']=now+max(5,int(record['flow'].get('interval',5)));return {'state':'pending'}
                    if 'access_token' not in result:
                        self.flows.pop(tid,None);return {'state':'denied','detail':'授权未完成或被拒绝，请重新连接'}
                    record.update(result={'access_token':result['access_token']},cache=cache.serialize())
                    profile=http.get(GRAPH+'/me',params={'$select':'id,mail,userPrincipalName'},headers={'Authorization':'Bearer '+result['access_token']})
                    if profile.status_code!=200:raise ValueError('无法确认微软邮箱身份，请检查 User.Read 权限后重新连接')
                    identity=profile.json();addresses={str(identity.get(k,'')).casefold() for k in ('mail','userPrincipalName')}
                    if t['config']['sender'].casefold() not in addresses:raise ValueError('授权账号与填写的发件邮箱不一致，请填写该账号的主邮箱地址后重新连接')
                    accounts=app.get_accounts()
                    if len(accounts)!=1:raise ValueError('无法确认唯一的授权账号，请重新连接')
                    self.save_tokens(t,{'oauth_cache':cache.serialize(),'oauth_client_id':record['client_id'],'oauth_account_id':accounts[0]['home_account_id'],'oauth_error':False},rebind=True)
            except httpx.HTTPError:return {'state':'pending','detail':'授权服务暂时无法连接，将继续检查'}
            except (ValueError,KeyError,TypeError):
                self.flows.pop(tid,None);raise ValueError('授权账号不匹配或权限不完整。请使用填写的主邮箱，确认 Mail.Send、User.Read 权限后重新连接') from None
            self.flows.pop(tid,None);self.store.audit('email.microsoft.connected',tid,now)
            return {'state':'authorized'}

    def cancel(self,tid,flow_id):
        with self.lock:
            if self.flows.get(tid,{}).get('id')==flow_id:self.flows.pop(tid,None)
        return {'ok':True}

    def disconnect(self,tid):
        with self.lock:
            t=self.target(tid);self.flows.pop(tid,None)
            self.save_tokens(t,{'oauth_cache':'','oauth_client_id':'','oauth_account_id':'','oauth_error':False},rebind=True)
            self.store.audit('email.microsoft.disconnected',tid,self.hub.clock())
        return {'ok':True}

    def send(self,config,message,delivery_id):
        if config.get('provider')!='outlook':return smtp_send(config,message,delivery_id)
        with self.lock:
            t=self.target(config['_email_target_id'])
            if t['revision']!=config['_email_revision']:raise DeliveryError('邮箱配置已变化，请重新检查','blocked')
            c=t['config']
            if not c.get('oauth_cache'):raise DeliveryError('请先连接微软邮箱并完成收件验证','blocked')
            try:
                with self.application(c,c['oauth_client_id'],c['oauth_cache']) as (app,cache,http):
                    accounts=[a for a in app.get_accounts() if a['home_account_id']==c['oauth_account_id']]
                    result=app.acquire_token_silent_with_error(SCOPES,account=accounts[0]) if accounts else None
                    if not result or 'access_token' not in result:
                        if result and result.get('error') in {'temporarily_unavailable','server_error'}:raise DeliveryError('微软授权服务暂时不可用，等待自动重试','retry')
                        self.save_tokens(t,{'oauth_error':True},rebind=True)
                        raise DeliveryError('微软邮箱授权已失效，请重新连接并验证收件邮箱','blocked')
                    if cache.has_state_changed:self.save_tokens(t,{'oauth_cache':cache.serialize(),'oauth_error':False})
                    text=message['body']+ ('\n\n请确认收件：'+message['ack_url'] if message.get('ack_url') else '')
                    payload={'message':{'subject':message['title'],'body':{'contentType':'Text','content':text},'toRecipients':[{'emailAddress':{'address':c['recipient']}}],'internetMessageHeaders':[{'name':'x-posthub-delivery-id','value':delivery_id}]},'saveToSentItems':True}
                    try:response=http.post(GRAPH+'/me/sendMail',json=payload,headers={'Authorization':'Bearer '+result['access_token']})
                    except (httpx.ConnectError,httpx.ConnectTimeout,httpx.PoolTimeout):raise DeliveryError('微软邮件服务连接失败，等待自动重试','retry') from None
                    except httpx.HTTPError:raise DeliveryError('微软邮件请求已发出但回执不明，请核实收件','unknown') from None
                    if response.status_code==202:return response.headers.get('request-id','')
                    if response.status_code==429:raise DeliveryError('微软邮箱发送限流，等待自动重试','retry')
                    if response.status_code in {401,403}:
                        self.save_tokens(t,{'oauth_error':True},rebind=True);raise DeliveryError('微软邮箱授权或发送权限失效，请重新连接','blocked')
                    raise DeliveryError(f'微软邮件接口未接受请求（HTTP {response.status_code}）','unknown' if response.status_code>=500 else 'failed')
            except httpx.HTTPError:raise DeliveryError('微软授权服务连接失败，等待自动重试','retry') from None
            except (ValueError,KeyError,TypeError):raise DeliveryError('微软授权配置无效，请重新连接邮箱','blocked') from None
