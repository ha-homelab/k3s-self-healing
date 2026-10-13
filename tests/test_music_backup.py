"""Exercise the local music backup with an active WAL database and failed capture."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from disaster_recovery import music
from disaster_recovery.restore import verify_snapshot


class MusicBackupTests(unittest.TestCase):
    def test_wal_state_restores_and_failure_preserves_last_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, backups = base / 'data', base / 'backups'
            source.mkdir()
            (source / 'settings.json').write_text('{"server_id":"fixture"}')
            (source / 'musicassistant.log').write_text('volatile log')
            with sqlite3.connect(source / 'library.db') as database:
                database.execute('PRAGMA journal_mode=WAL')
                database.execute('CREATE TABLE tracks (name TEXT)')
                database.execute("INSERT INTO tracks VALUES ('music')")
                database.commit()
                music.run(source, backups)
                success = (backups / 'latest-success.json').read_bytes()
                snapshot = backups / json.loads(success)['snapshot']
                target = base / 'restore'
                verify_snapshot(snapshot, extract_to=target)
                with sqlite3.connect(target / 'music-assistant/library.db') as restored:
                    self.assertEqual(restored.execute('SELECT name FROM tracks').fetchall(), [('music',)])
                self.assertFalse((target / 'music-assistant/musicassistant.log').exists())
                with patch.object(music, 'archive_component', side_effect=RuntimeError('fixture')):
                    with self.assertRaises(RuntimeError):
                        music.run(source, backups)
                self.assertEqual((backups / 'latest-success.json').read_bytes(), success)
                self.assertEqual(json.loads((backups / 'latest-attempt.json').read_text())['state'], 'failed')


if __name__ == '__main__':
    unittest.main()
