"""Export one wheel plus launchers; resolve runtime pins from validated metadata."""
import argparse
from email.parser import BytesParser
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import tomllib
from zipfile import ZipFile, ZIP_DEFLATED

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def requirements(wheel):
    pins = {}
    for line in (ROOT / 'requirements.lock').read_text(encoding='utf-8').splitlines():
        if line.strip() and not line.startswith('#'):
            requirement = Requirement(line)
            pins[canonicalize_name(requirement.name)] = requirement
    with ZipFile(wheel) as archive:
        metadata = BytesParser().parsebytes(archive.read(next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA'))))
    queue = [(Requirement(r), {''}) for r in metadata.get_all('Requires-Dist', [])]
    selected = {}
    while queue:
        dependency, parent_extras = queue.pop()
        enabled = dependency.marker is None
        for platform, system in [('win32', 'Windows'), ('linux', 'Linux')]:
            for version in ['3.11', '3.12', '3.13']:
                for extra in parent_extras | {''}:
                    env = default_environment() | {'sys_platform': platform, 'platform_system': system,
                        'os_name': 'nt' if platform == 'win32' else 'posix', 'python_version': version,
                        'python_full_version': version + '.0', 'extra': extra}
                    enabled = enabled or bool(dependency.marker and dependency.marker.evaluate(env))
        if not enabled:
            continue
        name = canonicalize_name(dependency.name)
        extras = set(dependency.extras)
        if name in selected and extras <= selected[name]:
            continue
        pin = pins[name]
        version = next(iter(pin.specifier)).version
        if version not in dependency.specifier:
            raise ValueError('Runtime pin conflicts with dependency: ' + name)
        dist = importlib.metadata.distribution(name)
        if dist.version != version:
            raise ValueError('Build environment does not match requirements.lock: ' + name)
        selected.setdefault(name, set()).update(extras)
        queue.extend((Requirement(r), selected[name] | {''}) for r in dist.requires or [])
    return '\n'.join(str(pins[name]) for name in sorted(selected)) + '\n', sorted(selected)


def export(wheel, output):
    if output.exists():
        raise FileExistsError('Use a new output filename')
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    assert wheel.name == f'posthub-{version}-py3-none-any.whl'
    with ZipFile(wheel) as archive:
        # Every runtime file, including fonts, help and channel adapters, must match source.
        for p in (ROOT / 'posthub').rglob('*'):
            if p.is_file() and '__pycache__' not in p.parts:
                assert archive.read(p.relative_to(ROOT).as_posix()) == p.read_bytes(), p.name
        assert not any('/tests/' in n or '/artifacts/' in n for n in archive.namelist())
    lock, names = requirements(wheel)
    files = {p.name: p.read_bytes() for p in (ROOT / 'deploy/installer').iterdir() if p.is_file() and p.suffix in {'.py', '.ps1', '.sh'}}
    files.update({'packages/' + wheel.name: wheel.read_bytes(), 'requirements-runtime.lock': lock.encode(),
        'LICENSE': (ROOT / 'LICENSE').read_bytes(), 'INSTALL.md': (ROOT / 'docs/INSTALLER.md').read_bytes()})
    # Runtime licenses are preserved in the wheel and its installed static directory.
    files['THIRD_PARTY_NOTICES.txt'] = ('主项目采用 MIT，见 LICENSE。\n程序依赖由 pip 按 requirements-runtime.lock 安装，各自遵循原许可。\n字体与图标原许可随 wheel 保存，安装后位于 posthub/static/fonts/OFL.txt、\nposthub/static/icons/LICENSE.txt 和 posthub/static/icons/FEATHER-LICENSE.txt。\n').encode('utf-8')
    manifest = {'format': 1, 'version': version, 'wheel': 'packages/' + wheel.name,
        'files': {name: hashlib.sha256(value).hexdigest() for name, value in sorted(files.items())}}
    files['manifest.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        for name, value in sorted(files.items()):
            archive.writestr('posthub/' + name, value)
    with ZipFile(output) as archive:
        assert archive.testzip() is None
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.sha256').write_text(digest + '  ' + output.name + '\n', encoding='utf-8')
    return {'version': version, 'files': len(files), 'bytes': output.stat().st_size, 'sha256': digest,
        'runtime_dependencies': names, 'wheel_copies': 1, 'runtime_source_files_match': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.wheel, args.output), ensure_ascii=True, indent=2))
