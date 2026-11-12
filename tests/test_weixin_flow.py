import re
import httpx
import pytest

from posthub import adapters
from posthub.bindings import Bindings
from posthub.core import HubError
from posthub.models import TargetInput


def test_reauthorizing_weixin_revokes_old_receipt_code(hub, monkeypatch):
    target = hub.save_target(TargetInput(name='微信收件一', kind='weixin'))['id']
    binding = Bindings(hub)
    account = ['owner-one']
    def handle(request):
        if request.url.path.endswith('get_bot_qrcode'):
            return httpx.Response(200, json={'qrcode':account[0], 'qrcode_img_content':'https://example.test/scan'})
        return httpx.Response(200, json={'status':'confirmed', 'bot_token':'test-token', 'ilink_user_id':account[0]})
    monkeypatch.setattr('posthub.bindings.client', lambda *a, **k: httpx.Client(transport=httpx.MockTransport(handle)))
    binding.qr_start(target); binding.qr_poll(target)
    binding._save(hub.target(target), {'context_token':'test-context'})
    hub.verify_start(target); hub.tick()
    assert '微信 收件目标：微信收件一' in hub.calls[-1][2]['body']
    code = re.search(r'验证码：(\d{6})',hub.calls[-1][2]['body'])[1]
    account[0] = 'owner-two'
    binding.qr_start(target); binding.qr_poll(target)
    fresh = hub.target(target)
    assert fresh['config']['address'] == 'owner-two'
    assert not fresh['verified'] and not fresh['verify_hash'] and not fresh['config']['context_token']
    with pytest.raises(HubError, match='失效'):
        hub.verify_finish(target, code)


@pytest.mark.parametrize('invalidate', ['new_qr','disabled'])
def test_late_weixin_confirmation_cannot_replace_current_state(hub, monkeypatch, invalidate):
    target = hub.save_target(TargetInput(name='微信一',kind='weixin'))['id']
    binding = Bindings(hub)
    binding._save(hub.target(target), {'qr_code':'old-qr','qr_expires':hub.clock()+480})
    def handle(request):
        if invalidate == 'disabled':
            hub.set_enabled('targets', target, False)
        else:
            binding._save(hub.target(target), {'qr_code':'new-qr'})
        return httpx.Response(200, json={'status':'confirmed','bot_token':'stale-token','ilink_user_id':'stale-owner'})
    monkeypatch.setattr('posthub.bindings.client', lambda *a, **k:httpx.Client(transport=httpx.MockTransport(handle)))
    with pytest.raises(HubError, match='配置已变化'):
        binding.qr_poll(target)
    assert not hub.target(target)['config'].get('bot_token')


def test_received_code_confirms_delivery_separately_from_provider_result(hub):
    target=hub.save_target(TargetInput(name='微信回执',kind='weixin'))['id']
    binding=Bindings(hub)
    binding._save(hub.target(target),{'bot_token':'t','address':'owner','context_token':'c'})
    original=hub.sender
    def uncertain(*args):
        original(*args)
        raise adapters.DeliveryError('旧版本未识别回执','unknown')
    hub.sender=uncertain
    hub.verify_start(target);hub.tick()
    code=re.search(r'验证码：(\d{6})',hub.calls[-1][2]['body'])[1]
    assert hub.qq_test_status(target)['state']=='unknown'
    assert not hub.qq_test_status(target)['receipt_confirmed']
    hub.verify_finish(target,code)
    assert hub.qq_test_status(target)['receipt_confirmed']
    assert hub.qq_test_status(target)['state']=='unknown'
