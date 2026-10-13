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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    subprocess.run([sys.executable, str(ROOT / 'tools/validate.py')], check=True, cwd=ROOT)
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT).strip():
        raise SystemExit('Commit reviewed source before packaging')
    files = json.loads((ROOT / 'publication.json').read_text())['files']
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(files):
            payload = (ROOT / name).read_bytes()
            member = tarfile.TarInfo('k3s-self-healing/' + name)
            member.size = len(payload)
            member.mode = 0o644
            member.uid = member.gid = member.mtime = 0
            member.uname = member.gname = ''
            archive.addfile(member, io.BytesIO(payload))
    payload = gzip.compress(buffer.getvalue(), mtime=0)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('xb') as output:
        output.write(payload)
    digest = hashlib.sha256(payload).hexdigest()
    args.output.with_suffix(args.output.suffix + '.sha256').write_text(digest + '  ' + args.output.name + '\n')
    print(f'Packaged {len(files)} source files: {digest}')


if __name__ == '__main__':
    main()
