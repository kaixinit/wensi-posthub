# 安装、部署与维护

先阅读 [README](../README.md)。当前为单机、单服务进程、SQLite 模式；不要对同一数据目录启动多个进程或增加 Uvicorn workers。

## 环境与安装脚本

需要 Python 3.11+。Windows 11 / Python 3.11 已实测；正式安装包已在 Debian 12 / Python 3.11、Ubuntu 24.04 / Python 3.12 独立虚拟机通过安装升级，见 [Linux 验收](LINUX-REVIEW.md)。Docker 等其他范围见 [测试说明](TESTING.md)。

在包含 `pyproject.toml` 的项目目录执行：

- Windows：`powershell -ExecutionPolicy Bypass -File .\deploy\install.ps1`，然后 `powershell -ExecutionPolicy Bypass -File .\deploy\start.ps1`。
- Debian / Ubuntu：安装 Python、venv 与 pip 后，执行 `bash deploy/install.sh` 和 `bash deploy/start.sh`。
- 非默认解释器：`PYTHON_BIN=/path/to/python3.11 bash deploy/install.sh`。

首次安装生成独立密码。默认账号 `admin`，首次改密后进入接入向导。重复安装不会重新初始化已有 `config.json`。

## 手动安装

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps .
.\.venv\Scripts\python.exe -m posthub init --generate-password --data-dir data
.\.venv\Scripts\python.exe -m posthub serve --data-dir data --host 127.0.0.1 --port 8787
```

### Linux shell

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps .
.venv/bin/python -m posthub init --generate-password --data-dir data
.venv/bin/python -m posthub serve --data-dir data --host 127.0.0.1 --port 8787
```

`init` 仅用于新数据目录；已有安装直接使用原数据目录启动。手动运行不带 `--generate-password` 的 `init` 可交互设置自己的密码。

`requirements.lock` 固定当前验证的环境，安装与测试都使用它。数据目录默认是工作目录下 `data/`，可通过 `POSTHUB_DATA_DIR` 或 `--data-dir` 指定；长期运行应使用固定绝对路径。

## 局域网与公网入口

Windows 可执行 `powershell -ExecutionPolicy Bypass -File .\deploy\start.ps1 -HostAddress 0.0.0.0 -Port 8787`；Linux 可执行 `POSTHUB_HOST=0.0.0.0 POSTHUB_PORT=8787 bash deploy/start.sh`。先关闭同一数据目录的旧服务。防火墙只放行需要访问的范围。

上游填写机器实际 IP 或域名；`0.0.0.0` 仅用于监听，不是访问地址。另一台机器不能使用 `127.0.0.1` 访问本机服务。

平台使用 Python / FastAPI / Uvicorn，不内置 Nginx。frpc 同机时可直接转发 `127.0.0.1:8787`；公网 HTTPS 可由外部 Nginx 或 Caddy 提供。

- [frpc TCP 示例](../deploy/frpc-posthub.toml.example)
- [Nginx 与 frps 同机示例](../deploy/nginx-posthub.conf.example)
- [Caddy 示例](../deploy/Caddyfile.example)

替换示例域名、端口及证书路径，按代理软件自己的检查方式验证后再启用。当前仅支持网站根路径，不支持 `/posthub/` 子路径。保留 Host 和 QQ 回调所需签名头。后端默认不信任代理 IP 头；公网入口可另外限制登录频率。

专属 `/api/v1/hooks/` 地址包含凭据，反向代理访问日志应关闭或脱敏。Uvicorn 访问日志默认关闭。设置平台访问地址不会自动创建映射、证书或防火墙规则。

后台收件确认链接需要在“网络通信 → 平台访问地址”设置收件人可访问的固定地址。HTTPS 部署后使用同一 HTTPS 地址重新登录。出站代理与公网入口是两件事，见 [网络通信](guide/05-network.md)。

## Linux systemd

假设程序已放在 `/opt/posthub` 并安装好 `.venv`，以部署机器管理员身份执行：

```bash
sudo useradd --system --home /var/lib/posthub --shell /usr/sbin/nologin posthub
sudo install -d -m 700 -o posthub -g posthub /var/lib/posthub
sudo -u posthub /opt/posthub/.venv/bin/python -m posthub init --data-dir /var/lib/posthub
sudo cp /opt/posthub/deploy/posthub.service /etc/systemd/system/posthub.service
sudo systemctl daemon-reload
sudo systemctl enable --now posthub
sudo systemctl status posthub
```

用户已存在时跳过创建用户；数据已初始化时跳过 `init`。服务账号需能读取程序文件。服务配置见 [posthub.service](../deploy/posthub.service)，日志使用 `journalctl -u posthub -f`。此部署流程仍需 Linux 实机验收。

## Windows 常驻

先使用启动脚本验证。任务计划程序可指定 `.venv\Scripts\python.exe`，参数 `-m posthub serve --data-dir "固定数据目录" --host 127.0.0.1 --port 8787`，起始目录为项目目录，并配置失败重启。只保留一个服务任务；若依赖 Clash，使用可访问该代理的用户环境。数据目录使用专用用户权限保护。

## Docker

```bash
docker compose build
docker compose run --rm posthub init
docker compose up -d
docker compose logs -f
```

初始化为交互式设置密码。Compose 默认仅将端口映射到宿主机 `127.0.0.1:8787`，数据保存在具名卷；不要删除卷。容器中的 `127.0.0.1` 指容器自身，代理地址必须从容器可达。当前环境尚未执行 Docker 构建与运行，见 [测试范围](TESTING.md)。

## 备份恢复与升级

在项目虚拟环境中执行，输出文件必须尚不存在：

```text
python -m posthub backup --data-dir /path/to/data --output posthub-backup.sqlite3
```

此命令在线备份 SQLite 数据库。**必须另外保存同一数据目录的 `config.json`**，它含解密密钥；数据库和配置要配套保管，不能放入公开仓库。

升级时先做配套备份，停止服务，安装新程序，再用原数据目录启动。默认保留密码、接口密钥、收件配置和历史。数据库会自动执行支持的迁移；当前 schema 10，旧程序不能直接打开更新后的库。回滚需原版本程序及其升级前配套备份。

恢复时先保留当前数据副本并停止服务，再还原数据库为 `posthub.sqlite3` 及对应 `config.json`。不要用新初始化的配置替代旧解密密钥。

忘记登录密码时，用原数据目录运行 `python -m posthub reset-password --data-dir /path/to/data`，按提示输入新密码，不要删除配置。步骤见 [账号管理](guide/07-account.md)。

## 监控与规模边界

`/healthz` 用于外部存活及分发心跳监控，心跳异常返回 503。系统完全停止时无法自行发出告警，应由外部监控检查。

当前串行处理投递，慢渠道会影响后续队列耗时；适合个人和小团队通知，不承诺吞吐量或固定到达秒数。历史消息不会自动清理，长期部署需监控磁盘并制定保留、归档策略。
