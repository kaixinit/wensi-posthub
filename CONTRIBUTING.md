# 贡献指南

请先阅读 [README](README.md)、[行为准则](CODE_OF_CONDUCT.md) 与 [MIT 许可证](LICENSE)。提交贡献时确认自己有权提供相关代码或资源，并同意以项目 MIT 许可提供原创贡献；这不要求转让版权。第三方内容需保留原许可，不能把不兼容或来源不明的代码直接改标 MIT。

## 提交问题或建议

先搜索已有 Issues，并按 [反馈说明](SUPPORT.md) 选择故障、功能建议或使用咨询。说明具体触发条件、预期和实际结果；安全问题按 [安全政策](SECURITY.md) 私下报告，不公开利用细节或真实凭据。

## 开发环境

在项目目录创建并激活 Python 3.11+ 虚拟环境，然后执行：

```bash
python -m pip install -r requirements.lock
python -m pip install -e '.[test]'
python -m pytest
python scripts/build_help.py --check
python scripts/check_release.py
```

Windows 激活方式为 `.\.venv\Scripts\Activate.ps1`；Linux 为 `source .venv/bin/activate`。也可直接使用虚拟环境中的 Python，避免脚本策略问题。测试方法和浏览器依赖见 [测试说明](docs/TESTING.md)。

使用单独的数据目录运行开发实例，例如 `python -m posthub init --data-dir dev-data` 和 `python -m posthub serve --data-dir dev-data --port 8877`。不要使用正式数据库、真实机器人凭据或正式收件人作为自动测试夹具。

## 修改约定

- 保持 Python 后端和现有原生 Web 界面，新增依赖应说明必要性；锁定依赖变化需重新验证安装。
- 用户文案统一使用“上游分发”“下游渠道”“系统分发”“网络通信”；技术兼容标识仍使用 `posthub`。
- 上游授权彼此独立；修改默认配置不能暗中扩大其他上游的发送范围。
- HTTP 202、渠道成功回执和实际收件确认必须区分。未知回执不能直接判为失败并自动重发。
- 密钥、授权码、完整专属推送 URL、消息正文和收件标识不应进入调试输出、公开截图或测试数据。
- 数据库结构变更需提供迁移及保留原密码、密钥、授权、队列和历史的验证；说明升级与回滚限制。

## 文档与测试

功能变更要更新对应操作手册和 `CHANGELOG.md` 的待发布部分。文档源在 `docs/guide/*.md`，执行 `python scripts/build_help.py` 同步 Web 帮助和可下载手册。不要只修改生成后的 `help-data.js`。

按影响范围运行测试：纯文档运行帮助同步和发布检查；改变 API、数据库或投递逻辑运行后端回归；改变交互流程运行对应浏览器验收。新增测试应覆盖可观察行为、权限或故障边界，不要求为每个文案写单元测试。

自定义 HTTP 测试使用回环端口，SMTP 使用本机测试接收器。真实外部发送须事先取得测试收件人的同意，并清楚标记测试编号；不能在 CI 中配置个人账号。

## Pull Request

围绕一个明确问题提交，使用仓库 PR 模板说明：问题、改变后的行为、验证方法、兼容影响和剩余限制。关联已有 Issue；有界面变化时附脱敏截图。不要提交虚拟环境、数据库、配置备份、构建输出和整套本机验收截图。

维护者核对功能、测试、许可来源和文档后决定合并；没有承诺固定响应时间。不主动向真实用户发送消息来证明修复。
