import re
import time

import pytest

from posthub.core import Hub
from posthub.models import TargetInput
from posthub.storage import Store, initialize


class Clock:
    def __init__(self):
        self.value = float(round(time.time()))
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += seconds


@pytest.fixture
def environment(tmp_path):
    initialize(tmp_path, "test-password-12345")
    return Store(tmp_path)


@pytest.fixture
def hub(environment):
    clock, calls = Clock(), []
    def send(kind, config, message, delivery_id):
        calls.append((kind, config, message, delivery_id))
        return "fixture-provider-id"
    obj = Hub(environment, sender=send, clock=clock)
    obj.calls = calls
    return obj


@pytest.fixture
def target(hub):
    return make_target(hub)


def make_target(hub, name="值班目标", config=None, kind="webhook"):
    item = hub.save_target(TargetInput(name=name, kind=kind, config=config or {"url": "https://example.test/receive"}))
    hub.verify_start(item["id"])
    hub.tick()
    code = re.search(r"验证码：(\d{6})", hub.calls[-1][2]["body"])[1]
    hub.verify_finish(item["id"], code)
    hub.calls.clear()
    return item["id"]
