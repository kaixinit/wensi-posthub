"""Install the release wheel with pip; retain and back up existing user data."""
import argparse
from contextlib import contextmanager, closing, ExitStack
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'installation.json'


def run(args):
    subprocess.run([str(x) for x in args], cwd=ROOT, check=True)


def python_path():
    return ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


@contextmanager
def exclusive(path):
    """Use the same OS lock as filelock, without requiring bootstrap packages."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    locked = False
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise RuntimeError('服务或另一安装任务正在运行，请先停止后再安装/升级。') from None
        yield
    finally:
        if locked:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def verify_package():
    manifest = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
    required = {'manage.py', 'requirements-runtime.lock', 'INSTALL.md', 'LICENSE', manifest['wheel']}
    if manifest.get('format') != 1 or not required <= manifest['files'].keys():
        raise RuntimeError('安装包清单不完整或格式不支持。')
    for name, expected in manifest['files'].items():
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise RuntimeError('安装包文件缺失或路径无效，请重新解压。')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('安装包校验失败：' + name + '，请重新下载/解压。')
    wheels = list((ROOT / 'packages').glob('*.whl'))
    if len(wheels) != 1 or wheels[0].relative_to(ROOT).as_posix() != manifest['wheel']:
        raise RuntimeError('安装包必须恰好包含一份指定版本程序。')
    return manifest, wheels[0]


def data_path(value):
    if value:
        return Path(value).expanduser().resolve()
    if STATE.exists():
        return Path(json.loads(STATE.read_text(encoding='utf-8'))['data_dir']).resolve()
    if os.environ.get('POSTHUB_DATA_DIR'):
        return Path(os.environ['POSTHUB_DATA_DIR']).expanduser().resolve()
    return ROOT / 'data'


def inspect_data(data, upgrade):
    config, database = data / 'config.json', data / 'posthub.sqlite3'
    if config.exists() != database.exists():
        raise RuntimeError('数据不完整：config.json 与 posthub.sqlite3 必须配套。不会重新初始化，请先恢复配套备份。')
    if not config.exists():
        if upgrade:
            raise RuntimeError('未找到已有数据；升级时请用 --data-dir 指定原数据目录。')
        return False
    settings = json.loads(config.read_text(encoding='utf-8'))
    if not settings.get('encryption_key') or not settings.get('password_hash'):
        raise RuntimeError('原配置缺少解密密钥或密码信息，安装已停止。')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('原数据库完整性检查失败，安装已停止。')
        if conn.execute('PRAGMA user_version').fetchone()[0] > 10:
            raise RuntimeError('原数据库比本程序更新，不支持降级安装。')
    return True


def backup(data):
    target = data / 'backups' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    target.mkdir(parents=True, mode=0o700)
    config = target / 'config.json'
    shutil.copyfile(data / 'config.json', config)
    os.chmod(config, 0o600)
    db = target / 'posthub.sqlite3'
    with closing(sqlite3.connect((data / 'posthub.sqlite3').as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(db)) as destination:
            source.backup(destination)
            if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('升级前备份检查失败，安装已停止。')
    os.chmod(db, 0o600)
    print('已保存升级前配套备份：' + str(target), flush=True)
    return target


def install(data, upgrade):
    manifest, wheel = verify_package()
    with ExitStack() as locks:
        locks.enter_context(exclusive(ROOT / 'install.lock'))
        if STATE.exists():
            old_data = Path(json.loads(STATE.read_text(encoding='utf-8'))['data_dir']).resolve()
            if old_data != data:
                locks.enter_context(exclusive(old_data / 'server.lock'))
        locks.enter_context(exclusive(data / 'server.lock'))
        existing = inspect_data(data, upgrade)
        snapshot = backup(data) if existing else None
        python = python_path()
        if not python.exists():
            run([sys.executable, '-m', 'venv', ROOT / '.venv'])
        # pip owns package installation. No source tree or tests are bundled.
        run([python, '-I', '-X', 'utf8', '-m', 'pip', 'install', '--disable-pip-version-check', '-r', ROOT / 'requirements-runtime.lock'])
        run([python, '-I', '-X', 'utf8', '-m', 'pip', 'install', '--disable-pip-version-check', '--no-deps', '--force-reinstall', wheel])
        run([python, '-I', '-X', 'utf8', '-m', 'pip', 'check'])
        if existing:
            original = (data / 'config.json').read_bytes()
            run([python, '-I', '-X', 'utf8', '-c', 'import sys; from posthub.storage import Store; Store(sys.argv[1]); print("Data migration passed.")', data])
            if (data / 'config.json').read_bytes() != original:
                raise RuntimeError('原账号配置发生了意外变化，请使用备份核对。')
        else:
            run([python, '-I', '-X', 'utf8', '-m', 'posthub', 'init', '--generate-password', '--data-dir', data])
        state = {'version': manifest['version'], 'data_dir': str(data), 'backup': str(snapshot) if snapshot else None}
        temporary = STATE.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, STATE)
        print('安装完成。数据目录：' + str(data), flush=True)
        print('运行 python manage.py serve 启动。', flush=True)


def main():
    parser = argparse.ArgumentParser(description='文思邮差安装与升级')
    parser.add_argument('command', choices=['install', 'upgrade', 'serve', 'verify'])
    parser.add_argument('--data-dir', help='原数据目录或新数据目录，建议绝对路径')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error('需要 Python 3.11 或更新版本。')
    if args.command == 'verify':
        manifest, _ = verify_package()
        print('安装包校验通过：' + manifest['version'])
    elif args.command == 'serve':
        python = python_path()
        if not python.exists() or not STATE.exists():
            raise RuntimeError('请先运行安装或升级命令。')
        argv = [str(python), '-I', '-X', 'utf8', '-m', 'posthub', 'serve', '--data-dir', str(data_path(args.data_dir)), '--host', args.host, '--port', str(args.port)]
        if os.name == 'nt':
            # Keep the launcher attached to the foreground Windows service.
            sys.exit(subprocess.call(argv, cwd=ROOT))
        os.execv(str(python), argv)
    else:
        install(data_path(args.data_dir), args.command == 'upgrade')


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    try:
        main()
    except (RuntimeError, OSError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as error:
        print('安装/启动未完成：' + str(error), file=sys.stderr)
        print('原数据不会被重新初始化；升级备份保存在原数据目录 backups 下。请排除错误后重试。', file=sys.stderr)
        sys.exit(1)
