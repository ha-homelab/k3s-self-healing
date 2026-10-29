"""Disposable config copy preserves internal aliases without reading their targets."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch


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
        disk = SimpleNamespace(total=64 * 1024**3, free=32 * 1024**3)
        mocked_disk = patch.object(preflight.shutil, 'disk_usage', return_value=disk)
        self.disk_usage = mocked_disk.start()
        self.addCleanup(mocked_disk.stop)

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

    def test_schema_only_runtime_directories_do_not_consume_copy_budget(self):
        excluded = ['.storage/home_floorplan_generated_assets',
                    '.storage/home_floorplan_studio_assets',
                    '.storage/home_floorplan/studio',
                    '.home-studio-backups', '.home-studio-install-20261029',
                    '.asterisk-telephony-backup-fixture', '.epg-channel-backup-fixture',
                    '.epg-media-backup-fixture', '.ha-stale-retirement-fixture',
                    '.recorder-quality-backup-fixture']
        for name in excluded:
            self.write(name + '/generated.bin', 'x' * 100)
        self.write('configuration.yaml', 'keep')
        report = self.copy(limit=4)
        self.assertEqual(report['configuration_bytes'], 4)
        self.assertEqual(report['files'], 1)
        for name in excluded:
            self.assertFalse((self.target / name).exists(), name)

    def test_schema_runtime_filter_keeps_registries_neighbors_and_component_assets(self):
        kept = ['.storage/core.config_entries', '.storage/core.entity_registry',
                'configuration.yaml', 'secrets.yaml', 'packages/lighting.yaml',
                '.storage/home_floorplan_generated_studio', '.storage/home_floorplan_studio',
                '.storage/home_floorplan_map', '.storage/home_floorplan_curtains',
                '.storage/home_floorplan_access_sessions',
                '.storage/home_floorplan_generated_assets.backup/metadata.json',
                '.storage/home_floorplan/studio_settings.json',
                '.home-studio-backups-old/metadata.json',
                '.home-studio-installer/metadata.json',
                'packages/.home-studio-install-example/automation.yaml',
                'custom_components/home_floorplan/__init__.py',
                'custom_components/home_floorplan/runtime/site.json',
                'custom_components/home_floorplan/runtime/private_site_config.json',
                'custom_components/home_floorplan/runtime/assets/model.glb',
                'custom_components/home_floorplan/assets/model.glb']
        for name in kept:
            self.write(name, 'keep')
        report = self.copy()
        self.assertEqual(report['files'], len(kept))
        for name in kept:
            self.assertEqual((self.target / name).read_text(), 'keep', name)

    def test_runtime_named_regular_files_are_not_excluded(self):
        kept = ['.storage/home_floorplan_generated_assets',
                '.storage/home_floorplan_studio_assets', '.storage/home_floorplan/studio',
                '.home-studio-backups', '.home-studio-install-store',
                '.asterisk-telephony-backup-store', '.epg-channel-backup-store',
                '.epg-media-backup-store', '.ha-stale-retirement-store',
                '.recorder-quality-backup-store']
        for name in kept:
            self.write(name, 'store')
        self.assertEqual(self.copy()['files'], len(kept))
        for name in kept:
            self.assertEqual((self.target / name).read_text(), 'store', name)

    def test_maintenance_prefixes_keep_nested_adjacent_and_aliased_inputs(self):
        prefixes = ['.asterisk-telephony-backup-', '.epg-channel-backup-',
                    '.epg-media-backup-', '.ha-stale-retirement-', '.recorder-quality-backup-']
        self.write('required/config.yaml', 'keep')
        for prefix in prefixes:
            self.write('packages/' + prefix + 'fixture/config.yaml', 'keep')
            self.write(prefix.rstrip('-') + '_settings/config.yaml', 'keep')
            self.link(prefix + 'alias', '/config/required')
        self.copy()
        for prefix in prefixes:
            self.assertEqual((self.target / ('packages/' + prefix + 'fixture/config.yaml')).read_text(), 'keep')
            self.assertEqual((self.target / (prefix.rstrip('-') + '_settings/config.yaml')).read_text(), 'keep')
            self.assertEqual((self.target / (prefix + 'alias/config.yaml')).read_text(), 'keep')

    def test_maintenance_named_external_alias_is_still_refused(self):
        self.link('.epg-channel-backup-alias', '/outside')
        with self.assertRaisesRegex(RuntimeError, 'external'):
            self.copy()

    def test_runtime_named_internal_alias_keeps_normal_confinement(self):
        self.write('required/metadata.json', 'keep')
        name = '.storage/home_floorplan_generated_assets'
        self.link(name, '/config/required')
        self.assertEqual(self.copy()['symlinks'], 1)
        self.assertEqual((self.target / name).resolve(), self.target / 'required')
        self.assertEqual((self.target / name / 'metadata.json').read_text(), 'keep')

    def test_runtime_named_external_alias_is_still_refused(self):
        self.link('.storage/home_floorplan_generated_assets', '/outside')
        with self.assertRaisesRegex(RuntimeError, 'external'):
            self.copy()

    def test_alias_to_omitted_runtime_stays_dangling_inside_copy(self):
        self.write('.storage/home_floorplan_studio_assets/model.glb', 'runtime')
        self.link('runtime_alias', '/config/.storage/home_floorplan_studio_assets')
        self.copy()
        self.assertTrue((self.target / 'runtime_alias').is_symlink())
        self.assertEqual((self.target / 'runtime_alias').resolve(),
                         self.target / '.storage/home_floorplan_studio_assets')
        self.assertFalse((self.target / 'runtime_alias').exists())

    def test_special_files_are_refused_without_opening(self):
        os.mkfifo(self.source / 'fifo')
        with self.assertRaisesRegex(RuntimeError, 'special file'):
            self.copy()

    def test_byte_limit_is_enforced_and_source_is_untouched(self):
        path = self.write('configuration.yaml', '12345')
        with self.assertRaisesRegex(RuntimeError, 'byte budget'):
            self.copy(limit=4)
        self.assertEqual(path.read_text(), '12345')
        self.assertFalse(self.target.exists())

    def test_capacity_counts_allocated_files_directories_and_links_only_once(self):
        self.write('required/file.yaml', 'x' * 4097)
        self.link('alias', '/config/required/file.yaml')
        self.write('.home-studio-backups/large.bin', 'x' * 10000)
        with patch.object(preflight, 'wait_for_copy_capacity') as capacity, \
                patch.object(preflight.os, 'statvfs', return_value=SimpleNamespace(f_frsize=4096)):
            self.copy()
        # Root + directory + file entry + two data blocks + link.
        self.assertEqual(capacity.call_args_list[0].args, (self.target.parent, 6 * 4096))

    def test_capacity_failure_happens_before_destination_creation(self):
        self.write('configuration.yaml', 'keep')
        self.disk_usage.return_value.free = 0
        with patch.object(preflight.time, 'monotonic', side_effect=[0, 300]), \
                patch.object(preflight.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'free space'):
                self.copy()
        sleep.assert_not_called()
        self.assertFalse(self.target.exists())

    def test_capacity_recovery_waits_without_creating_payload(self):
        self.write('configuration.yaml', 'keep')
        calls = []

        def disk_usage(_path):
            calls.append(self.target.exists())
            return SimpleNamespace(total=64 * 1024**3,
                                   free=0 if len(calls) == 1 else 32 * 1024**3)

        self.disk_usage.side_effect = disk_usage
        with patch.object(preflight.time, 'monotonic', return_value=0), \
                patch.object(preflight.time, 'sleep') as sleep:
            self.copy()
        sleep.assert_called_once_with(5)
        self.assertEqual(calls[:2], [False, False])
        self.assertEqual((self.target / 'configuration.yaml').read_text(), 'keep')

    def test_capacity_wait_is_bounded_to_300_seconds(self):
        self.disk_usage.return_value.free = 0
        elapsed = [0]

        def sleep(seconds):
            elapsed[0] += seconds

        with patch.object(preflight.time, 'monotonic', side_effect=lambda: elapsed[0]), \
                patch.object(preflight.time, 'sleep', side_effect=sleep), \
                patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'free space'):
                self.copy()
        self.assertEqual(elapsed[0], 300)
        self.assertFalse(self.target.exists())

    def test_concurrent_space_loss_stops_before_writing_payload(self):
        self.write('configuration.yaml', 'keep')
        self.disk_usage.side_effect = [SimpleNamespace(total=64 * 1024**3, free=32 * 1024**3),
                                       SimpleNamespace(total=64 * 1024**3, free=0)]
        with self.assertRaisesRegex(RuntimeError, 'free space'):
            self.copy()
        self.assertFalse((self.target / 'configuration.yaml').exists())
        self.assertEqual((self.source / 'configuration.yaml').read_text(), 'keep')

    def test_source_growth_after_measurement_still_enforces_byte_cap(self):
        source = self.write('configuration.yaml', 'keep')

        def capacity(_path, _bytes, wait_seconds=300):
            if wait_seconds:
                source.write_text('too large')

        with patch.object(preflight, 'wait_for_copy_capacity', side_effect=capacity):
            with self.assertRaisesRegex(RuntimeError, 'byte budget'):
                self.copy(limit=4)
        self.assertFalse((self.target / 'configuration.yaml').exists())

    def test_copy_reserve_matches_native_backup_policy(self):
        source = ast.parse((ROOT / 'native_backup/runner.py').read_text())
        function = next(item for item in source.body
                        if isinstance(item, ast.FunctionDef) and item.name == 'reserve_bytes')
        namespace = {'GIB': 1024**3}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<reserve policy>', 'exec'), namespace)
        for total in [0, 40 * 1024**3, 64 * 1024**3, 1024**4, 64 * 1024**3 + 1]:
            self.assertEqual(preflight.copy_reserve_bytes(total), namespace['reserve_bytes'](total))

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
