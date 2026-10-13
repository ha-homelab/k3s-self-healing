"""Disposable config copy preserves internal aliases without reading their targets."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('core_preflight', ROOT / 'core_updates/preflight.py')
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class ConfigurationCopyTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / 'source';self.source.mkdir()
        self.target = self.root / 'copy'

    def write(self, name, content='test'):
        path = self.source / name;path.parent.mkdir(exist_ok=True, parents=True);path.write_text(content)
        return path

    def link(self, name, target):
        path = self.source / name;path.parent.mkdir(exist_ok=True, parents=True);path.symlink_to(target)

    def copy(self, **kwargs):
        return preflight.copy_configuration(self.source, self.target, **kwargs)

    def test_root_alias_and_staging_aliases_stay_in_copy(self):
        self.write('configuration.yaml', 'fixture')
        self.write('custom_components/example/__init__.py', '# fixture')
        self.link('config', '/config')
        self.link('.staging/custom_components', '/config/custom_components')
        self.link('.staging/config', '/config/config')
        report = self.copy()
        self.assertEqual(report['files'], 2)
        self.assertEqual(report['symlinks'], 3)
        self.assertEqual((self.target / 'config').resolve(), self.target)
        self.assertEqual((self.target / '.staging/config').resolve(), self.target)
        self.assertEqual((self.target / '.staging/custom_components').resolve(), self.target / 'custom_components')
        (self.target / 'config/configuration.yaml').write_text('changed')
        self.assertEqual((self.source / 'configuration.yaml').read_text(), 'fixture')

    def test_relative_chain_is_resolved_without_following_source(self):
        self.write('real/config.yaml', 'fixture')
        self.link('first', 'real')
        self.link('stage/second', '../first')
        report = self.copy()
        self.assertEqual(report['files'], 1)
        self.assertEqual((self.target / 'stage/second/config.yaml').read_text(), 'fixture')

    def test_external_absolute_link_is_refused(self):
        self.link('external', '/etc')
        with self.assertRaisesRegex(RuntimeError, 'external'):
            self.copy()

    def test_escape_after_expanding_parent_alias_is_refused(self):
        self.link('alias', '/config')
        self.link('escape', 'alias/../outside')
        with self.assertRaisesRegex(RuntimeError, 'escapes'):
            self.copy()

    def test_relative_escape_is_refused(self):
        self.link('escape', '../outside')
        with self.assertRaisesRegex(RuntimeError, 'escapes'):
            self.copy()

    def test_cycle_is_refused(self):
        self.link('a', 'b');self.link('b', 'a')
        with self.assertRaisesRegex(RuntimeError, 'cyclic'):
            self.copy()

    def test_missing_internal_target_remains_contained(self):
        self.link('missing', '/config/backups/removed.tar')
        self.copy()
        self.assertTrue((self.target / 'missing').is_symlink())
        self.assertEqual((self.target / 'missing').resolve(), self.target / 'backups/removed.tar')

    def test_exclusions_apply_only_to_root(self):
        self.write('backups/excluded', 'big')
        self.write('media/excluded', 'big')
        self.write('home-assistant.log', 'big')
        self.write('packages/media/lights.yaml', 'keep')
        self.write('custom_components/demo/backups/__init__.py', 'keep')
        report = self.copy()
        self.assertEqual(report['files'], 2)
        self.assertFalse((self.target / 'backups').exists())
        self.assertEqual((self.target / 'packages/media/lights.yaml').read_text(), 'keep')

    def test_special_files_are_refused_without_opening(self):
        os.mkfifo(self.source / 'fifo')
        with self.assertRaisesRegex(RuntimeError, 'special file'):
            self.copy()

    def test_byte_limit_is_enforced_and_source_is_untouched(self):
        path = self.write('configuration.yaml', '12345')
        with self.assertRaisesRegex(RuntimeError, 'byte budget'):
            self.copy(limit=4)
        self.assertEqual(path.read_text(), '12345')

    def test_existing_destination_is_never_overwritten(self):
        self.target.mkdir();(self.target / 'keep').write_text('keep')
        with self.assertRaisesRegex(RuntimeError, 'new destination'):
            self.copy()
        self.assertEqual((self.target / 'keep').read_text(), 'keep')

    def test_destination_inside_source_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'outside'):
            preflight.copy_configuration(self.source, self.source / 'nested')

    def test_source_root_symlink_is_not_followed(self):
        self.link('configuration.yaml', '/config/elsewhere')
        alias = self.root / 'source-alias';alias.symlink_to(self.source)
        with self.assertRaises(OSError):
            preflight.copy_configuration(alias, self.target)

    def test_absolute_config_alias_points_only_at_disposable_copy(self):
        self.write('configuration.yaml', 'fixture');self.copy()
        alias = self.root / 'image-config';alias.mkdir()
        preflight.expose_disposable_config(self.target, alias)
        self.assertEqual(alias.resolve(), self.target)
        (alias / 'configuration.yaml').write_text('copy only')
        self.assertEqual((self.source / 'configuration.yaml').read_text(), 'fixture')

    def test_nonempty_or_existing_symlink_alias_is_never_replaced(self):
        self.write('configuration.yaml', 'fixture');self.copy()
        alias = self.root / 'image-config';alias.mkdir();(alias / 'keep').write_text('keep')
        with self.assertRaisesRegex(RuntimeError, 'not empty'):
            preflight.expose_disposable_config(self.target, alias)
        self.assertEqual((alias / 'keep').read_text(), 'keep')
        link = self.root / 'existing-link';link.symlink_to(self.source)
        with self.assertRaisesRegex(RuntimeError, 'Cannot expose'):
            preflight.expose_disposable_config(self.target, link)
        self.assertEqual(link.resolve(), self.source)

    def test_replacing_image_workdir_does_not_leave_deleted_cwd(self):
        self.write('configuration.yaml', 'fixture');self.copy()
        alias = self.root / 'image-config';alias.mkdir()
        previous = Path.cwd()
        try:
            os.chdir(alias)
            preflight.expose_disposable_config(self.target, alias)
            self.assertEqual(Path.cwd(), self.target)
            self.assertEqual(alias.resolve(), self.target)
        finally:
            os.chdir(previous)


class ConfigurationReportTest(unittest.TestCase):
    def report(self, warnings=None, errors=None):
        return {"warnings": warnings or {}, "errors": errors or {},
                "total_warnings": len(warnings or {}), "total_errors": len(errors or {})}

    def test_clean_report_accepted(self):
        self.assertEqual(preflight.validate_config_report(self.report()), 0)

    def test_package_merge_warning_is_fatal_even_with_exit_zero(self):
        warning = {"homeassistant.packages.sample": ["Setup of package 'sample' failed: integration 'logger' has duplicate key 'rest_command'", {"loggers": {"rest_command": "warning"}}]}
        with self.assertRaisesRegex(RuntimeError, "package merge"):
            preflight.validate_config_report(self.report(warnings=warning), 0)

    def test_unrelated_legacy_warning_is_not_blindly_rejected(self):
        warning = {"example": ["The example integration uses deprecated configuration", {"private_config": "not logged"}]}
        self.assertEqual(preflight.validate_config_report(self.report(warnings=warning)), 1)

    def test_integration_failures_reported_as_warnings_are_fatal(self):
        for message in ("Integration error: example - Requirements not found",
                        "Component error: example - cannot import name example",
                        "Platform error 'sensor' from integration 'example' - not found",
                        "Invalid config for 'example' at configuration.yaml, line 2: invalid key"):
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, "integration configuration"):
                preflight.validate_config_report(self.report(warnings={"example": [message]}), 0)

    def test_schema_error_or_failed_process_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "validation failed"):
            preflight.validate_config_report(self.report(errors={"example": ["invalid"]}), 0)
        with self.assertRaisesRegex(RuntimeError, "validation failed"):
            preflight.validate_config_report(self.report(), 1)

    def test_missing_report_contract_is_rejected(self):
        for value in ({}, None, [], {"warnings": {}, "errors": {}, "total_errors": 0}):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                preflight.validate_config_report(value)


class PlaceholderTest(unittest.TestCase):
    def test_only_explicit_directories_below_live_external_mounts_are_allowed(self):
        result = preflight.validate_placeholder_directories(['/share', '/share/ssd/Music'], ['/config', '/share'])
        self.assertEqual(result, [{'path': '/share', 'mount': '/share'}, {'path': '/share/ssd/Music', 'mount': '/share'}])
        for path, mounts in [('/share/Music', []), ('/etc/example', ['/etc']), ('/config/media', ['/config']),
                             ('/app/example', ['/app']), ('/usr/example', ['/usr']), ('/share/../etc', ['/share']),
                             ('/share//Music', ['/share']), ('relative', ['/share']), ('/', ['/'])]:
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                preflight.validate_placeholder_directories([path], mounts)

    def test_empty_placeholders_are_created_without_any_source_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            specs = preflight.validate_placeholder_directories(['/share', '/share/ssd/Music'], ['/share'])
            self.assertEqual(preflight.prepare_placeholder_directories(specs, root), 2)
            self.assertTrue((root / 'share/ssd/Music').is_dir())
            self.assertEqual(list((root / 'share/ssd/Music').iterdir()), [])

    def test_nonempty_or_symlink_image_mount_is_never_overwritten(self):
        specs = [{'path': '/share/ssd/Music', 'mount': '/share'}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);shared = root / 'share';shared.mkdir();(shared / 'existing').write_text('keep')
            with self.assertRaisesRegex(RuntimeError, 'existing image data'):
                preflight.prepare_placeholder_directories(specs, root)
            self.assertEqual((shared / 'existing').read_text(), 'keep')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);(root / 'real').mkdir();(root / 'share').symlink_to(root / 'real')
            with self.assertRaisesRegex(RuntimeError, 'symlink'):
                preflight.prepare_placeholder_directories(specs, root)


class CheckerBootstrapTest(unittest.TestCase):
    def test_ha_logging_to_stdout_is_redirected_without_filtering_json(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / 'homeassistant';package.mkdir()
            (package / '__init__.py').write_text('')
            (package / '__main__.py').write_text(
                'import json,logging,sys\n'
                'logging.basicConfig(stream=sys.stdout,level=logging.INFO)\n'
                'logging.info("dependency install progress")\n'
                'print(json.dumps({"validated":True,"argv":sys.argv[1:]}))\n')
            result = subprocess.run([sys.executable, '-c', preflight.CHECK_CONFIG_BOOTSTRAP,
                                     'homeassistant', '--script', 'check_config', '--json'],
                                    env=dict(os.environ, PYTHONPATH=directory), capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), {'validated': True, 'argv': ['--script', 'check_config', '--json']})
            self.assertIn('dependency install progress', result.stderr)
