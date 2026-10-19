"""Exercise the local music backup with an active WAL database and failed capture."""
import json
import os
from pathlib import Path
import sqlite3
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch

from disaster_recovery import music
from disaster_recovery.restore import verify_snapshot


class MusicBackupTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'NODE_NAME': 'fixture-music-node',
                                             'SOURCE_DATA_CLAIM': 'music-assistant-home-data'})
        environment.start()
        self.addCleanup(environment.stop)

    def test_missing_or_invalid_node_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'backups'
            for node in ('', '../node', 'node..local'):
                with self.subTest(node=node), patch.dict(os.environ, {'NODE_NAME': node}):
                    with self.assertRaisesRegex(ValueError, 'NODE_NAME'):
                        music.run(Path(temporary), root)
                    self.assertFalse(root.exists())

    def test_wal_state_restores_and_failure_preserves_last_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, backups = base / 'data', base / 'backups'
            source.mkdir()
            (source / 'settings.json').write_text('{"server_id":"fixture"}')
            (source / 'musicassistant.log').write_text('volatile log')
            with closing(sqlite3.connect(source / 'library.db')) as database, database:
                database.execute('PRAGMA journal_mode=WAL')
                database.execute('CREATE TABLE tracks (name TEXT)')
                database.execute("INSERT INTO tracks VALUES ('music')")
                database.commit()
                music.run(source, backups)
                success = (backups / 'latest-success.json').read_bytes()
                snapshot = backups / json.loads(success)['snapshot']
                manifest = json.loads((snapshot / 'manifest.json').read_text())
                self.assertEqual(manifest['components'][0]['source']['node'], 'fixture-music-node')
                self.assertEqual(manifest['components'][0]['source']['claim'], 'music-assistant-home-data')
                target = base / 'restore'
                verify_snapshot(snapshot, extract_to=target)
                with closing(sqlite3.connect(target / 'music-assistant/library.db')) as restored:
                    self.assertEqual(restored.execute('SELECT name FROM tracks').fetchall(), [('music',)])
                self.assertFalse((target / 'music-assistant/musicassistant.log').exists())
                with patch.object(music, 'archive_component', side_effect=RuntimeError('fixture')):
                    with self.assertRaises(RuntimeError):
                        music.run(source, backups)
                self.assertEqual((backups / 'latest-success.json').read_bytes(), success)
                self.assertEqual(json.loads((backups / 'latest-attempt.json').read_text())['state'], 'failed')

    def test_claim_identity_follows_the_selected_mounted_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / 'data'
            source.mkdir()
            (source / 'settings.json').write_text('{}')
            for claim in (None, 'fixture-music-data'):
                backups = base / ('default' if claim is None else 'selected')
                with patch.dict(os.environ):
                    if claim is None:
                        os.environ.pop('SOURCE_DATA_CLAIM', None)
                    else:
                        os.environ['SOURCE_DATA_CLAIM'] = claim
                    music.run(source, backups)
                success = json.loads((backups / 'latest-success.json').read_text())
                snapshot = backups / success['snapshot']
                manifest = json.loads((snapshot / 'manifest.json').read_text())
                self.assertEqual(manifest['components'][0]['source']['claim'],
                                 claim or 'music-assistant-home-data')
                verify_snapshot(snapshot)

    def test_invalid_claim_fails_before_any_backup_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'backups'
            for claim in ('', '../claim', 'claim..local', 'Uppercase', 'x' * 254):
                with self.subTest(claim=claim), patch.dict(os.environ, {'SOURCE_DATA_CLAIM': claim}):
                    with self.assertRaisesRegex(ValueError, 'SOURCE_DATA_CLAIM'):
                        music.run(Path(temporary), root)
                    self.assertFalse(root.exists())


if __name__ == '__main__':
    unittest.main()
