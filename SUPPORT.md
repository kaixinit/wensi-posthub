# 使用帮助与问题反馈

先查看 [快速上手](docs/guide/01-quickstart.md)、[故障处理](docs/guide/06-troubleshooting.md) 和 [已验证范围](docs/TESTING.md)。Web 内“使用帮助”可离线阅读同一份手册。

## 选择合适的入口

在 [GitHub 问题反馈](https://github.com/kaixinit/wensi-posthub/issues/new/choose) 中选择：

- **故障报告**：功能没有按说明工作，附复现步骤与实际投递状态。
- **功能建议**：描述使用场景、目前遇到的困难和期望结果。
- **使用咨询**：安装、渠道权限、地址配置等操作问题。

项目仓库为 [kaixinit/wensi-posthub](https://github.com/kaixinit/wensi-posthub)。公开问题通过 Issues 处理；安全问题使用下方私密报告流程。项目未预设支持邮箱。

## 故障报告需要什么

提供程序版本、操作系统与 Python 版本、安装方式、浏览器、渠道类型、复现步骤、预期与实际结果。投递问题附状态、发生时间及无敏感内容的错误摘要，说明收件人是否确实收到、是否重复。

网络问题说明是系统网络还是应用内代理，并区分“网络可达”“账号已授权”“渠道已接受”“收件人已确认”。不需要公开代理密码或完整地址。

请先删去截图、日志中的管理密码、API 密钥、完整专属推送地址、AppSecret、授权码、二维码、个人收件标识和业务消息。不要上传数据库或 `config.json`。代码与 JSON 使用 Markdown 代码块，必要时用虚构数据复现。

## 敏感问题

涉及越权、密钥泄露或其他安全风险时按 [SECURITY.md](SECURITY.md) 报告。行为准则问题按 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) 处理。公开 Issue 中可以只请求私密联系方式，不发布敏感细节。

这是早期项目，支持按维护者可用时间处理；未承诺 SLA 或所有第三方平台的可用性。
