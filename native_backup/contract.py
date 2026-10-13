"""Read-only, fail-closed eligibility check for the native Synology backup path.

Run this module inside the candidate Home Assistant image, without starting HA,
mounting production config, or permitting network access. Version numbers alone
do not establish compatibility: the reviewed source and dependencies must match.
"""
import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import re
import sys


CONTRACT_ID = 'ha-container-synology-stream-v1'
REVIEWED_DEPENDENCIES = {'securetar': '2026.4.1', 'py-synologydsm-api': '2.10.3'}
REVIEWED_PYTHON = (3, 14)
# Exact upstream tag contents, reviewed 2026-10-08; no production configuration.
REVIEWED_PROFILES = {'2026.10.0': {'files': {'components/backup/__init__.py': 'c51b1b0ca9e5bd3b3b656e522973486d9278f2449d42b788bf1854c1b7655731',
                         'components/backup/agent.py': 'e7dc56534bf98b1bb29186f865eca4eef41501cfc913f83a72e9d0228f811949',
                         'components/backup/backup.py': '1848ccdac965db2c7af52615552136d16c3bc5bd73bafcd40520fda9b6c63a07',
                         'components/backup/config.py': 'bdc57a88d6bd1b7eda1c8957b767bdd2a5bd034b67fe9576fa975a97935f91e5',
                         'components/backup/config_flow.py': '255ea0aaba541e434913e2efb57967f1b5d8c0aa4c17859c1c243990505a68d1',
                         'components/backup/const.py': 'b9fef279d016c74055a399993aef05d903e0278bef2445e01beeee9f28b61c36',
                         'components/backup/coordinator.py': '284b05ae8c141c5c6533ee008dca349c055e30d087d0211a4b0d7c7013f4bf1b',
                         'components/backup/diagnostics.py': '5cbff853f89a030053fb1f3486d1626ef2454edb6119759363fb7cd55530696e',
                         'components/backup/entity.py': '06a60d482d4aea202f8ec50de6a3964873037147fcba330a37083aca28ecd6bb',
                         'components/backup/event.py': '9d8d2745fcd3323c395b8d9a24b0aa5f5657552f9a8b7a1925f9fd9c8efc6b2b',
                         'components/backup/http.py': '311988e30beeecde7b36bdc9f4a3eabe1d245d1b50cb051c012e6c2a25c06912',
                         'components/backup/manager.py': '634df1878a2cb4311d7c574779d8dfdaa1c4f71fb98ba01638578176025f20b7',
                         'components/backup/models.py': '061c8345454ad7058e49c199a13f8e555d86746cda1ff261044ed04142d234b9',
                         'components/backup/onboarding.py': 'a9fafca03a5d9de6b679c8f95323402703624c47bedfa55333d531137a05fe26',
                         'components/backup/sensor.py': 'cc2885c5f7b1c7bd206ade9d32601cb5ec966bc983ac71dd98909038375ede47',
                         'components/backup/services.py': '72c780fa2ec4c294fcc3d378dc55e0317a67710450139a9e76260b9c52fe955d',
                         'components/backup/store.py': 'b6360e97779ef3ba66f1b939593f7c1b6339ca068f7558b986f33c70fa45a1de',
                         'components/backup/util.py': 'c449129149ff6bf7df4dc66a5e9a555d2137d78a0df18debfcd87e2ae762bd7c',
                         'components/backup/websocket.py': '8ba4e047c61e3b4a7734151f64c9503b836abd7939f556a041a3679477825a45',
                         'components/synology_dsm/backup.py': '153ca5749e70aeef6c038a4cd424e0dd0d3433f2096dd7069737180b71359918'},
               'source_sha256': '3ad35fc1d066f66c25e1c51cbad522eb9e381bb2353fbfbfddec4296efe1b76c'},
 '2026.9.1': {'files': {'components/backup/__init__.py': 'c51b1b0ca9e5bd3b3b656e522973486d9278f2449d42b788bf1854c1b7655731',
                        'components/backup/agent.py': 'e7dc56534bf98b1bb29186f865eca4eef41501cfc913f83a72e9d0228f811949',
                        'components/backup/backup.py': '1848ccdac965db2c7af52615552136d16c3bc5bd73bafcd40520fda9b6c63a07',
                        'components/backup/config.py': 'bdc57a88d6bd1b7eda1c8957b767bdd2a5bd034b67fe9576fa975a97935f91e5',
                        'components/backup/config_flow.py': '255ea0aaba541e434913e2efb57967f1b5d8c0aa4c17859c1c243990505a68d1',
                        'components/backup/const.py': 'b9fef279d016c74055a399993aef05d903e0278bef2445e01beeee9f28b61c36',
                        'components/backup/coordinator.py': '284b05ae8c141c5c6533ee008dca349c055e30d087d0211a4b0d7c7013f4bf1b',
                        'components/backup/diagnostics.py': '5cbff853f89a030053fb1f3486d1626ef2454edb6119759363fb7cd55530696e',
                        'components/backup/entity.py': '06a60d482d4aea202f8ec50de6a3964873037147fcba330a37083aca28ecd6bb',
                        'components/backup/event.py': '9d8d2745fcd3323c395b8d9a24b0aa5f5657552f9a8b7a1925f9fd9c8efc6b2b',
                        'components/backup/http.py': '311988e30beeecde7b36bdc9f4a3eabe1d245d1b50cb051c012e6c2a25c06912',
                        'components/backup/manager.py': '634df1878a2cb4311d7c574779d8dfdaa1c4f71fb98ba01638578176025f20b7',
                        'components/backup/models.py': '061c8345454ad7058e49c199a13f8e555d86746cda1ff261044ed04142d234b9',
                        'components/backup/onboarding.py': '2d34dae58d488fa7f9513fdf850efcd5fdff4321c1d49c9ff19239d3b5d8e2b9',
                        'components/backup/sensor.py': 'cc2885c5f7b1c7bd206ade9d32601cb5ec966bc983ac71dd98909038375ede47',
                        'components/backup/services.py': '2c51c5886651ad12f7f38dd82d99455fe633de559e23edb6aa11eb0430b0cabb',
                        'components/backup/store.py': '333ff88fb4a6be033d5fe984ab624fc8be9cc51533175c49455e4597c4857b01',
                        'components/backup/util.py': '3349eaebd4dcdf56761e5a8164acf58a4479525b846a2a1dd21f2eb70d1bf3ef',
                        'components/backup/websocket.py': '197832c0cea884f70d033cbd0de9750cfd3771f250bb0764ffe1af173820be41',
                        'components/synology_dsm/backup.py': '153ca5749e70aeef6c038a4cd424e0dd0d3433f2096dd7069737180b71359918'},
              'source_sha256': 'e1f9d32e731aef867bde68bd594175a2e13f1786529c3cf878d2a3cf7d9a27e6'}}


def source_digest(fingerprints):
    """Canonical package fingerprint, including the module inventory."""
    return hashlib.sha256(json.dumps(
        fingerprints, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_snapshot(version, fingerprints, dependencies, python_minor):
    """Accept a stable version only when a complete reviewed profile matches."""
    if not isinstance(version, str) or not re.fullmatch(r'20\d{2}\.(?:[1-9]|1[0-2])\.\d+', version):
        raise RuntimeError('Native backup contract requires a stable Home Assistant version')
    if tuple(python_minor) != REVIEWED_PYTHON:
        raise RuntimeError('Native backup Python runtime requires review')
    if dependencies != REVIEWED_DEPENDENCIES:
        raise RuntimeError('Native backup dependency versions require review')
    digest = source_digest(fingerprints)
    for reviewed_version, profile in REVIEWED_PROFILES.items():
        if digest == profile['source_sha256'] and fingerprints == profile['files']:
            return {'status': 'compatible', 'allowed': True,
                    'contract_id': CONTRACT_ID, 'homeassistant_version': version,
                    'reviewed_source_version': reviewed_version,
                    'source_sha256': digest, 'dependencies': dependencies}
    raise RuntimeError('Native backup source differs from every reviewed contract')


def verify_runtime_contract(expected_version=None):
    """Inspect installed package files; no HA imports, setup or service calls."""
    try:
        distribution = importlib.metadata.distribution('homeassistant')
        version = distribution.version
        if expected_version is not None and version != expected_version:
            raise RuntimeError('Installed Home Assistant version does not match the requested image')
        # Official containers use an editable install under /usr/src rather
        # than placing the source next to distribution metadata.
        spec = importlib.util.find_spec('homeassistant')
        if spec is None or not spec.submodule_search_locations or len(spec.submodule_search_locations) != 1:
            raise RuntimeError('Native backup source location is unavailable or ambiguous')
        root = Path(next(iter(spec.submodule_search_locations)))
        backup = root / 'components/backup'
        paths = sorted(backup.rglob('*.py'))
        paths.append(root / 'components/synology_dsm/backup.py')
        fingerprints = {}
        for path in paths:
            if path.is_symlink() or not path.is_file():
                raise RuntimeError('Native backup contract requires regular installed source files')
            fingerprints[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        dependencies = {name: importlib.metadata.version(name) for name in REVIEWED_DEPENDENCIES}
        return validate_snapshot(version, fingerprints, dependencies, sys.version_info[:2])
    except (OSError, importlib.metadata.PackageNotFoundError) as error:
        raise RuntimeError('Native backup contract dependencies or source files are unavailable') from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect-version', help='Require this exact installed stable version')
    args = parser.parse_args()
    try:
        result = verify_runtime_contract(args.expect_version)
    except RuntimeError as error:
        print(json.dumps({'status': 'incompatible', 'allowed': False,
                          'contract_id': CONTRACT_ID, 'reason': str(error)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
