"""Public configuration remains portable without weakening deployment guards."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from site_profile import DEFAULTS, load_profile, materialize, recovery_objects


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'site.json'

    def load(self, values, **kwargs):
        self.path.write_text(json.dumps(values))
        return load_profile(self.path, **kwargs)

    def test_examples_render_but_cannot_be_deployed(self):
        self.assertEqual(self.load(DEFAULTS), DEFAULTS)
        with self.assertRaisesRegex(ValueError, 'Documentation'):
            self.load(DEFAULTS, applying=True)

    def test_inline_secrets_and_unknown_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            self.load(dict(DEFAULTS, password='fixture'))

    def test_traversal_and_interpolation_are_rejected(self):
        for value in ('/exports/../private', '/exports//private', '/exports/"secret', '/'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(dict(DEFAULTS, nas_export=value))

    def test_substitution_does_not_rewrite_inserted_values(self):
        profile = dict(DEFAULTS, node='example-lan-node', lan_node='other-node')
        self.assertEqual(materialize({'node': 'example-ha-node'}, profile), {'node': 'example-lan-node'})

    def test_unrequested_music_backup_is_excluded(self):
        values = [{'metadata': {'name': 'music-assistant-home-backup'}, 'kind': 'CronJob'}]
        self.assertEqual(recovery_objects(values, DEFAULTS), [])


class RepositoryBoundaryTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / 'core_updates'))
        import worker
        self.worker = worker
        sys.path.pop(0)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_unconfigured_or_unsafe_remote_never_runs_git(self):
        for remote in ('--upload-pack=bad', '/tmp/local', 'https://github.com/example/repo',
                       'git@github.com:example/ha-deployment.git'):
            source = self.worker.Source({'repository': remote})
            source.path = Path(self.temp.name)
            source.git = Mock()
            with self.subTest(remote=remote), self.assertRaises(RuntimeError):
                source.prepare()
            source.git.assert_not_called()

    def test_configured_remote_must_match_existing_checkout(self):
        source = self.worker.Source({'repository': 'git@github.com:operator/deployment.git'})
        source.path = Path(self.temp.name)
        source.git = Mock(return_value='git@github.com:unexpected/deployment.git')
        with self.assertRaisesRegex(RuntimeError, 'unexpected origin'):
            source.prepare()
        self.assertEqual(source.git.call_count, 1)

    def test_clean_configured_checkout_fetches_only_expected_branch(self):
        remote = 'git@github.com:operator/deployment.git'
        source = self.worker.Source({'repository': remote})
        source.path = Path(self.temp.name)
        source.git = Mock(side_effect=[remote, '', '', '', 'a' * 40])
        self.assertEqual(source.prepare(), 'a' * 40)
        self.assertIn(unittest.mock.call('fetch', '--no-tags', 'origin', 'main'), source.git.call_args_list)
