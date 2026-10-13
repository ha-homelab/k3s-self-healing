#!/usr/bin/env python3
"""Build a deterministic source archive with no Git or workstation metadata."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def require_source(root, base=None, channel=None):
    """Retain clean local builds; candidates permit only a verified version overlay."""
    if base is None and channel is None:
        if subprocess.check_output(['git', 'status', '--porcelain'], cwd=root).strip():
            raise ValueError('Commit reviewed source before packaging')
        return
    if base is None or channel not in {'beta', 'rc', 'nightly'}:
        raise ValueError('Candidate builds require both base and channel')
    if not (root / '.release-plan.json').is_file():
        raise ValueError('Candidate builds require a frozen release plan')
    sys.path.insert(0, str(root / 'scripts'))
    from release_version_adapter import checked_version
    checked_version(root, base, channel)
    changed = set(subprocess.check_output(
        ['git', 'diff', '--name-only', '-z', 'HEAD'], cwd=root
    ).decode().split('\0')) - {''}
    untracked = subprocess.check_output(
        ['git', 'ls-files', '--others', '--exclude-standard', '-z'], cwd=root
    )
    if changed - {'SOURCE_VERSION'} or untracked:
        raise ValueError('Only the frozen SOURCE_VERSION overlay may differ from the commit')


def build(root, output, base=None, channel=None):
    """Package the complete approved inventory with the historical archive layout."""
    subprocess.run([sys.executable, str(root / 'tools/validate.py')], check=True, cwd=root)
    require_source(root, base, channel)
    files = json.loads((root / 'publication.json').read_text())['files']
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(files):
            payload = (root / name).read_bytes()
            member = tarfile.TarInfo('k3s-self-healing/' + name)
            member.size = len(payload)
            member.mode = 0o644
            member.uid = member.gid = member.mtime = 0
            member.uname = member.gname = ''
            archive.addfile(member, io.BytesIO(payload))
    payload = gzip.compress(buffer.getvalue(), mtime=0)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as handle:
        handle.write(payload)
    digest = hashlib.sha256(payload).hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(digest + '  ' + output.name + '\n')
    print(f'Packaged {len(files)} source files: {digest}')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--base')
    parser.add_argument('--channel', choices=('beta', 'rc', 'nightly'))
    args = parser.parse_args()
    build(ROOT, args.output, args.base, args.channel)


if __name__ == '__main__':
    main()
