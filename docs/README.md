# 文思邮差文档

Web 后台左侧“使用帮助”内置同一份操作手册，可按目录阅读、搜索关键词、复制代码示例或下载手册。无需连接外部文档网站。

- [快速上手](guide/01-quickstart.md)
- [上游 API 接入](guide/02-upstream.md)
- [渠道绑定与收件验证](guide/03-channels.md)
- [分发、时间与回执](guide/04-delivery.md)
- [局域网、公网与代理](guide/05-network.md)
- [常见问题与维护](guide/06-troubleshooting.md)
- [账号登录与密码管理](guide/07-account.md)

项目概览见 [README](../README.md)。维护与部署文档：

- [安装、常驻、备份与升级](DEPLOYMENT.md)
- [正式安装包与升级](INSTALLER.md)
- [安装与升级实测结果](INSTALLER-REVIEW.md)
- [Debian / Ubuntu 安装升级验收](LINUX-REVIEW.md)
- [测试范围与复现](TESTING.md)
- [发布流程](RELEASING.md)
- [历史版本变化](../CHANGELOG.md)
- [贡献指南](../CONTRIBUTING.md)
- [问题反馈](../SUPPORT.md)

维护文档时编辑 `docs/guide/*.md`。在已安装项目锁定依赖的虚拟环境中运行 `python scripts/build_help.py`，生成随程序打包的网页帮助及下载文件。使用 `python scripts/build_help.py --check` 检查是否同步；渲染复用 Python Markdown，不需要 Node.js 或外部 CDN。
