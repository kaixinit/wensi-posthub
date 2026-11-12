from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TargetInput(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["email", "wecom", "feishu", "dingtalk", "qq", "weixin", "webhook", "apprise"]
    config: dict = Field(default_factory=dict)


class MessageInput(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)
    target_ids: list[str] = Field(default_factory=list, max_length=50)
    profile_id: str | None = Field(default=None, description="仅管理员系统分发可用；上游使用自己的下游配置或 target_ids")
    request_id: str = Field(min_length=1, max_length=128)
    scheduled_at: datetime | None = None
    expires_at: datetime | None = None
    require_ack: bool | None = None
    ack_minutes: int | None = Field(default=None, ge=1, le=10080)

    @field_validator("scheduled_at", "expires_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("时间必须携带时区，例如 2026-11-11T18:00:00+08:00")
        return value


class HookMessageInput(MessageInput):
    title: str = Field(default="上游通知", min_length=1, max_length=200)
    request_id: str = Field(default_factory=lambda: "hook-" + uuid4().hex, min_length=1, max_length=128)


class ProfileInput(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    target_ids: list[str] = Field(min_length=1, max_length=50)
    require_ack: bool = False
    ack_minutes: int = Field(default=30, ge=1, le=10080)


class AppInput(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    target_ids: list[str] = Field(default_factory=list, max_length=50)
    default_profile_id: str | None = Field(default=None, max_length=64)
    require_ack: bool = False
    ack_minutes: int = Field(default=30, ge=1, le=10080)


class SettingsInput(StrictModel):
    public_base_url: str = Field(default="", max_length=300)
    alert_target_id: str = ""
    default_proxy: str | None = Field(default=None, max_length=500)


class ProxyInput(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    name: str = Field(min_length=1, max_length=80)
    url: str | None = Field(default=None, max_length=1000)
    scheme: Literal["http", "https", "socks5", "socks5h"] = "http"
    host: str = Field(default="", max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str = Field(default="", max_length=200)
    password: str | None = Field(default=None, max_length=300)
    revision: int | None = None
    expected_targets: list[str] | None = Field(default=None, max_length=1000)
    expected_default: bool = False

    @field_validator("name", "host", "url", mode="before")
    @classmethod
    def clean_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class NetworkRouteInput(StrictModel):
    mode: Literal["default", "system", "profile"]
    proxy_id: str = Field(default="", max_length=64)
    signature: str = Field(default="", max_length=64)
    previous_proxy_id: str = Field(default="", max_length=64)
    expected_targets: list[str] | None = Field(default=None, max_length=1000)


class NetworkTestInput(StrictModel):
    scope: Literal["default", "proxy", "target", "preview"] = "default"
    id: str = Field(default="", max_length=64)
    endpoint: Literal["qq", "weixin", "wecom", "feishu", "dingtalk"] = "qq"
    proxy: ProxyInput | None = None


class LoginInput(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    username: str = Field(default="admin", min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class SetupTestInput(StrictModel):
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class PasswordChangeInput(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)
    confirm_password: str = Field(min_length=12, max_length=256)


class MicrosoftEmailInput(StrictModel):
    client_id: str = Field(min_length=36, max_length=36)


class EmailFlowInput(StrictModel):
    flow_id: str = Field(min_length=32, max_length=32)


class CodeInput(StrictModel):
    code: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


class QQConversationInput(StrictModel):
    conversation_id: str = Field(min_length=1, max_length=64)


class EnabledInput(StrictModel):
    enabled: bool


class RetryInput(StrictModel):
    accept_duplicate_risk: bool = False


class ReceivedInput(StrictModel):
    confirmed: Literal[True]
