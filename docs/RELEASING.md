# 发布流程

源码检查、安装验证和实际远端发布是不同阶段。准备好文件不等于已经推送到 GitHub，也不等于跨平台 Actions 已通过。

## 本地发布检查

在项目虚拟环境运行：

```bash
python scripts/build_help.py --check
python scripts/check_release.py --json artifacts/release-check.json
python -m pytest --junitxml=artifacts/tests.xml
```

界面或帮助变更时按 [测试说明](TESTING.md) 运行对应浏览器验收。核对 README 的当前行为、渠道矩阵、测试范围和 CHANGELOG；不在发布说明中复制真实凭据或用户业务消息。

## 干净源码包

```bash
python scripts/export_source.py --output dist/posthub-source.zip
```

输出必须使用尚不存在的文件名。脚本先执行发布检查，再从明确的文件清单导出源码与 `.github`、文档、部署脚本、测试，生成 SHA-256 文件。排除本机 `artifacts/`、构建缓存、运行数据库与配置，不包含旧版本安装包。

解压后再运行检查，确认所有相对链接、截图和必需文件都在包内。上传仓库使用干净源码内容；wheel、源码压缩包及校验文件放在 GitHub Release 附件，不提交到源码历史。CI 证据使用 Actions artifacts 保存。

## wheel 安装验证

```bash
python -m pip wheel . --no-deps --wheel-dir dist
```

在另一个全新虚拟环境安装 `requirements.lock` 和生成的 wheel。使用该环境的 Python 从源码目录之外执行 `scripts/smoke_installed.py`；检查会拒绝直接从源码导入，验证 MIT 元数据、静态资源、初始化、登录、上游 API 和同编号去重。测试不配置真实收件人。

如果更改 Docker 构建上下文，检查 README、LICENSE 等包元数据引用的文件也已 COPY；静态检查不能替代实际构建镜像。

## 正式安装包

正式安装包由 `python scripts/export_installer.py --wheel dist/posthub-0.1.25-py3-none-any.whl --output dist/posthub-installer.zip` 生成。使用已经按 `requirements.lock` 安装的构建环境；脚本按依赖元数据提取 Windows/Linux、Python 3.11–3.13 运行依赖，排除测试依赖，并核对 wheel 中所有运行文件与源码一致。

运行 `python tests/installer_acceptance.py --package dist/posthub-installer.zip --work-dir artifacts/install-work --artifacts artifacts/install-check` 验证全新安装和同版本重装。`--work-dir` 必须不存在；可多次添加 `--old-wheel 旧版.whl`，用真实旧程序创建数据后测试升级。此验收只使用临时账号和本机 HTTP 接收器。

安装包只用于 Python Web 服务部署，不是内置 Python 的离线 EXE；Linux 与 Docker 验收状态仍须分别报告。

## GitHub 仓库设置

上传前确认仓库归属与可见性，使用干净源码副本建立远端。取得项目所有者的发布授权后，再创建仓库、推送代码和发布 Release。

1. 确认根目录 MIT `LICENSE`、第三方声明、README、贡献指南、行为准则、安全政策和 Issue/PR 模板完整。
2. 在实际仓库启用 Issues 与 Actions；按 `SECURITY.md` 启用私密漏洞报告，核实 Security 页面入口确实出现。文件不能自动开启仓库设置。
3. 推送候选提交后等待 Windows、Ubuntu、Debian、浏览器及 wheel 安装任务实际通过；不要把本机结果填写成远端结果。
4. 公布可用的维护者联系方式，核对模板和支持入口能正常使用；没有时按文档的“请求私密联系方式”流程处理。
5. 核对提交版本、标签、更新日志及安装包校验值，再手动创建 Release。仓库没有授权前，本地脚本不会自动推送、打远端标签或发布。

本轮本地结果与未完成项见 [发布准备验收](RELEASE-REVIEW.md)。
