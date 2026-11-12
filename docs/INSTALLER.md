# 文思邮差正式安装包

程序版本 0.1.25。这是 Python Web 服务的在线安装包，支持 Windows 11、Debian / Ubuntu 的部署方式。需要预先安装 Python 3.11+；Linux 还需 python3-venv。首次安装需要联网下载依赖，包内不附带 Python 解释器。

安装包保留一份完整 wheel（含全部渠道、字体、图标和 Web 帮助）、运行依赖清单、安装/升级/启动脚本与许可。没有测试脚本、截图、开发缓存、用户数据库或重复的程序源码。运行时不需要 Node.js、浏览器自动化或 pytest。

## Windows 11 首次安装

解压到固定目录，在包含 manage.py 的目录打开 PowerShell：

```powershell
python --version
powershell -ExecutionPolicy Bypass -File .\install.ps1
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

也可直接执行 `python manage.py install` 和 `python manage.py serve`。执行策略参数仅影响该次进程，不修改系统策略。

默认数据目录为安装目录下的 data；安装时显示独立初始密码，账号 admin。打开 http://127.0.0.1:8787，首次改密后按接入向导操作。已有数据不会生成新密码。

## Debian / Ubuntu 首次安装

Debian 12 / Ubuntu 24.04 可先安装系统提供的 Python 组件，再进入解压后的 posthub 目录。系统 Python 必须至少为 3.11：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip
python3 --version
bash install.sh
bash start.sh
```

解释器不在默认位置时使用 `PYTHON_BIN=/path/to/python3.11 bash install.sh`。安装创建独立 .venv，不修改系统 Python 包。Debian 12 / Python 3.11 与 Ubuntu 24.04 / Python 3.12 已在独立 Linux 虚拟机通过安装、旧版升级及队列续发验收；结果见源码仓库 docs/LINUX-REVIEW.md。

## 升级已有安装

1. 在旧服务中查看/确认实际数据目录，里面必须同时有 config.json 和 posthub.sqlite3。旧源码版默认在其 data 目录；使用过自定义目录时，以原启动参数为准。
2. 停止旧服务。新包解压到新目录，保留旧程序目录供必要时回退。首次切换到新包时明确指定原数据目录。
3. Windows 执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\upgrade.ps1 -DataDir 'D:\PostHub\data'
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

Linux 执行：

```bash
bash upgrade.sh --data-dir /var/lib/posthub
bash start.sh
```

路径是示例，请换成原数据目录。以后在同一安装目录升级可直接运行升级脚本，读取 installation.json 中记住的目录；也支持 POSTHUB_DATA_DIR 环境变量（已保存目录优先）。

升级会校验安装包、拒绝正在运行的服务，先在原数据目录的 backups 子目录保存数据库与 config.json 配套备份，再安装程序、执行数据库迁移。账号密码、解密密钥、上游 API 密钥和数据沿用；备份含敏感配置，应按正式数据保护。

安装器不会自动停止/启动服务，不会自动向正式渠道发测试消息。启动后未过期的待发任务会按原规则继续；成功任务不会因升级而自动重发。同版本重复安装也会替换程序文件，并先备份已有数据。

数据目录内容不配套、数据库损坏或 schema 高于当前程序时拒绝升级。安装步骤失败时停止并保留备份；不保证 pip 安装中断后旧环境仍可启动，应排除错误重试或回退。

## 访问地址与常驻

Windows 局域网访问：`powershell -ExecutionPolicy Bypass -File .\start.ps1 -HostAddress 0.0.0.0 -Port 8787`。

Linux 局域网访问：`bash start.sh --host 0.0.0.0 --port 8787`。

使用实际 IP 访问并按需放行防火墙。公网使用 HTTPS 入口；访问地址、出站网络与可选代理的区别见 Web 中“使用帮助 → 网络通信”。

任务计划程序或 systemd 可执行安装目录内的 .venv Python，参数为 `-I -X utf8 -m posthub serve --data-dir 固定绝对路径 --host 127.0.0.1 --port 8787`。只保留一个服务进程。升级后更新任务中的解释器路径，避免仍启动旧目录程序。

## 校验、备份与回退

`python manage.py verify` 核对包内文件 SHA-256；外部 ZIP 也附带 .sha256 文件。这用于发现损坏，不是发布者数字签名。请使用可信来源的包。

升级备份目录含 config.json 和 posthub.sqlite3。恢复前停止所有相关服务，保留当前数据副本，用升级前配套文件恢复，并使用对应旧程序与旧依赖启动。不要让旧程序直接打开已经迁移的新库。历史消息、重试和验证记录均以恢复的备份时刻为准，回退可能重复处理备份后已投递的消息，需先核对记录。

安装后可用 `/healthz` 检查服务状态；登录 Web 核对上游、下游、历史和待发任务。渠道接口接受与实际收件是不同状态，真实收件需收件人确认。

## 测试范围

本包的 Windows 11、Debian 12 和 Ubuntu 24.04 全新安装、旧版本升级、同版本重装、运行中拒绝升级、数据保留与本地 HTTP 实际投递均按独立验收脚本检查。Linux 在完整虚拟机内实测；结果记录在源码仓库 docs/INSTALLER-REVIEW.md 与 docs/LINUX-REVIEW.md。真实 QQ/微信/邮箱账号不作为安装测试夹具；Docker 和 systemd 开机自启尚未在本轮验收。
