"""Destructive upgrade boundaries: refuse before pip or original-data mutation."""
import importlib.util
import json
from pathlib import Path
import sqlite3

from filelock import FileLock
import pytest


@pytest.fixture
def manager(tmp_path, monkeypatch):
    path=Path(__file__).resolve().parents[1]/'deploy/installer/manage.py'
    spec=importlib.util.spec_from_file_location('release_manager',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'ROOT',tmp_path)
    monkeypatch.setattr(module,'STATE',tmp_path/'installation.json')
    monkeypatch.setattr(module,'verify_package',lambda:({'version':'0.1.25'},tmp_path/'package.whl'))
    monkeypatch.setattr(module,'run',lambda args:pytest.fail('pip/initialization must not run'))
    return module


def test_running_service_rejects_upgrade_before_backup(manager,tmp_path):
    data=tmp_path/'data';data.mkdir()
    with FileLock(str(data/'server.lock')):
        with pytest.raises(RuntimeError,match='正在运行'):
            manager.install(data,True)
    assert not (data/'backups').exists()


def test_switching_data_does_not_upgrade_another_running_environment(manager,tmp_path):
    old=tmp_path/'old';old.mkdir()
    manager.STATE.write_text(json.dumps({'data_dir':str(old)}),encoding='utf-8')
    with FileLock(str(old/'server.lock')):
        with pytest.raises(RuntimeError,match='正在运行'):
            manager.install(tmp_path/'new',False)


def test_incomplete_existing_data_never_resets_password(manager,tmp_path):
    data=tmp_path/'data';data.mkdir()
    config=data/'config.json';original=b'{"password_hash":"fixture","encryption_key":"fixture"}'
    config.write_bytes(original)
    with pytest.raises(RuntimeError,match='数据不完整'):manager.install(data,False)
    assert config.read_bytes()==original and not (data/'backups').exists()


def test_newer_schema_refuses_before_pip(manager,tmp_path):
    data=tmp_path/'data';data.mkdir()
    (data/'config.json').write_text(json.dumps({'password_hash':'fixture','encryption_key':'fixture'}))
    with sqlite3.connect(data/'posthub.sqlite3') as conn:conn.execute('PRAGMA user_version=11')
    with pytest.raises(RuntimeError,match='不支持降级'):manager.install(data,True)
    assert not (data/'backups').exists()


def test_upgrade_requires_existing_data(manager,tmp_path):
    with pytest.raises(RuntimeError,match='未找到已有数据'):manager.install(tmp_path/'missing',True)
