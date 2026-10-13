"""Fail-closed backup eligibility, independently of HA and the Kubernetes API."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_contract', ROOT / 'native_backup/contract.py')
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


class BackupContractTest(unittest.TestCase):
    def snapshot(self, version='2026.10.0', files=None, dependencies=None, python_minor=(3, 14)):
        return contract.validate_snapshot(version,
            files if files is not None else contract.REVIEWED_PROFILES['2026.10.0']['files'],
            dependencies if dependencies is not None else contract.REVIEWED_DEPENDENCIES.copy(),
            python_minor)

    def test_both_independently_reviewed_releases_qualify(self):
        for version, profile in contract.REVIEWED_PROFILES.items():
            with self.subTest(version=version):
                result = self.snapshot(version, profile['files'])
                self.assertTrue(result['allowed'])
                self.assertEqual(result['status'], 'compatible')
                self.assertEqual(result['homeassistant_version'], version)
                self.assertEqual(result['reviewed_source_version'], version)
                self.assertEqual(result['source_sha256'], profile['source_sha256'])

    def test_future_stable_release_needs_identical_reviewed_contract(self):
        result = self.snapshot('2026.10.1')
        self.assertTrue(result['allowed'])
        self.assertEqual(result['homeassistant_version'], '2026.10.1')
        self.assertEqual(result['reviewed_source_version'], '2026.10.0')

    def test_known_version_does_not_bypass_changed_source(self):
        for operation in ('changed', 'missing', 'added'):
            files = contract.REVIEWED_PROFILES['2026.10.0']['files'].copy()
            if operation == 'changed':files['components/backup/manager.py'] = '0' * 64
            elif operation == 'missing':files.pop('components/backup/manager.py')
            else:files['components/backup/new_archive.py'] = '0' * 64
            with self.subTest(operation=operation), self.assertRaisesRegex(RuntimeError, 'source differs'):
                self.snapshot(files=files)

    def test_mixed_reviewed_profiles_are_not_implicitly_reviewed(self):
        files = contract.REVIEWED_PROFILES['2026.10.0']['files'].copy()
        files['components/backup/util.py'] = contract.REVIEWED_PROFILES['2026.9.1']['files']['components/backup/util.py']
        with self.assertRaisesRegex(RuntimeError, 'source differs'):
            self.snapshot(files=files)

    def test_unreviewed_dependencies_or_python_fail_closed(self):
        for dependency in contract.REVIEWED_DEPENDENCIES:
            versions = contract.REVIEWED_DEPENDENCIES.copy();versions[dependency] = '9999.1'
            with self.subTest(dependency=dependency), self.assertRaisesRegex(RuntimeError, 'dependency'):
                self.snapshot(dependencies=versions)
        with self.assertRaisesRegex(RuntimeError, 'Python'):
            self.snapshot(python_minor=(3, 15))

    def test_prerelease_development_and_mutable_tags_never_qualify(self):
        for version in ('2026.10.0b1', '2026.11.0.dev0', 'stable', 'latest', '', None):
            with self.subTest(version=version), self.assertRaisesRegex(RuntimeError, 'stable'):
                self.snapshot(version)


class InstalledContractTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.files = {'components/backup/manager.py': b'reviewed manager\n',
                      'components/synology_dsm/backup.py': b'reviewed uploader\n'}
        for name, value in self.files.items():
            path = self.root / name;path.parent.mkdir(parents=True, exist_ok=True);path.write_bytes(value)
        fingerprints = {name: hashlib.sha256(value).hexdigest() for name, value in self.files.items()}
        profile = {'files': fingerprints, 'source_sha256': contract.source_digest(fingerprints)}
        self.patches = [
            patch.object(contract, 'REVIEWED_PROFILES', {'2026.10.0': profile}),
            patch.object(contract.importlib.metadata, 'distribution', return_value=types.SimpleNamespace(version='2026.10.0')),
            patch.object(contract.importlib.metadata, 'version', side_effect=contract.REVIEWED_DEPENDENCIES.__getitem__),
            patch.object(contract.importlib.util, 'find_spec', return_value=types.SimpleNamespace(submodule_search_locations=[str(self.root)])),
            patch.object(sys, 'version_info', (3, 14, 6)),
        ]
        for item in self.patches:item.start();self.addCleanup(item.stop)

    def test_inspects_editable_install_without_importing_ha(self):
        result = contract.verify_runtime_contract('2026.10.0')
        self.assertTrue(result['allowed'])

    def test_candidate_tag_must_match_installed_distribution(self):
        with self.assertRaisesRegex(RuntimeError, 'requested image'):
            contract.verify_runtime_contract('2026.10.1')

    def test_on_disk_modification_is_detected(self):
        (self.root / 'components/backup/manager.py').write_text('changed\n')
        with self.assertRaisesRegex(RuntimeError, 'source differs'):
            contract.verify_runtime_contract()

    def test_added_recursive_module_is_detected(self):
        new = self.root / 'components/backup/helpers/new.py';new.parent.mkdir();new.write_text('new\n')
        with self.assertRaisesRegex(RuntimeError, 'source differs'):
            contract.verify_runtime_contract()

    def test_symlinked_source_is_refused(self):
        path = self.root / 'components/backup/manager.py';path.unlink();path.symlink_to(self.root / 'components/synology_dsm/backup.py')
        with self.assertRaisesRegex(RuntimeError, 'regular'):
            contract.verify_runtime_contract()
