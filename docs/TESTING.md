# 测试范围与复现

测试结果必须区分本机、模拟渠道、真实账号与 GitHub 远端。当前程序版本为 0.1.25；当前验收摘要见 [TEST-REPORT](../TEST-REPORT.md)，历史原始证据保留在本地工作区，不把历史通过结果当作所有环境的承诺。

## 已有证据

| 范围 | 结果与限制 |
| --- | --- |
| Windows 11 / Python 3.11 后端 | 251 项测试通过，覆盖鉴权、授权、持久化、幂等、自动重试、重启、渠道及向导 |
| 本机 Chrome | 接入向导、首次改密、渠道返回、故障处理、Web 帮助及 390px 手机宽度已验收 |
| 真实 QQ、微信、QQ 邮箱 | 编号 1112-125812，A 向导试发、B HTTP API；用户确认各一条无重复，共 6 次收件 |
| 丢失响应与重复提交 | 本机接收端检查试发响应丢失后重试和 API 同编号重复请求，未新增发送 |
| 微信新会话恢复 | 自动续发与取消逻辑有模拟/浏览器回归；历史真实新会话补发已由用户确认 |
| HTTP / SMTP / HTTP代理 / SOCKS5 | 本机协议端点测试；不等同于每个第三方账号或远端代理节点可用 |
| Clash 规则与远端节点 | 历史规则模式三渠道正常；远端美国节点 QQ/微信收到，SMTP 超时，不标为邮件成功 |
| Gmail、微软、企业微信、钉钉、飞书、扩展通知 | 已有适配器或流程测试，尚无全部真实账号的收件验收 |
| Debian 12 / Python 3.11、Ubuntu 24.04 / Python 3.12 | 独立 Linux 虚拟机正式包安装、0.1.19 / 0.1.24 升级、重装、数据保留与队列续发通过，见 [专项报告](LINUX-REVIEW.md) |
| Python 3.13 / GitHub Actions | 已提供矩阵；当前未取得远端通过记录 |
| Docker | 已提供配置，当前未做镜像构建和容器运行验收 |

真实收发使用隔离队列及明确授权的收件人；常规测试不读取正式数据、不开启真实外部渠道发送。浏览器测试验证布局与部分键盘操作，不等于完整无障碍认证。

## 后端测试

正式安装包另有 [安装与升级验收](INSTALLER-REVIEW.md)：256 项回归通过（含 5 项安装保护），实际验证 Windows 全新安装、0.1.19 / 0.1.24 升级和同版本重装；[Linux 同类验收](LINUX-REVIEW.md) 也已在 Debian 12 与 Ubuntu 24.04 虚拟机通过。这里与上表历史首次接入收件证据分别记录。

在项目目录、已激活的 Python 虚拟环境中：

```bash
python -m pip install -r requirements.lock
python -m pip install -e '.[test]'
python -m pytest --junitxml=artifacts/tests.xml
```

本机网络测试会监听回环端口，进程测试会创建临时子进程。沙箱禁止这些能力时应在可信本机测试环境运行，不应将权限限制误报为产品通过。

## 文档与发布文件检查

```bash
python scripts/build_help.py --check
python scripts/check_release.py
```

检查 Markdown 相对链接和锚点、图片、JSON 示例、Issue 表单结构、工作流结构、版本一致性及来源清单。离线检查不会替代 GitHub 服务端解析，也不会验证所有外部链接或证明无安全漏洞。

修改 `docs/guide/*.md` 后先运行 `python scripts/build_help.py`；检查模式不修改文件。字体文字有变化时可使用 `python tools/build_ui_font.py`，开发环境额外安装 `fonttools brotli`。

## 浏览器接入验收

```bash
python -m pip install playwright
python -m playwright install chromium
mkdir -p artifacts/browser-work
python tests/browser_onboarding.py --browser /path/to/chrome-or-chromium --work-dir artifacts/browser-work --artifacts artifacts/onboarding
```

Windows 使用 PowerShell 的 `New-Item -ItemType Directory -Force artifacts/browser-work`，浏览器参数可指向 `C:/Program Files/Google/Chrome/Application/chrome.exe`。脚本创建临时数据库和本机 HTTP 收件端，不使用正式账号。它检查四步向导、验证码、显式收件确认、独立上游授权、响应丢失去重及故障指引。

可同样运行 `browser_account.py`、`browser_app_channels.py`、`browser_help.py` 和 `browser_weixin_recovery.py`，均位于 `tests/`，参数使用 `--help` 查看。不要把历史需要接入现有服务的验收脚本直接指向生产环境。

## GitHub Actions

[工作流](../.github/workflows/test.yml) 包含 Windows / Ubuntu 的 Python 3.11–3.13、Debian Bookworm 容器、Ubuntu 浏览器与独立 wheel 安装检查。测试使用最低必要的只读仓库权限，不需要真实 QQ、微信或邮箱凭据。测试报告作为临时 Actions artifact 保存，不提交到源码历史。

GitHub Actions 结果以仓库中对应提交的实际运行记录为准，本地测试与历史报告不能替代远端结果。实际发布前应检查目标提交对应的所有任务，失败时保留日志并修复；不能仅添加绿色徽章或手动填“通过”。
