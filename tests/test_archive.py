"""Offline archive acceptance: no cluster, NAS or device access."""

from contextlib import closing
import hashlib
import itertools
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from disaster_recovery import archive
from disaster_recovery.restore import verify_snapshot


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.output = self.root / "nas"
        self.source.mkdir()
        self.output.mkdir()
        self.destination = self.output / "component.tar.gz"

    def test_live_wal_database_becomes_valid_standalone_snapshot(self):
        database = self.source / "zigbee.db"
        live = sqlite3.connect(database)
        self.addCleanup(live.close)
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("CREATE TABLE network_backups (value TEXT)")
        live.execute("INSERT INTO network_backups VALUES ('paired network state')")
        live.commit()
        wal = Path(str(database) + "-wal")
        before = (database.read_bytes(), wal.read_bytes())

        with patch.object(archive, "_snapshot_quiescent_sqlite") as fallback:
            result = archive.create_archive(self.source, self.destination)
        fallback.assert_not_called()

        self.assertEqual(result["sqlite_databases"], ["zigbee.db"])
        self.assertEqual(result["sqlite_quiescent_copies"], [])
        self.assertEqual(before, (database.read_bytes(), wal.read_bytes()))
        with tarfile.open(self.destination) as captured:
            self.assertEqual(captured.getnames(), [".", "zigbee.db"])
            standalone = self.root / "restored.db"
            standalone.write_bytes(captured.extractfile("zigbee.db").read())
        with closing(sqlite3.connect(standalone)) as restored:
            self.assertEqual(restored.execute("PRAGMA quick_check").fetchall(), [("ok",)])
            self.assertEqual(
                restored.execute("SELECT value FROM network_backups").fetchall(),
                [("paired network state",)],
            )

    def test_database_alias_and_sidecars_are_preserved_without_following_links(self):
        outside = self.root / "outside.db"
        with closing(sqlite3.connect(outside)) as connection, connection:
            connection.execute("CREATE TABLE private (value TEXT)")
            connection.execute("INSERT INTO private VALUES ('must not be followed')")
        (self.source / "zigbee.db").symlink_to(outside)
        for suffix in ("-wal", "-shm", "-journal"):
            (self.source / ("zigbee.db" + suffix)).symlink_to(str(outside) + suffix)
        before = outside.read_bytes()
        with patch.object(archive, "_snapshot_sqlite") as snapshot:
            result = archive.create_archive(self.source, self.destination)
        snapshot.assert_not_called()
        self.assertEqual(result["sqlite_databases"], [])
        self.assertEqual(len(result["symlinks"]), 4)
        self.assertEqual(outside.read_bytes(), before)
        with tarfile.open(self.destination) as captured:
            for suffix in ("", "-wal", "-shm", "-journal"):
                member = captured.getmember("zigbee.db" + suffix)
                self.assertTrue(member.issym())
                self.assertEqual(member.linkname, str(outside) + suffix)

    def test_sidecar_without_a_real_database_is_not_silently_dropped(self):
        (self.source / "zigbee.db").symlink_to("/config/zigbee.db")
        payload = b"standalone sidecar evidence"
        (self.source / "zigbee.db-wal").write_bytes(payload)
        result = archive.create_archive(self.source, self.destination)
        self.assertEqual(result["sqlite_databases"], [])
        with tarfile.open(self.destination) as captured:
            self.assertTrue(captured.getmember("zigbee.db").issym())
            self.assertEqual(captured.extractfile("zigbee.db-wal").read(), payload)

    def test_generated_image_policy_preserves_source_identity_and_unrelated_files(self):
        config = json.loads((Path(__file__).resolve().parents[1] / 'disaster_recovery/config.json').read_text())
        policy = next(x for x in config['claims'] if x['name'] == 'home-assistant')
        skipped = ['.storage/mail_and_packages.email_cache',
                   'custom_components/mail_and_packages/images/example.jpg',
                   'custom_components/mail_and_packages/images/amazon/tracking.gif',
                   'custom_components/mail_and_packages/images/grid.png',
                   'custom_components/mail_and_packages/images/grid.mp4',
                   'www/mail_and_packages/overlay.png']
        for key in ('front', 'backdoor', 'garage', 'porch', 'ring'):
            skipped += [f'www/{key}_camera_alert{suffix}.jpg' for suffix in ('', '2', '3', '4')]
            skipped += [f'www/.{key}_camera_alert.pending.jpg',
                        f'www/.{key}_camera_alert3.jpgabc_1234',
                        f'www/..{key}_camera_alert.pending.jpgabc_1234']
        kept = ['.storage/auth', '.storage/core.config_entries', '.storage/unknown.cache',
                '.storage/mail_and_packages.email_cache.backup',
                'housekeeping/camera_alert.py', 'www/user-photo.jpg',
                'www/front_camera_alert_backup.jpg', 'www/other_camera_alert.jpg',
                'www/.front_camera_alert.jpg-not-a-temp-file',
                'www/mail_and_packages/custom.yaml', 'www/mail_and_packages/video.mp4',
                'www/mail_and_packages/subdir/photo.jpg',
                'custom_components/mail_and_packages/helpers.py',
                'custom_components/mail_and_packages/images/.PLACEHOLDER',
                'custom_components/mail_and_packages/images/config.json',
                'custom_components/mail_and_packages/mail_none.gif']
        for name in skipped + kept:
            path = self.source / name;path.parent.mkdir(parents=True, exist_ok=True);path.write_text('fixture')
        link = self.source / 'custom_components/mail_and_packages/images/link.jpg'
        link.symlink_to('config.json')
        result = archive.create_archive(self.source, self.destination, excludes=policy['exclude'],
                                        generated_image_caches=policy['generated_image_caches'])
        self.assertEqual(result['generated_image_caches'], policy['generated_image_caches'])
        with tarfile.open(self.destination) as captured:
            names = captured.getnames()
            for name in skipped:self.assertNotIn(name, names)
            for name in kept:self.assertIn(name, names)
            self.assertTrue(captured.getmember(str(link.relative_to(self.source))).issym())

    def test_generated_cache_changes_do_not_restart_capture_but_auth_changes_fail(self):
        camera = self.source / 'www/front_camera_alert.jpg';camera.parent.mkdir();camera.write_text('first')
        storage = self.source / '.storage';storage.mkdir()
        identity = storage / 'auth';identity.write_text('private token')
        cache = storage / 'mail_and_packages.email_cache';cache.write_text('rebuild me')
        config = self.source / 'z.yaml';config.write_text('stable')
        original = archive._copy_regular
        def regenerate(root_fd, relative, target, deadline):
            result = original(root_fd, relative, target, deadline)
            camera.write_text('next frame');cache.write_text('fresh cache')
            return result
        policy = {'excludes': ['.storage/mail_and_packages.email_cache'], 'generated_image_caches': ['ha-camera-alerts']}
        with patch.object(archive, '_copy_regular', side_effect=regenerate):
            result = archive.create_archive(self.source, self.destination, **policy)
        self.assertEqual(result['attempts'], 1)
        self.destination.unlink()
        def change_identity(root_fd, relative, target, deadline):
            result = original(root_fd, relative, target, deadline)
            if relative == 'z.yaml':identity.write_text(identity.read_text() + ' private secret')
            return result
        with patch.object(archive, '_copy_regular', side_effect=change_identity):
            with self.assertRaises(archive.SourceChangedError) as context:
                archive.create_archive(self.source, self.destination, **policy)
        diagnostic = str(context.exception.__cause__)
        self.assertIn('.storage/auth', diagnostic)
        self.assertNotIn('private token', diagnostic);self.assertNotIn('private secret', diagnostic)
        self.assertFalse(self.destination.exists())

    def test_only_matched_generated_temp_may_vanish_during_inventory(self):
        www = self.source / 'www';www.mkdir()
        temporary = www / '.front_camera_alert.jpgabcdefgh';temporary.write_text('temporary')
        ordinary = self.source / 'configuration.yaml';ordinary.write_text('keep')
        original = os.stat
        vanished = set()
        def vanish(path, *args, **kwargs):
            if path == temporary.name and kwargs.get('dir_fd') is not None:
                if path not in vanished:temporary.unlink();vanished.add(path)
                raise FileNotFoundError(path)
            return original(path, *args, **kwargs)
        with patch.object(archive.os, 'stat', side_effect=vanish):
            result = archive.create_archive(self.source, self.destination, generated_image_caches=['ha-camera-alerts'])
        self.assertEqual(result['attempts'], 1);self.destination.unlink()
        def missing_config(path, *args, **kwargs):
            if path == ordinary.name and kwargs.get('dir_fd') is not None:raise FileNotFoundError(path)
            return original(path, *args, **kwargs)
        with patch.object(archive.os, 'stat', side_effect=missing_config):
            with self.assertRaises(archive.SourceChangedError):
                archive.create_archive(self.source, self.destination, generated_image_caches=['ha-camera-alerts'])
        self.assertFalse(self.destination.exists())

    def test_changed_path_diagnostics_are_bounded_and_escape_filenames(self):
        before = {'keep': (1,), 'deleted': (2,), 'changed\nname': (3,)}
        after = {'keep': (1,), 'changed\nname': (4,), **{f'added-{i}': (5,) for i in range(30)}}
        details = json.loads(archive._tree_change_details(before, after))
        self.assertEqual(details['added']['count'], 30);self.assertEqual(len(details['added']['paths']), 10)
        self.assertEqual(details['removed']['paths'], ['deleted'])
        self.assertEqual(details['changed']['paths'], ['changed\nname'])
        with self.assertRaises(ValueError):
            archive.create_archive(self.source, self.destination, generated_image_caches=['all-www'])

    def test_esphome_policy_excludes_only_regenerable_cache_subtree(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "disaster_recovery/config.json").read_text())
        policy = next(item for item in config["claims"] if item["name"] == "esphome")
        fixtures = [".cache/esphome/idf/tools/download", ".esphome/build/device/firmware.bin",
                    "device.yaml", "secrets.yaml", "custom_components/local/component.py", ".cache-not-a-cache.yaml"]
        for relative in fixtures:
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        archive.create_archive(self.source, self.destination, excludes=policy["exclude"])
        with tarfile.open(self.destination) as captured:
            names = captured.getnames()
            self.assertFalse(any(name == ".cache" or name.startswith(".cache/") for name in names))
            self.assertTrue(all(relative in names for relative in fixtures[1:]))

    def _inactive_wal(self):
        database = self.source / "inactive.db"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE pairing (value TEXT)")
            connection.execute("INSERT INTO pairing VALUES ('retained state')")
        self.assertEqual(sorted(path.name for path in self.source.iterdir()), ["inactive.db"])
        self.assertEqual(database.read_bytes()[18:20], bytes([2, 2]))
        return database

    def _exclusive_wal(self):
        database = self.source / "library.db"
        live = sqlite3.connect(database)
        self.addCleanup(live.close)
        self.assertEqual(live.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone(), ("exclusive",))
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("CREATE TABLE saved_queue (value TEXT)")
        live.commit()
        live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        live.execute("INSERT INTO saved_queue VALUES ('latest committed queue')")
        live.commit()
        self.assertGreater(Path(str(database) + "-wal").stat().st_size, 32)
        return database, live

    def test_exclusive_wal_latest_commit_survives_bounded_online_wait(self):
        database, _live = self._exclusive_wal()
        before = {path.name: path.read_bytes() for path in self.source.iterdir()}
        started = time.monotonic()
        with patch.object(archive, "SQLITE_BUSY_WAIT_SECONDS", 0.01):
            result = archive.create_archive(self.source, self.destination, timeout_seconds=20)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result["sqlite_stable_wal_copies"], ["library.db"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.source.iterdir()})
        with tarfile.open(self.destination) as captured:
            self.assertEqual(captured.getnames(), [".", "library.db"])
            restored = self.root / "library-restored.db"
            restored.write_bytes(captured.extractfile("library.db").read())
        with closing(sqlite3.connect(restored)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone(), ("delete",))
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchall(), [("ok",)])
            self.assertEqual(connection.execute("SELECT value FROM saved_queue").fetchall(),
                             [("latest committed queue",)])

    def test_exclusive_wal_concurrent_commit_during_copy_fails_closed(self):
        _database, live = self._exclusive_wal()
        copy = archive._copy_stream

        def concurrent_commit(source, target, deadline):
            copied = copy(source, target, deadline)
            live.execute("INSERT INTO saved_queue VALUES ('concurrent state')")
            live.commit()
            return copied

        with patch.object(archive, "SQLITE_BUSY_WAIT_SECONDS", 0.01), patch.object(
            archive, "_copy_stream", side_effect=concurrent_commit
        ):
            with self.assertRaises(archive.SourceChangedError):
                archive.create_archive(self.source, self.destination, timeout_seconds=20)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_physical_wal_copy_refuses_journal_and_symlinked_wal(self):
        database, _live = self._exclusive_wal()
        root_fd = os.open(self.source, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, root_fd)
        journal = Path(str(database) + "-journal")
        journal.touch()
        with self.assertRaisesRegex(archive.ArchiveError, "rollback journal"):
            archive._snapshot_stable_wal(root_fd, "library.db", self.output / "copy", time.monotonic() + 5)
        journal.unlink()
        wal = Path(str(database) + "-wal")
        saved_wal = self.root / "outside-wal"
        wal.rename(saved_wal)
        wal.symlink_to(saved_wal)
        try:
            with self.assertRaisesRegex(archive.ArchiveError, "regular file"):
                archive._snapshot_stable_wal(root_fd, "library.db", self.output / "copy", time.monotonic() + 5)
        finally:
            wal.unlink()
            saved_wal.rename(wal)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_physical_wal_second_pass_detects_same_size_bytes_with_coarse_timestamps(self):
        database, _live = self._exclusive_wal()
        root_fd = os.open(self.source, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, root_fd)
        copy = archive._copy_stream
        signature = archive._signature

        def coarse_timestamps(value):
            fields = list(signature(value))
            fields[3] = fields[4] = 0
            return tuple(fields)

        for suffix in ("", "-wal"):
            with self.subTest(changed_file=suffix or "main"):
                changed_file = Path(str(database) + suffix)
                original_bytes = changed_file.read_bytes()
                changed = False

                def mutate_bytes(source, target, deadline):
                    nonlocal changed
                    result = copy(source, target, deadline)
                    target_is_wal = str(target.name).endswith("-wal")
                    if not changed and target_is_wal == bool(suffix):
                        changed = True
                        with changed_file.open("r+b") as output:
                            output.seek(-1, os.SEEK_END)
                            output.write(bytes([original_bytes[-1] ^ 1]))
                    return result

                target = self.output / ("main-copy" if not suffix else "wal-copy")
                try:
                    with patch.object(archive, "_signature", side_effect=coarse_timestamps), patch.object(
                        archive, "_copy_stream", side_effect=mutate_bytes
                    ):
                        with self.assertRaisesRegex(archive.SourceChangedError, "bytes changed"):
                            archive._snapshot_stable_wal(root_fd, "library.db", target, time.monotonic() + 5)
                finally:
                    changed_file.write_bytes(original_bytes)
                self.assertFalse(self.destination.exists())

    def test_wal_reset_generation_guard_detects_header_change_with_coarse_timestamps(self):
        database, live = self._exclusive_wal()
        root_fd = os.open(self.source, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, root_fd)
        signature = archive._signature

        def coarse_timestamps(value):
            fields = list(signature(value))
            fields[3] = fields[4] = 0
            return tuple(fields)

        with patch.object(archive, "_signature", side_effect=coarse_timestamps):
            before = archive._stable_wal_signature(root_fd, "library.db")
            # A real checkpoint/reset reuses the WAL for a new committed frame.
            live.execute("PRAGMA wal_checkpoint(RESTART)")
            live.execute("INSERT INTO saved_queue VALUES ('after WAL reset')")
            live.commit()
            after = archive._stable_wal_signature(root_fd, "library.db")
            self.assertNotEqual(before[3], after[3])
            with self.assertRaises(archive.SourceChangedError):
                archive._require_stable_wal(root_fd, "library.db", before)

    def test_online_busy_timer_resets_after_healthy_progress(self):
        now = [0.0]

        class Connection:
            def execute(self, _sql):
                return self

            def fetchall(self):
                return [("ok",)]

            def close(self):
                pass

            def set_progress_handler(self, *_args):
                pass

            def backup(self, _output, pages, progress, sleep):
                for timestamp, status in ((0, sqlite3.SQLITE_BUSY), (4, sqlite3.SQLITE_OK),
                                          (20, sqlite3.SQLITE_BUSY), (24, sqlite3.SQLITE_OK),
                                          (40, sqlite3.SQLITE_DONE)):
                    now[0] = timestamp
                    progress(status, 1, 2)

        with patch.object(archive.sqlite3, "connect", side_effect=[Connection(), Connection()]), patch.object(
            archive.time, "monotonic", side_effect=lambda: now[0]
        ):
            archive._snapshot_sqlite_online(self.source, "large.db", self.output / "copy", 100)

    def test_physical_hash_pass_is_chunked_and_deadline_bounded(self):
        class Input(io.BytesIO):
            def read(self, size):
                self.asserted_size = size
                if size != archive.COPY_CHUNK:
                    raise AssertionError("Unbounded hash read")
                return super().read(size)

        source = Input(b"x" * (archive.COPY_CHUNK * 2))
        with patch.object(archive.time, "monotonic", side_effect=[0.0, 2.0]):
            with self.assertRaises(TimeoutError):
                archive._stream_digest(source, 1.0)
        self.assertEqual(source.tell(), archive.COPY_CHUNK)

    @staticmethod
    def _cantopen(*_args):
        error = sqlite3.OperationalError("unable to open database file")
        error.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
        raise error

    def test_inactive_wal_without_sidecars_on_readonly_source_is_preserved(self):
        database = self._inactive_wal()
        original = database.read_bytes()
        database.chmod(0o444)
        self.source.chmod(0o555)
        self.addCleanup(self.source.chmod, 0o700)
        # This reproduces SQLite's real READONLY_DIRECTORY failure for a closed
        # WAL database. Root can bypass directory modes, so also inject CANTOPEN
        # on that platform to exercise the same production read-only-mount path.
        if os.geteuid() == 0:
            with patch.object(archive, "_snapshot_sqlite_online", side_effect=self._cantopen):
                result = archive.create_archive(self.source, self.destination)
        else:
            result = archive.create_archive(self.source, self.destination)
        self.assertEqual(result["sqlite_quiescent_copies"], ["inactive.db"])
        self.assertEqual(database.read_bytes(), original)
        self.assertEqual(sorted(path.name for path in self.source.iterdir()), ["inactive.db"])
        with tarfile.open(self.destination) as captured:
            restored = self.root / "restored.db"
            restored.write_bytes(captured.extractfile("inactive.db").read())
        with closing(sqlite3.connect(restored)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone(), ("delete",))
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchall(), [("ok",)])
            self.assertEqual(connection.execute("SELECT value FROM pairing").fetchall(), [("retained state",)])

    def test_readonly_error_never_ignores_existing_wal_shm_or_journal(self):
        database = self._inactive_wal()
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(sidecar=suffix):
                sidecar = Path(str(database) + suffix)
                sidecar.touch()  # Even an empty sidecar makes raw copying unsafe.
                with patch.object(archive, "_snapshot_sqlite_online", side_effect=self._cantopen), patch.object(
                    archive, "_snapshot_quiescent_sqlite"
                ) as fallback:
                    with self.assertRaises(sqlite3.OperationalError):
                        archive.create_archive(self.source, self.destination)
                fallback.assert_not_called()
                self.assertEqual(list(self.output.iterdir()), [])
                sidecar.unlink()

    def test_inactive_copy_rejects_main_file_changes_and_transient_sidecars(self):
        database = self._inactive_wal()
        original_copy = archive._copy_stream
        for change in ("main", "transient-journal"):
            with self.subTest(change=change):
                def mutate(source, target, deadline):
                    copied = original_copy(source, target, deadline)
                    if change == "main":
                        current = database.stat()
                        os.utime(database, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000))
                    else:
                        current = database.parent.stat()
                        sidecar = Path(str(database) + "-journal")
                        sidecar.write_bytes(b"transaction started and finished")
                        sidecar.unlink()
                        # Creation and unlink can share one filesystem clock
                        # tick. Make the observed directory change explicit,
                        # as the main-file branch does, without timing sleeps.
                        os.utime(database.parent, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000))
                    return copied

                with patch.object(archive, "_snapshot_sqlite_online", side_effect=self._cantopen), patch.object(
                    archive, "_copy_stream", side_effect=mutate
                ):
                    with self.assertRaises(archive.SourceChangedError):
                        archive.create_archive(self.source, self.destination)
                self.assertEqual(list(self.output.iterdir()), [])

    def test_unrelated_sqlite_failures_never_use_raw_copy(self):
        self._inactive_wal()
        for code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_IOERR, None):
            with self.subTest(code=code):
                error = sqlite3.OperationalError("unrelated SQLite failure")
                if code is not None:
                    error.sqlite_errorcode = code
                with patch.object(archive, "_snapshot_sqlite_online", side_effect=error), patch.object(
                    archive, "_snapshot_quiescent_sqlite"
                ) as fallback:
                    with self.assertRaises(sqlite3.OperationalError):
                        archive.create_archive(self.source, self.destination)
                fallback.assert_not_called()
                self.assertEqual(list(self.output.iterdir()), [])

    def test_inactive_database_must_remain_quiescent_until_component_is_complete(self):
        database = self._inactive_wal()
        (self.source / "z-last.json").write_text("fixture")
        original_copy = archive._copy_regular

        def mutate_after_snapshot(root_fd, relative, target, deadline):
            result = original_copy(root_fd, relative, target, deadline)
            if relative == "z-last.json":
                current = database.stat()
                os.utime(database, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000))
            return result

        with patch.object(archive, "_snapshot_sqlite_online", side_effect=self._cantopen), patch.object(
            archive, "_copy_regular", side_effect=mutate_after_snapshot
        ):
            with self.assertRaises(archive.SourceChangedError):
                archive.create_archive(self.source, self.destination)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_scoped_exclusions_links_modes_and_checksum(self):
        (self.source / "project.yaml").write_text("name: light\n")
        (self.source / "project.yaml").chmod(0o640)
        (self.source / "secrets.yaml").write_text("private fixture\n")
        (self.source / "home-assistant_v2.db").write_text("unwanted history")
        (self.source / "tmp_backups").mkdir()
        (self.source / "tmp_backups" / "large.tar").write_text("staging")
        (self.source / "self").symlink_to("/config")
        (self.source / "project-link").symlink_to("project.yaml")
        result = archive.create_archive(
            self.source, self.destination,
            excludes=["home-assistant_v2.db", "tmp_backups/**"],
        )
        self.assertEqual(result["files"], 2)
        self.assertEqual(len(result["symlinks"]), 2)
        self.assertEqual(result["bytes"], self.destination.stat().st_size)
        self.assertEqual(result["sha256"], hashlib.sha256(self.destination.read_bytes()).hexdigest())
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)
        with tarfile.open(self.destination) as captured:
            self.assertEqual(captured.getnames(), [".", "project-link", "project.yaml", "secrets.yaml", "self"])
            self.assertEqual(captured.getmember("project.yaml").mode, 0o640)
            self.assertEqual(captured.getmember("self").linkname, "/config")
            self.assertTrue(captured.getmember("self").issym())

    def test_memory_size_boundary_and_sqlite_always_uses_nas_scratch(self):
        self.assertEqual(archive.MEMORY_FILE_LIMIT, 8 * 1024 * 1024)
        sizes = {"empty": 0, "limit": archive.MEMORY_FILE_LIMIT,
                 "oversized": archive.MEMORY_FILE_LIMIT + 1}
        for name, size in sizes.items():
            with (self.source / name).open("wb") as output:
                output.truncate(size)
        with closing(sqlite3.connect(self.source / "small.db")) as database, database:
            database.execute("CREATE TABLE pairing (value)")
        original = archive._copy_regular
        observed = {}
        buffers = []

        def capture(root_fd, relative, target, deadline):
            result = original(root_fd, relative, target, deadline)
            _metadata, sqlite, memory = result
            observed[relative] = (sqlite, isinstance(memory, io.BytesIO), target.exists())
            if memory is not None:
                buffers.append(memory)
            return result

        with patch.object(archive, "_copy_regular", side_effect=capture):
            result = archive.create_archive(self.source, self.destination)
        self.assertEqual(observed, {
            "empty": (False, True, False), "limit": (False, True, False),
            "oversized": (False, False, True), "small.db": (True, False, False),
        })
        self.assertTrue(all(buffer.closed for buffer in buffers))
        self.assertEqual(result["sqlite_databases"], ["small.db"])
        with tarfile.open(self.destination) as captured:
            for name, size in sizes.items():
                self.assertEqual(captured.getmember(name).size, size)

    def test_file_growing_past_memory_cap_retries_without_unbounded_buffer(self):
        source = self.source / "growing.json"
        source.write_bytes(b"small")
        copy = archive._copy_stream
        write = archive._MemoryFile.write
        lengths = []
        changed = False

        def grow_once(input_file, output_file, deadline):
            nonlocal changed
            if not changed:
                changed = True
                with source.open("r+b") as changed_file:
                    changed_file.truncate(archive.MEMORY_FILE_LIMIT + 1)
            return copy(input_file, output_file, deadline)

        def bounded_write(buffer, value):
            try:
                return write(buffer, value)
            finally:
                lengths.append(buffer.tell())

        with patch.object(archive, "_copy_stream", side_effect=grow_once), patch.object(
            archive._MemoryFile, "write", bounded_write
        ):
            result = archive.create_archive(self.source, self.destination)
        self.assertTrue(lengths)
        self.assertLessEqual(max(lengths), archive.MEMORY_FILE_LIMIT)
        self.assertEqual(result["attempts"], 2)
        with tarfile.open(self.destination) as captured:
            self.assertEqual(captured.extractfile("growing.json").read(), source.read_bytes())

    def test_memory_copy_deadline_closes_buffer_and_never_creates_nas_staging(self):
        (self.source / "settings.json").write_bytes(b"x" * (2 * archive.COPY_CHUNK))
        constructor = archive._MemoryFile
        buffers = []

        def remember():
            result = constructor()
            buffers.append(result)
            return result

        root_fd = os.open(self.source, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with patch.object(archive, "_MemoryFile", side_effect=remember), patch.object(
                archive.time, "monotonic", side_effect=[0.0, 0.0, 2.0]
            ):
                with self.assertRaises(TimeoutError):
                    archive._copy_regular(root_fd, "settings.json", self.output / "staged", 1.0)
        finally:
            os.close(root_fd)
        self.assertEqual(len(buffers), 1)
        self.assertTrue(buffers[0].closed)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_file_mutating_once_is_retried_with_complete_new_content(self):
        source = self.source / "state.json"
        source.write_bytes(b"before")
        original = archive._copy_stream
        calls = 0

        def mutate_once(input_file, output_file, deadline):
            nonlocal calls
            calls += 1
            result = original(input_file, output_file, deadline)
            if calls == 1:
                source.write_bytes(b"after with complete longer state")
            return result

        with patch.object(archive, "_copy_stream", side_effect=mutate_once):
            result = archive.create_archive(self.source, self.destination)
        # The initial attempt re-reads the changed file, then rejects the changed
        # tree. A second component attempt captures a wholly stable inventory.
        self.assertEqual(calls, 3)
        self.assertEqual(result["attempts"], 2)
        with tarfile.open(self.destination) as captured:
            self.assertEqual(captured.extractfile("state.json").read(), source.read_bytes())

    def test_continuously_mutating_file_leaves_no_published_archive(self):
        source = self.source / "state.json"
        source.write_bytes(b"initial")
        original = archive._copy_stream

        def keep_mutating(input_file, output_file, deadline):
            result = original(input_file, output_file, deadline)
            with source.open("ab") as changed:
                changed.write(b" changed")
            return result

        with patch.object(archive, "_copy_stream", side_effect=keep_mutating):
            with self.assertRaises(archive.SourceChangedError):
                archive.create_archive(self.source, self.destination)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_corrupt_sqlite_or_timeout_cannot_publish_success(self):
        (self.source / "broken.db").write_bytes(archive.SQLITE_HEADER + b"invalid database")
        with self.assertRaises(sqlite3.DatabaseError):
            archive.create_archive(self.source, self.destination)
        self.assertEqual(list(self.output.iterdir()), [])
        (self.source / "broken.db").unlink()
        (self.source / "settings.yaml").write_text("valid: true")
        with patch.object(archive.time, "monotonic", side_effect=[1.0, 200.0]):
            with self.assertRaises(TimeoutError):
                archive.create_archive(self.source, self.destination, timeout_seconds=1)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_unsafe_exclusion_destination_or_existing_archive_rejected(self):
        for exclusion in ("../secrets", "/config", "*.db", "a//b", "a/./b", ""):
            with self.subTest(exclusion=exclusion):
                with self.assertRaises(ValueError):
                    archive.create_archive(self.source, self.destination, excludes=[exclusion])
        with self.assertRaises(ValueError):
            archive.create_archive(self.source, self.source / "recursive.tar.gz")
        self.destination.write_bytes(b"previous complete backup")
        with self.assertRaises(FileExistsError):
            archive.create_archive(self.source, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"previous complete backup")

    def test_sqlite_backup_progress_enforces_deadline(self):
        database = self.source / "pairing.db"
        with closing(sqlite3.connect(database)) as source, source:
            source.execute("CREATE TABLE pairing (data BLOB)")
            source.executemany(
                "INSERT INTO pairing VALUES (zeroblob(1024))", [()] * 4000
            )
        # A real multi-page SQLite backup calls the progress callback repeatedly;
        # advance its clock deterministically, without sleeping or touching live state.
        with patch.object(archive.time, "monotonic", side_effect=itertools.count(0.0, 0.1)):
            with self.assertRaises(TimeoutError):
                archive.create_archive(self.source, self.destination, timeout_seconds=1.0)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_special_files_and_unbounded_timeout_are_rejected(self):
        os.mkfifo(self.source / "runtime.pipe")
        with self.assertRaises(archive.ArchiveError):
            archive.create_archive(self.source, self.destination)
        self.assertEqual(list(self.output.iterdir()), [])
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    archive.create_archive(self.source, self.destination, timeout_seconds=timeout)

    def test_added_file_and_previously_copied_file_change_restart_component(self):
        (self.source / "a.json").write_text("old")
        (self.source / "z.json").write_text("last")
        original = archive._copy_regular
        changed = False

        def update_tree(root_fd, relative, target, deadline):
            nonlocal changed
            result = original(root_fd, relative, target, deadline)
            if relative == "z.json" and not changed:
                changed = True
                (self.source / "a.json").write_text("new version")
                (self.source / "added.json").write_text("new member")
            return result

        with patch.object(archive, "_copy_regular", side_effect=update_tree):
            result = archive.create_archive(self.source, self.destination)
        self.assertEqual(result["attempts"], 2)
        with tarfile.open(self.destination) as captured:
            self.assertEqual(captured.extractfile("a.json").read(), b"new version")
            self.assertEqual(captured.extractfile("added.json").read(), b"new member")

    def test_vanishing_sqlite_sidecar_during_inventory_is_expected(self):
        database = self.source / "state.db"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE state (value)")
        # A WAL can disappear between scandir and stat during a checkpoint.
        # The database identifies this as a sidecar before stat is attempted.
        sidecar = self.source / "state.db-wal"
        sidecar.write_bytes(b"checkpoint fixture")
        original = archive._is_sqlite_sidecar

        def checkpoint(directory_fd, name):
            if name == "state.db-wal":
                sidecar.unlink(missing_ok=True)
            return original(directory_fd, name)

        with patch.object(archive, "_is_sqlite_sidecar", side_effect=checkpoint):
            result = archive.create_archive(self.source, self.destination)
        self.assertEqual(result["attempts"], 1)
        with tarfile.open(self.destination) as captured:
            self.assertEqual(captured.getnames(), [".", "state.db"])

    def test_source_root_ownership_and_mode_are_retained_in_restore_report(self):
        (self.source / "settings.yaml").write_text("enabled: true\n")
        (self.source / "empty-directory").mkdir()
        self.source.chmod(0o750)
        os.utime(self.source, (1234567890, 1234567890))
        original = self.source.stat()
        result = archive.create_archive(self.source, self.destination)
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["directories"], 2)  # Source root plus its child.
        (self.output / "manifest.json").write_text(json.dumps({
            "format_version": 1,
            "completed": True,
            "components": [{"name": "component", "source": {}, **result}],
        }))

        destination = self.root / "restored"
        report = verify_snapshot(self.output, destination)
        root = next(member for member in report["components"][0]["members"] if member["path"] == ".")
        self.assertEqual(root["type"], "directory")
        self.assertEqual(root["original_uid"], original.st_uid)
        self.assertEqual(root["original_gid"], original.st_gid)
        self.assertEqual(root["original_mode"], "0750")
        self.assertEqual(root["original_mtime"], original.st_mtime)
        # Inspection keeps the extracted workspace private; operators use the
        # report's original ownership/mode only for the later production restore.
        self.assertEqual((destination / "component").stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
