# Debian 与 Ubuntu 安装升级实测

日期：2026-11-12（Asia/Shanghai）。程序版本：0.1.25。

**两套独立 Linux 虚拟机均通过正式包全新安装、同版本重装、0.1.24 与 0.1.19 升级验收。配置与原有记录保留，升级后的待发和重试任务正常转发，本轮业务测试共收到 24 条，无重复。**

## 环境与范围

| 系统 | Python | 结果 |
| --- | --- | --- |
| Debian GNU/Linux 12 (bookworm)，x86_64 | Python 3.11.2 | 全部安装升级场景通过 |
| Ubuntu 24.04.5 LTS，x86_64 | Python 3.12.3 | 全部安装升级场景通过 |

使用官方云镜像启动完整 Linux 内核与用户空间，在 QEMU TCG 软件模拟虚拟机内运行；每台 2 个虚拟 CPU、768 MiB 内存，逐台验收。不是 Git Bash、语法检查或远端 GitHub Actions。镜像下载后核对官方校验值；来源为 [Debian Bookworm](https://cloud.debian.org/images/cloud/bookworm/latest/) 与 [Ubuntu Noble Minimal](https://cloud-images.ubuntu.com/minimal/releases/noble/release/)。

两台系统都实际执行包内 `install.sh`、`upgrade.sh` 和 `start.sh`，安装目录含中文与空格。受测原安装 ZIP 的 SHA-256：

```text
414047b753497f93b6031c07718c5b3f12e88e7e8a05f2109ab706808b860f08
```

## 验收结果

| 检查 | Debian / Ubuntu 结果 |
| --- | --- |
| 全新安装 | 独立 venv、锁定运行依赖、初始账号、首次改密、Web 资源和字体正常 |
| 安装范围 | pip check 通过；安装环境没有 pytest、aiosmtpd、Playwright 等测试依赖 |
| 同版本重装 | 0.1.25 → 0.1.25，原配置与记录保留 |
| 较新旧版升级 | 0.1.24 → 0.1.25，schema 10 保持 |
| 数据库迁移 | 0.1.19 → 0.1.25，schema 8 → 10；使用真实历史 wheel 生成旧数据 |
| 配置与密钥 | config.json 摘要不变；原密码登录成功、原 API 密钥仍可使用；加密渠道、代理配置和各上游授权保留 |
| 消息与历史 | 升级前每张已有数据表逐行比较，数据不丢失；配套备份与升级前数据一致 |
| 升级后转发 | 未过期 queued、retry 自动继续，部分成功任务只发送剩余渠道 |
| 幂等与隔离 | 同业务编号重提仍返回原消息；另一上游越权指定渠道返回 403 |
| 取消与待配置 | 取消任务不发送；尚未配置下游的消息继续保留 |
| 重启与重复 | 每种升级场景启动并再次重启，均未新增重复投递；每系统 12 条业务收件、两系统共 24 条 |
| 运行中升级保护 | Linux 文件锁生效，服务运行时拒绝升级，未替换程序或创建升级备份 |

每种场景的 4 条业务收件分别为：升级前已完成 1 条、部分成功任务的两个渠道各 1 条、原排队任务 1 条。收件验证通知另行执行，不混入上述业务计数。

## 复现

在隔离 Linux 测试机准备正式安装包、真实历史 wheel、源码中的 `tests/installer_acceptance.py` 与同目录的 `installer_seed.py`。测试运行器单独安装 httpx，不能指向正式数据目录：

```bash
python3 -m venv runner
runner/bin/python -m pip install httpx==0.28.1
runner/bin/python installer_acceptance.py \
  --package installer.zip \
  --work-dir '安装升级 实测' \
  --artifacts results \
  --http-timeout 60 \
  --old-wheel posthub-0.1.24-py3-none-any.whl \
  --old-wheel posthub-0.1.19-py3-none-any.whl
```

工作目录必须尚不存在。验收器读取实际安装的 wheel，不依赖源码导入；其本地 HTTP 接收端记录真实请求次数，并检查投递 API 状态。维护工作区 `artifacts/linux025/` 保存两台系统的脱敏结果、系统版本、内核、Python 版本及镜像校验记录；正式安装包不包含测试资料。

## 本轮问题与边界

- Debian 首次尝试在登录时触发测试客户端的 5 秒超时。软件模拟环境中，同样的 PBKDF2-SHA256 600,000 次运算实测耗时约 5.74 秒。仅为验收器增加可配置等待时间，本轮使用 60 秒后从全新目录完整重跑通过；未降低产品密码强度或修改投递设置。
- 本轮 QQ、微信、邮箱保存测试使用虚构凭据并停用外部发送，真实收发由虚拟机内部 HTTP 接收器完成。证明的是安装、迁移、数据保留和队列续发；真实第三方收件仍参见 [已有渠道验收](../TEST-REPORT.md)。
- 不把软件模拟环境速度视为生产性能数据；未在本轮验证物理 Linux 机器、ARM、其他发行版版本、Python 3.13、systemd 开机自启或 Docker。GitHub 远端 CI 仍未运行。
- 升级前仍须停止旧服务、使用原数据目录并保管数据库与 config.json 配套备份。测试中的无重复结论对应上述明确场景，不把第三方超时或进程在外部发送中途崩溃视为绝对只发一次的保证。

操作步骤见 [正式安装与升级](INSTALLER.md)。本轮未改动正式 Windows 服务、Clash 设置、真实账号或业务数据。

## 交付安装包

`文思邮差-0.1.25.zip`：8,174,976 字节，约 8.17 MB。相比上文实际完成安装升级的原包，只更新 `INSTALL.md` 和对应的 `manifest.json`；wheel、安装/升级/启动脚本及运行依赖清单逐字节一致，没有删减功能或加入测试文件。更新后的包也已在 Ubuntu 中解压并通过安装器清单校验。

SHA-256：

```text
a5d9e3a0a3d8ad052deaec9bd0665e48f433bb035e97fd0fd0f112de847ebef3
```
