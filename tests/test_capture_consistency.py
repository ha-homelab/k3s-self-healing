"""Capture-time consistency and deterministic staged scheduling, offline only."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from disaster_recovery import archive


class CaptureConsistencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "output"
        self.output.mkdir()
        self.destination = self.output / "component.tar.gz"

    def attempt(self, staged=True):
        return archive._create_archive_attempt(
            self.source, self.destination, (), time.monotonic() + 30,
            staged_capture=staged)

    def database(self, path):
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE state (value TEXT)")
            connection.execute("INSERT INTO state VALUES ('complete')")

    def test_changes_before_copy_use_complete_captured_bytes_and_metadata(self):
        source = self.source / "state.json"
        original = archive._copy_regular
        for staged in (False, True):
            for atomic in (False, True):
                with self.subTest(staged=staged, atomic=atomic):
                    source.write_bytes(b"old")
                    changed = False

                    def replace_before_copy(*args, **kwargs):
                        nonlocal changed
                        if not changed:
                            changed = True
                            target = self.root / "replacement" if atomic else source
                            target.write_bytes(b"new complete state")
                            target.chmod(0o640)
                            os.utime(target, ns=(10_000_000_000, 10_000_000_000))
                            if atomic:
                                os.replace(target, source)
                        return original(*args, **kwargs)

                    with patch.object(archive, "_copy_regular", side_effect=replace_before_copy):
                        result = archive.create_archive(self.source, self.destination,
                                                        staged_capture=staged)
                    self.assertEqual(result["attempts"], 1)
                    with tarfile.open(self.destination) as captured:
                        self.assertEqual(captured.extractfile("state.json").read(), b"new complete state")
                        self.assertEqual(captured.getmember("state.json").mode, 0o640)
                        self.assertEqual(captured.getmember("state.json").mtime, 10)
                    self.destination.unlink()

    def test_changes_after_copy_reject_even_with_backdated_mtime(self):
        source = self.source / "state.json"
        original = archive._copy_regular
        for staged in (False, True):
            for atomic in (False, True):
                with self.subTest(staged=staged, atomic=atomic):
                    source.write_bytes(b"state")
                    calls = 0

                    def replace_after_copy(*args, **kwargs):
                        nonlocal calls
                        result = original(*args, **kwargs)
                        calls += 1
                        target = self.root / "replacement" if atomic else source
                        if not atomic:
                            # Some filesystems update ctime on a coarse clock tick.
                            time.sleep(0.02)
                        target.write_bytes(b"other")
                        os.utime(target, ns=(result[0].st_atime_ns, result[0].st_mtime_ns))
                        if atomic:
                            os.replace(target, source)
                        current = source.stat()
                        self.assertEqual(current.st_mtime_ns, result[0].st_mtime_ns)
                        self.assertNotEqual(archive._signature(current), archive._signature(result[0]))
                        return result

                    with patch.object(archive, "_copy_regular", side_effect=replace_after_copy):
                        with self.assertRaises(archive.SourceChangedError):
                            archive.create_archive(self.source, self.destination, staged_capture=staged)
                    self.assertEqual(calls, 3)
                    self.assertEqual(list(self.output.iterdir()), [])

    def test_directory_identity_is_not_rebased_with_its_files(self):
        parent = self.source / "directory"
        parent.mkdir()
        (parent / "state").write_bytes(b"old")
        original = archive._copy_regular

        def replace_parent(*args, **kwargs):
            parent.rename(self.root / "previous-directory")
            parent.mkdir()
            (parent / "state").write_bytes(b"new")
            return original(*args, **kwargs)

        with patch.object(archive, "_copy_regular", side_effect=replace_parent):
            with self.assertRaises(archive.SourceChangedError):
                self.attempt()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_regular_file_replaced_by_symlink_is_never_followed(self):
        source = self.source / "state"
        source.write_bytes(b"old")
        outside = self.root / "outside"
        outside.write_bytes(b"must not be archived")
        original = archive._copy_regular

        def replace_before_copy(*args, **kwargs):
            source.unlink()
            source.symlink_to(outside)
            return original(*args, **kwargs)

        with patch.object(archive, "_copy_regular", side_effect=replace_before_copy):
            with self.assertRaises(OSError):
                self.attempt()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_member_and_symlink_changes_retain_initial_tree_guards(self):
        original = archive._copy_regular
        for change in ("add", "remove", "symlink"):
            with self.subTest(change=change):
                for path in self.source.iterdir():
                    path.unlink()
                (self.source / "state").write_bytes(b"state")
                (self.source / "victim").write_bytes(b"other")
                (self.source / "alias").symlink_to("state")
                changed = False

                def change_tree(root_fd, relative, target, deadline, **options):
                    nonlocal changed
                    result = original(root_fd, relative, target, deadline, **options)
                    if relative == "state" and not changed:
                        changed = True
                        if change == "add":
                            (self.source / "added").write_bytes(b"new")
                        elif change == "remove":
                            (self.source / "victim").unlink()
                        else:
                            (self.source / "alias").unlink()
                            (self.source / "alias").symlink_to("victim")
                    return result

                with patch.object(archive, "_copy_regular", side_effect=change_tree):
                    with self.assertRaises(archive.SourceChangedError):
                        self.attempt()
                self.assertEqual(list(self.output.iterdir()), [])

    def test_newly_captured_sqlite_keeps_initial_identity_guard(self):
        source = self.source / "state"
        original = archive._copy_regular
        for initially_sqlite in (False, True):
            with self.subTest(initially_sqlite=initially_sqlite):
                source.unlink(missing_ok=True)
                if initially_sqlite:
                    self.database(source)
                else:
                    source.write_bytes(b"ordinary")
                replacement = self.root / "replacement.db"
                self.database(replacement)

                def replace_before_copy(*args, **kwargs):
                    os.replace(replacement, source)
                    return original(*args, **kwargs)

                with patch.object(archive, "_copy_regular", side_effect=replace_before_copy):
                    with self.assertRaises(archive.SourceChangedError):
                        self.attempt()
                self.assertEqual(list(self.output.iterdir()), [])

    def test_former_database_replaced_before_copy_is_classified_as_ordinary(self):
        source = self.source / "state"
        self.database(source)
        original = archive._copy_regular

        def replace_before_copy(*args, **kwargs):
            replacement = self.root / "replacement"
            replacement.write_bytes(b"ordinary replacement")
            os.replace(replacement, source)
            return original(*args, **kwargs)

        with patch.object(archive, "_copy_regular", side_effect=replace_before_copy):
            result = self.attempt()
        self.assertEqual(result["sqlite_databases"], [])
        with tarfile.open(self.destination) as captured:
            self.assertEqual(captured.extractfile("state").read(), b"ordinary replacement")

    def test_former_database_sidecars_cannot_disappear_from_member_checks(self):
        source = self.source / "state"
        self.database(source)
        (self.source / "state-wal").write_bytes(b"sidecar")
        original = archive._copy_regular

        def replace_before_copy(*args, **kwargs):
            replacement = self.root / "replacement"
            replacement.write_bytes(b"ordinary replacement")
            os.replace(replacement, source)
            return original(*args, **kwargs)

        with patch.object(archive, "_copy_regular", side_effect=replace_before_copy):
            with self.assertRaises(archive.SourceChangedError):
                self.attempt()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_staged_families_use_newest_member_time_and_base_first(self):
        (self.source / "directory").mkdir()
        stamps = {"a": 1, "a-wal": 50, "a-wal-journal": 2,
                  "b": 10, "b-shm": 11, "orphan-wal": 20,
                  "c": 20, "directory/value": 5}
        for name, stamp in stamps.items():
            path = self.source / name
            path.write_bytes(name.encode() * (100 if name.endswith("-wal") else 1))
            os.utime(path, ns=(stamp * 10**9, stamp * 10**9))
        # These are ordinary files: family grouping must not require a SQLite header.
        with patch.object(archive, "_snapshot_sqlite") as snapshot:
            self.attempt()
        snapshot.assert_not_called()
        with tarfile.open(self.destination) as captured:
            self.assertEqual(captured.getnames(), [".", "directory", "directory/value",
                             "b", "b-shm", "c", "orphan-wal", "a", "a-wal", "a-wal-journal"])
            self.assertEqual(len(captured.getnames()), len(set(captured.getnames())))
        self.destination.unlink()
        self.attempt(staged=False)
        with tarfile.open(self.destination) as captured:
            self.assertEqual(captured.getnames(), [".", *sorted(["directory", *stamps])])


if __name__ == "__main__":
    unittest.main()
