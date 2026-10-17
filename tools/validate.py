#!/usr/bin/env python3
"""Validate the exact publication inventory, syntax and privacy boundaries."""
import argparse
import ast
import json
from pathlib import Path
import subprocess
import sys
import yaml
from privacy_scan import scan

ROOT = Path(__file__).resolve().parents[1]


def check_python_diagnostics(files):
    python_files = [name for name in files if name.endswith('.py')]
    ha_script = 'homeassistant/python_scripts/notification_service_available.py'
    lint = [sys.executable, '-m', 'ruff', 'check', '--isolated', '--ignore-noqa', '--select', 'E9,F63,F7,F82']
    subprocess.run(lint + [name for name in python_files if name != ha_script], check=True, cwd=ROOT)
    # Home Assistant's python_script integration supplies these names; keep
    # undefined-name checking for every other identifier in this exact script.
    subprocess.run(lint + ['--config', 'builtins = ["data", "hass", "output"]', ha_script], check=True, cwd=ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lint', action='store_true', help='also run the required CI Python diagnostic baseline')
    arguments = parser.parse_args()
    files = json.loads((ROOT / 'publication.json').read_text())['files']
    actual = set(subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT).decode().strip('\0').split('\0'))
    if len(files) != len(set(files)) or set(files) != actual:
        raise SystemExit('Publication inventory mismatch; explicitly review added/removed paths: ' + str(sorted(set(files) ^ actual)))
    failures = scan(ROOT, files)
    if failures:
        raise SystemExit(json.dumps(failures, indent=2))
    if arguments.lint:
        check_python_diagnostics(files)
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
