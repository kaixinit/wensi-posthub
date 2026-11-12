"""Explicit source-export scope; never recurse into local data or test evidence."""
from pathlib import Path

ROOT_FILES = {
    'README.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'SUPPORT.md', 'SECURITY.md',
    'CODE_OF_CONDUCT.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'TEST-REPORT.md',
    'pyproject.toml', 'requirements.lock', 'Dockerfile', 'compose.yaml',
    '.gitignore', '.dockerignore', '.gitattributes', '.editorconfig',
}
ROOT_DIRS = {'.github', 'posthub', 'docs', 'deploy', 'tests', 'scripts', 'tools'}
EXCLUDE_DIRS = {'__pycache__', '.pytest_cache', '.git', '.venv', 'build', 'dist',
                'data', 'dev-data', 'artifacts', 'posthub.egg-info'}

def selected_files(root: Path):
    for path in sorted(root.rglob('*')):
        rel=path.relative_to(root)
        if any(p in EXCLUDE_DIRS or p.endswith('.egg-info') for p in rel.parts):continue
        if not path.is_file():continue
        if len(rel.parts)==1 and rel.name not in ROOT_FILES:continue
        if len(rel.parts)>1 and rel.parts[0] not in ROOT_DIRS:continue
        if path.is_symlink():raise ValueError('Source export does not allow symlinks: '+rel.as_posix())
        yield path
