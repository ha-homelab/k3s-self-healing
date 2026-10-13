#!/usr/bin/env python3
"""Import a bounded, pinned set of HA files; never mirror a site's repository."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import urllib.error
import urllib.request

from privacy_scan import findings

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'ha-homelab/home-assistant'
LOCK = 'ha-sources.lock.json'
MAX_SOURCE_BYTES = 1024 * 1024
# Destination, upstream path, reviewed transformation. No other paths are fetched.
FILES = (
    ('homeassistant/packages/core_updates.yaml', 'config/packages/core_updates.yaml', 'core-package-v1'),
    ('homeassistant/packages/hacs_maintenance.yaml', 'config/packages/hacs_maintenance.yaml', 'identity-v1'),
    ('homeassistant/blueprints/motion_light_off.yaml', 'config/blueprints/automation/ha_homelab/motion_light_off.yaml', 'identity-v1'),
    ('runtime_tests/test_core_updates.py', 'tests/test_core_updates.py', 'core-tests-v1'),
    ('runtime_tests/test_hacs_maintenance.py', 'tests/test_hacs_maintenance.py', 'hacs-tests-v1'),
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def exact_revision(revision):
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('HA source revision must be a full lowercase commit SHA')
    return revision


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('HA source redirects are not allowed')


def fetch_source(revision, source):
    exact_revision(revision)
    if source not in {item[1] for item in FILES}:
        raise ValueError('HA source path is outside the reviewed inventory')
    url = f'https://raw.githubusercontent.com/{REPOSITORY}/{revision}/{source}'
    request = urllib.request.Request(url, headers={'Accept': 'text/plain'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            data = response.read(MAX_SOURCE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError):
        raise ValueError(f'Cannot fetch pinned HA source: {source}') from None
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError(f'HA source exceeds size limit: {source}')
    return data


def substitute(text, pattern, replacement, count):
    result, actual = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if actual != count:
        raise ValueError('HA source structure changed; review the transformation')
    return result


def transform(data, transformation):
    if len(data) > MAX_SOURCE_BYTES or b'\0' in data:
        raise ValueError('Invalid HA source content')
    try:
        text = data.decode('utf-8')
    except UnicodeError:
        raise ValueError('HA source must be UTF-8 text') from None
    if transformation == 'core-package-v1':
        # The first two generic comments are retained. Site-specific comments
        # following them are discarded without encoding their contents here.
        lines = text.splitlines(keepends=True)
        if len(lines) < 5 or any(not line.startswith('#') for line in lines[:4]) or lines[4].strip():
            raise ValueError('HA package header changed; review the transformation')
        text = ''.join(lines[:2] + lines[4:])
        text = substitute(text, r'^    url: .+$', '    url: !secret ha_core_updater_approve_url', 1)
        text = substitute(text, r'^(\s+)notify_service: .+$', r'\1notify_service: !secret optional_phone_notify_service', 3)
    elif transformation == 'core-tests-v1':
        text = substitute(text, re.escape(" / 'config/packages/core_updates.yaml'"), " / 'homeassistant/packages/core_updates.yaml'", 1)
        text = substitute(text, re.escape("FixtureLoader.add_constructor('!secret', lambda loader, node: 'Bearer TEST_ONLY')"),
                          "FixtureLoader.add_constructor('!secret', lambda loader, node: {\n"
                          "    'ha_core_updater_authorization': 'Bearer TEST_ONLY',\n"
                          "    'ha_core_updater_approve_url': 'http://ha-core-updater.homeassistant.svc.cluster.local:8099/approve',\n"
                          "    'optional_phone_notify_service': 'notify.mobile_app_example_phone',\n"
                          "}[node.value])", 1)
        text = substitute(text, r"self\.assertEqual\(notification\['notify_service'\], '[^']+'\)",
                          "self.assertEqual(notification['notify_service'], 'notify.mobile_app_example_phone')", 1)
    elif transformation == 'hacs-tests-v1':
        text = substitute(text, re.escape(" / 'config/packages/hacs_maintenance.yaml'"), " / 'homeassistant/packages/hacs_maintenance.yaml'", 1)
        text = substitute(text, re.escape('except ImportError:\n    HA_AVAILABLE = False'),
                          "except ImportError:\n    if os.environ.get('REQUIRE_HA_RUNTIME') == '1':\n        raise\n    HA_AVAILABLE = False", 1)
    elif transformation != 'identity-v1':
        raise ValueError('Unknown HA source transformation')
    return text.encode('utf-8')


def read_lock(root=ROOT):
    path = root / LOCK
    if path.resolve() != root.resolve() / LOCK or not path.is_file():
        raise ValueError('HA source lock must be a regular, non-symlink file')
    lock = json.loads(path.read_text())
    if (not isinstance(lock, dict) or set(lock) != {'format', 'repository', 'revision', 'files'}
            or type(lock['format']) is not int or lock['format'] != 1 or lock['repository'] != REPOSITORY):
        raise ValueError('Invalid HA source lock')
    exact_revision(lock['revision'])
    if not isinstance(lock['files'], list) or len(lock['files']) != len(FILES):
        raise ValueError('Invalid HA source inventory')
    for entry, (destination, source, transformation) in zip(lock['files'], FILES):
        expected = {'destination': destination, 'source': source, 'transformation': transformation}
        if (not isinstance(entry, dict) or set(entry) != set(expected) | {'source_sha256', 'output_sha256'}
                or any(entry[key] != value for key, value in expected.items())):
            raise ValueError('HA source inventory differs from the reviewed paths')
        if any(not isinstance(entry[key], str) or not re.fullmatch(r'[0-9a-f]{64}', entry[key]) for key in ('source_sha256', 'output_sha256')):
            raise ValueError('Invalid HA source digest')
    return lock


def checked_output(destination, data):
    problems = findings(destination, data.decode('utf-8'))
    if problems:
        raise ValueError('Generated HA source failed privacy checks: ' + json.dumps(problems))


def check(root=ROOT, upstream=False, fetch=fetch_source):
    lock = read_lock(root)
    for entry in lock['files']:
        path = root / entry['destination']
        if path.resolve() != root.resolve() / entry['destination'] or not path.is_file():
            raise ValueError('Generated HA source must be a regular, non-symlink file')
        output = path.read_bytes()
        if digest(output) != entry['output_sha256']:
            raise ValueError(f"Generated HA source changed independently: {entry['destination']}")
        checked_output(entry['destination'], output)
        if upstream:
            original = fetch(lock['revision'], entry['source'])
            if digest(original) != entry['source_sha256']:
                raise ValueError(f"Pinned HA source digest mismatch: {entry['source']}")
            generated = transform(original, entry['transformation'])
            checked_output(entry['destination'], generated)
            if generated != output:
                raise ValueError(f"Pinned HA regeneration mismatch: {entry['destination']}")
    return lock


def refresh(revision, root=ROOT, fetch=fetch_source):
    """Prepare all five reviewed outputs in memory before changing any file."""
    exact_revision(revision)
    lock_path = root / LOCK
    if lock_path.resolve() != root.resolve() / LOCK or (lock_path.exists() and not lock_path.is_file()):
        raise ValueError('HA source lock must be a regular path without symlinks')
    lock = {'format': 1, 'repository': REPOSITORY, 'revision': revision, 'files': []}
    outputs = []
    for destination, source, transformation in FILES:
        original = fetch(revision, source)
        output = transform(original, transformation)
        checked_output(destination, output)
        path = root / destination
        if path.resolve() != root.resolve() / destination or (path.exists() and not path.is_file()):
            raise ValueError('Generated HA source must be a regular path without symlinks')
        lock['files'].append({'destination': destination, 'source': source, 'transformation': transformation,
                              'source_sha256': digest(original), 'output_sha256': digest(output)})
        outputs.append((path, output))
    for path, output in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(output)
    (root / LOCK).write_text(json.dumps(lock, indent=2) + '\n')
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest='command', required=True)
    verify = subcommands.add_parser('check', help='Check output digests; optionally reproduce from pinned upstream')
    verify.add_argument('--upstream', action='store_true')
    update = subcommands.add_parser('refresh', help='Explicitly review and import a new full upstream commit SHA')
    update.add_argument('--revision', required=True)
    args = parser.parse_args()
    try:
        lock = refresh(args.revision) if args.command == 'refresh' else check(upstream=args.upstream)
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + '\n')
    print(f"Verified {len(lock['files'])} generated HA files from {REPOSITORY}@{lock['revision']}.")


if __name__ == '__main__':
    main()
