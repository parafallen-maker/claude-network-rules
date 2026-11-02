#!/bin/bash
# Generate the complete source component manifest and SHA256 integrity list.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 - <<'PY'
from pathlib import Path
import hashlib, json, re
root = Path('.')
version = (root / 'VERSION').read_text().strip()
if not re.fullmatch(r'[0-9A-Za-z._-]{1,64}', version):
    raise SystemExit('Invalid VERSION')
paths = [Path('VERSION'), Path('LICENSE'), Path('assets/icon-2048.png')]
for directory in ('core', 'daemons', 'installer', 'app', 'windows', 'linux', 'scripts', '.github/workflows'):
    paths += [p for p in Path(directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc']
files = {}
for path in sorted(set(paths)):
    if path.is_symlink():
        raise SystemExit('Symlink is not a component: ' + str(path))
    files[path.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
Path('COMPONENTS.json').write_text(json.dumps({'schema': 1, 'version': version, 'files': files}, ensure_ascii=False, indent=2) + '\n')
Path('SHA256SUMS').write_text(''.join(h + '  ' + p + '\n' for p, h in files.items()))
print('Component manifest and SHA256SUMS updated: ' + str(len(files)) + ' files')
PY
