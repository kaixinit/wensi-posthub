import argparse
from getpass import getpass
import os
import secrets
from pathlib import Path

from . import PRODUCT_NAME, PRODUCT_SUBTITLE
from .storage import Store, initialize


def main():
    parser = argparse.ArgumentParser(description=f"{PRODUCT_NAME} · {PRODUCT_SUBTITLE}")
    parser.add_argument("command", choices=["init", "serve", "reset-password", "backup"])
    parser.add_argument("--data-dir", default=os.getenv("POSTHUB_DATA_DIR", "data"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--output", help="数据库备份输出路径")
    parser.add_argument("--generate-password", action="store_true", help="初始化时生成一次性初始密码")
    args = parser.parse_args()
    directory = Path(args.data_dir).resolve()
    if args.generate_password and args.command != 'init':
        parser.error('--generate-password 仅用于首次初始化')
    if args.command in {"init", "reset-password"}:
        if args.generate_password:
            password = secrets.token_urlsafe(18)
        else:
            password = getpass("管理员密码（12–256 个字符）：")
            if password != getpass("再次输入密码："):
                parser.error("两次密码不一致")
        if not 12 <= len(password) <= 256:
            parser.error("密码需要 12–256 个字符")
        if args.command == "init":
            initialize(directory, password, must_change_password=args.generate_password)
            Store(directory)
        else:
            store = Store(directory)
            store.change_password(password, require_current=False)
        print("配置已保存：", directory)
        print('管理员账号：admin')
        if args.generate_password:
            print('初始密码：' + password)
            print('请保存此密码；首次登录后需在“账号设置”中修改。')
    elif args.command == "backup":
        import sqlite3
        store = Store(directory)
        if not args.output:
            parser.error("备份需要 --output 参数")
        destination = Path(args.output).resolve()
        if destination.exists():
            parser.error("备份目标已存在，请使用新文件名")
        with sqlite3.connect(store.path) as source, sqlite3.connect(destination) as backup:
            source.backup(backup)
        os.chmod(destination, 0o600)
        print("数据库已备份。恢复时还需要原 data/config.json（含解密密钥），请另行安全保存。")
    else:
        import uvicorn
        from .web import create_app
        # One process owns durable dispatch. FileLock rejects a second server.
        uvicorn.run(create_app(directory), host=args.host, port=args.port, workers=1, access_log=False, proxy_headers=False, timeout_graceful_shutdown=100)


if __name__ == "__main__":
    main()
