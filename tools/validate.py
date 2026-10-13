#!/usr/bin/env python3
"""Validate the exact publication inventory, syntax and privacy boundaries."""
import ast
import json
from pathlib import Path
import subprocess
import yaml
from privacy_scan import scan

ROOT = Path(__file__).resolve().parents[1]


def main():
    files = json.loads((ROOT / 'publication.json').read_text())['files']
    actual = set(subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT).decode().strip('\0').split('\0'))
    if len(files) != len(set(files)) or set(files) != actual:
        raise SystemExit('Publication inventory mismatch; explicitly review added/removed paths: ' + str(sorted(set(files) ^ actual)))
    failures = scan(ROOT, files)
    if failures:
        raise SystemExit(json.dumps(failures, indent=2))
    for name in files:
        path = ROOT / name
        if path.suffix == '.py':
            ast.parse(path.read_text(), filename=name)
        elif path.suffix == '.json':
            json.loads(path.read_text())
        elif path.suffix in {'.yaml', '.yml'} and not name.startswith('helm/native-recovery/templates/'):
            list(yaml.compose_all(path.read_text()))
        elif path.suffix == '.sh':
            subprocess.run(['bash', '-n', str(path)], check=True)
    print(f'Validated {len(files)} explicitly approved source files.')


if __name__ == '__main__':
    main()
