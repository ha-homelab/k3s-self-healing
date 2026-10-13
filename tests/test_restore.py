"""Offline snapshot verification tests; no host configuration is applied."""
from contextlib import redirect_stderr, redirect_stdout
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from disaster_recovery import restore
from disaster_recovery.restore import RestoreError, main, verify_snapshot


def regular(name, contents=b"payload", mode=0o640):
    member = tarfile.TarInfo(name)
    member.size = len(contents)
    member.mode = mode
    member.mtime = 1234567890
    member.uid = 123
    member.gid = 456
    return member, contents


def directory(name):
    member = tarfile.TarInfo(name)
    member.type = tarfile.DIRTYPE
    member.mode = 0o755
    return member, None


def link(name, target, hard=False):
    member = tarfile.TarInfo(name)
    member.type = tarfile.LNKTYPE if hard else tarfile.SYMTYPE
    member.linkname = target
    return member, None


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.snapshot = self.base / "snapshot"
        self.snapshot.mkdir()
        self.destination = self.base / "recovery"
        self.manifest = {
            "format_version": 1,
            "completed": True,
            "created_at": "2026-09-15T00:00:00Z",
            "components": [],
            "metadata": {},
        }

    def tearDown(self):
        self.temporary.cleanup()

    def add_component(self, members, name="home_assistant", sqlite_databases=None):
        archive = self.snapshot / (name + ".tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            for member, contents in members:
                tar.addfile(member, io.BytesIO(contents) if contents is not None else None)
        component = {
            "name": name,
            "archive": archive.name,
            "bytes": archive.stat().st_size,
            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "source": "test fixture",
        }
        if sqlite_databases is not None:
            component["sqlite_databases"] = sqlite_databases
        self.manifest["components"].append(component)
        self.write_manifest()
        return archive

    def write_manifest(self):
        (self.snapshot / "manifest.json").write_text(json.dumps(self.manifest))

    def refresh_digest(self, index=0):
        component = self.manifest["components"][index]
        data = (self.snapshot / component["archive"]).read_bytes()
        component["bytes"] = len(data)
        component["sha256"] = hashlib.sha256(data).hexdigest()
        self.write_manifest()

    def sqlite_bytes(self):
        path = self.base / "source.sqlite"
        connection = sqlite3.connect(str(path))
        connection.execute("CREATE TABLE sample (value TEXT)")
        connection.execute("INSERT INTO sample VALUES ('kept offline')")
        connection.commit()
        connection.close()
        return path.read_bytes()

    def test_verification_does_not_create_extraction_directory(self):
        self.add_component([regular("configuration.yaml", b"script: {}\n")])
        report = verify_snapshot(self.snapshot)
        self.assertEqual(report["status"], "verified")
        self.assertFalse(report["host_apply_performed"])
        self.assertFalse(self.destination.exists())
        self.assertEqual(sorted(p.name for p in self.snapshot.iterdir()), ["home_assistant.tar.gz", "manifest.json"])

    def test_extraction_permissions_and_original_metadata(self):
        self.add_component([directory("nested"), regular("nested/executable.sh", b"never executed\n", mode=0o4755)])
        report = verify_snapshot(self.snapshot, self.destination)
        payload = self.destination / "home_assistant/nested/executable.sh"
        self.assertEqual(payload.read_bytes(), b"never executed\n")
        self.assertEqual(stat.S_IMODE(payload.stat().st_mode), 0o600)
        for path in [self.destination, payload.parent, payload.parent.parent]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.destination / "report.json").stat().st_mode), 0o600)
        record = report["components"][0]["members"][1]
        self.assertEqual(record["original_mode"], "4755")
        self.assertEqual(record["original_uid"], 123)
        self.assertEqual(record["original_gid"], 456)
        self.assertTrue(json.loads((self.destination / "report.json").read_text())["completed"])

    def test_multiple_components_have_separate_roots(self):
        self.add_component([regular("same", b"one")], name="config")
        self.add_component([regular("same", b"two")], name="share")
        verify_snapshot(self.snapshot, self.destination)
        self.assertEqual((self.destination / "config/same").read_bytes(), b"one")
        self.assertEqual((self.destination / "share/same").read_bytes(), b"two")

    def test_hash_and_size_failures_are_rejected_before_extraction(self):
        self.add_component([regular("file")])
        original = dict(self.manifest["components"][0])
        for field, value in [("sha256", "0" * 64), ("bytes", original["bytes"] + 1)]:
            with self.subTest(field=field):
                self.manifest["components"][0] = {**original, field: value}
                self.write_manifest()
                with self.assertRaises(RestoreError):
                    verify_snapshot(self.snapshot, self.destination)
                self.assertFalse(self.destination.exists())

    def test_verify_only_detects_archive_change_during_scan(self):
        archive = self.add_component([regular("file")])
        validate_layout = restore._validate_layout

        def mutate_after_scan(records):
            validate_layout(records)
            changed = bytearray(archive.read_bytes())
            changed[-1] ^= 0xFF
            archive.write_bytes(changed)

        with patch.object(restore, "_validate_layout", side_effect=mutate_after_scan):
            with self.assertRaisesRegex(RestoreError, "SHA-256"):
                verify_snapshot(self.snapshot)
        self.assertFalse(self.destination.exists())

    def test_corrupt_gzip_crc_is_rejected_even_with_matching_archive_hash(self):
        archive = self.add_component([regular("file")])
        data = bytearray(archive.read_bytes())
        data[-8] ^= 0xFF
        archive.write_bytes(data)
        self.refresh_digest()
        with self.assertRaisesRegex(RestoreError, "corrupt"):
            verify_snapshot(self.snapshot)

    def test_truncated_gzip_trailer_is_rejected_even_with_matching_hash(self):
        archive = self.add_component([regular("file")])
        archive.write_bytes(archive.read_bytes()[:-4])
        self.refresh_digest()
        with self.assertRaisesRegex(RestoreError, "corrupt"):
            verify_snapshot(self.snapshot)

    def test_invalid_deflate_stream_produces_summary_error(self):
        archive = self.add_component([regular("file")])
        archive.write_bytes(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff\x07" + b"\x00" * 8)
        self.refresh_digest()
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            result = main(["verify", str(self.snapshot)])
        self.assertEqual(result, 1)
        self.assertIn("corrupt", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())

    def test_gzip_tail_is_read_after_tar_end_markers(self):
        archive = self.add_component([regular("file")])
        # A second gzip member after tar's end markers must also pass its CRC.
        extra = bytearray(gzip.compress(b"trailing bytes"))
        extra[-8] ^= 0xFF
        archive.write_bytes(archive.read_bytes() + extra)
        self.refresh_digest()
        with self.assertRaisesRegex(RestoreError, "corrupt"):
            verify_snapshot(self.snapshot)

    def test_manifest_rejects_unsafe_archive_and_component_names(self):
        self.add_component([regular("file")])
        original = dict(self.manifest["components"][0])
        cases = [("archive", "../outside.tar.gz"), ("archive", "/outside.tar.gz"),
                 ("archive", "nested/archive.tar.gz"), ("archive", "C:\\archive.tar.gz"),
                 ("name", "../outside"), ("name", "report.json")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.manifest["components"][0] = {**original, field: value}
                self.write_manifest()
                with self.assertRaises(RestoreError):
                    verify_snapshot(self.snapshot)

    def test_manifest_requires_completed_v1_and_unique_names(self):
        self.add_component([regular("file")])
        valid = json.loads(json.dumps(self.manifest))
        cases = [dict(valid, completed=False), dict(valid, completed=1),
                 dict(valid, format_version=2), dict(valid, format_version=True),
                 dict(valid, components=[]),
                 dict(valid, components=[valid["components"][0], valid["components"][0]])]
        for manifest in cases:
            with self.subTest(manifest=manifest):
                self.manifest = manifest
                self.write_manifest()
                with self.assertRaises(RestoreError):
                    verify_snapshot(self.snapshot)

    def test_duplicate_manifest_json_keys_are_rejected(self):
        (self.snapshot / "manifest.json").write_text('{"format_version":1,"completed":false,"completed":true,"components":[]}')
        with self.assertRaisesRegex(RestoreError, "duplicate JSON"):
            verify_snapshot(self.snapshot)

    def test_snapshot_archive_and_manifest_symlinks_are_rejected(self):
        archive = self.add_component([regular("file")])
        outside = self.base / "outside.tar.gz"
        archive.rename(outside)
        archive.symlink_to(outside)
        with self.assertRaises(RestoreError):
            verify_snapshot(self.snapshot)
        archive.unlink()
        outside.rename(archive)
        manifest = self.snapshot / "manifest.json"
        outside_manifest = self.base / "outside.json"
        manifest.rename(outside_manifest)
        manifest.symlink_to(outside_manifest)
        with self.assertRaises(RestoreError):
            verify_snapshot(self.snapshot)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO fixture requires a POSIX filesystem")
    def test_snapshot_archive_fifo_is_rejected_without_blocking(self):
        archive = self.add_component([regular("file")])
        archive.unlink()
        os.mkfifo(archive)
        with self.assertRaisesRegex(RestoreError, "regular files"):
            verify_snapshot(self.snapshot)

    def test_absolute_parent_and_windows_member_paths_are_rejected(self):
        for member_name in ["../outside", "/absolute", "safe/../../outside", "C:\\outside", "safe\\..\\outside"]:
            with self.subTest(path=member_name):
                self.manifest["components"] = []
                self.add_component([regular(member_name)])
                with self.assertRaisesRegex(RestoreError, "unsafe member path"):
                    verify_snapshot(self.snapshot, self.destination)
                self.assertFalse(self.destination.exists())

    def test_devices_and_fifos_are_rejected(self):
        for member_type in [tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE]:
            with self.subTest(member_type=member_type):
                member = tarfile.TarInfo("device")
                member.type = member_type
                self.manifest["components"] = []
                self.add_component([(member, None)])
                with self.assertRaisesRegex(RestoreError, "unsupported member type"):
                    verify_snapshot(self.snapshot)

    def test_duplicate_and_case_colliding_members_are_rejected(self):
        for first, second in [("same", "same"), ("same", "./same"), ("Name", "name")]:
            with self.subTest(first=first, second=second):
                self.manifest["components"] = []
                self.add_component([regular(first), regular(second)])
                with self.assertRaisesRegex(RestoreError, "duplicate or case-colliding"):
                    verify_snapshot(self.snapshot)

    def test_no_member_can_be_written_below_any_link_in_either_order(self):
        for target in ["real", "/outside", "../outside"]:
            for link_first in [True, False]:
                with self.subTest(target=target, link_first=link_first):
                    members = [link("alias", target), regular("alias/file", b"secret")]
                    if not link_first:
                        members.reverse()
                    self.manifest["components"] = []
                    self.add_component(members)
                    with self.assertRaisesRegex(RestoreError, "nested below"):
                        verify_snapshot(self.snapshot, self.destination)
                    self.assertFalse(self.destination.exists())

    def test_file_cannot_be_a_parent_directory(self):
        self.add_component([regular("not_a_directory"), regular("not_a_directory/file")])
        with self.assertRaisesRegex(RestoreError, "nested below"):
            verify_snapshot(self.snapshot)

    def test_safe_relative_symlinks_preserved_unsafe_aliases_reported(self):
        self.add_component([link("safe", "nested/file"), regular("nested/file", b"safe contents"),
                            link("config", "/config"), link("nested/escape", "../../outside"),
                            link("indirect", "config/secrets.yaml"), link("dangling", "missing")])
        report = verify_snapshot(self.snapshot, self.destination)
        root = self.destination / "home_assistant"
        self.assertEqual(os.readlink(root / "safe"), "nested/file")
        self.assertEqual((root / "safe").read_bytes(), b"safe contents")
        self.assertTrue((root / "dangling").is_symlink())
        for name in ["config", "nested/escape", "indirect"]:
            self.assertFalse((root / name).is_symlink())
        skipped = {item["path"]: item for item in report["components"][0]["skipped_links"]}
        self.assertEqual(skipped["config"]["link_target"], "/config")
        self.assertEqual(skipped["config"]["skip_reason"], "absolute_target")
        self.assertEqual(skipped["nested/escape"]["skip_reason"], "outside_component")
        self.assertEqual(skipped["indirect"]["skip_reason"], "target_traverses_link")

    def test_safe_relative_parent_link_within_component_is_preserved(self):
        self.add_component([regular("target", b"inside"), link("nested/link", "../target")])
        verify_snapshot(self.snapshot, self.destination)
        self.assertEqual((self.destination / "home_assistant/nested/link").read_bytes(), b"inside")

    def test_hardlink_to_regular_file_preserved_and_external_hardlink_skipped(self):
        self.add_component([link("hard", "target", hard=True), regular("target", b"same inode"),
                            link("outside", "../outside", hard=True), link("missing", "absent", hard=True)])
        report = verify_snapshot(self.snapshot, self.destination)
        root = self.destination / "home_assistant"
        self.assertEqual((root / "hard").stat().st_ino, (root / "target").stat().st_ino)
        self.assertEqual(len(report["components"][0]["skipped_links"]), 2)

    def test_existing_destination_is_rejected_even_when_empty(self):
        self.add_component([regular("file")])
        self.destination.mkdir()
        with self.assertRaisesRegex(RestoreError, "must not already exist"):
            verify_snapshot(self.snapshot, self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_destination_within_snapshot_or_symlink_alias_is_rejected(self):
        self.add_component([regular("file")])
        alias = self.base / "snapshot-alias"
        alias.symlink_to(self.snapshot, target_is_directory=True)
        for destination in [self.snapshot / "recovery", alias / "recovery"]:
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(RestoreError, "outside the snapshot"):
                    verify_snapshot(self.snapshot, destination)

    def test_sqlite_header_discovery_and_quick_check(self):
        self.add_component([regular("unusual_database_name", self.sqlite_bytes())])
        report = verify_snapshot(self.snapshot, self.destination)
        component = report["components"][0]
        self.assertEqual(component["sqlite_databases"], ["unusual_database_name"])
        self.assertEqual(component["sqlite_quick_check"], "ok")
        self.assertFalse(any(path.name.endswith(("-wal", "-shm", "-journal")) for path in self.destination.rglob("*")))

    def test_corrupt_declared_sqlite_leaves_failed_private_report(self):
        self.add_component([regular("database.sqlite", b"not sqlite data")], sqlite_databases=["database.sqlite"])
        with self.assertRaisesRegex(RestoreError, "quick_check"):
            verify_snapshot(self.snapshot, self.destination)
        report = json.loads((self.destination / "report.json").read_text())
        self.assertFalse(report["completed"])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(stat.S_IMODE((self.destination / "report.json").stat().st_mode), 0o600)

    def test_sqlite_page_corruption_is_detected_after_header_discovery(self):
        database = bytearray(self.sqlite_bytes())
        database[100] = 0  # Invalid first B-tree page type; SQLite header remains intact.
        self.add_component([regular("database", database)])
        with self.assertRaisesRegex(RestoreError, "quick_check"):
            verify_snapshot(self.snapshot, self.destination)
        self.assertFalse(json.loads((self.destination / "report.json").read_text())["completed"])

    def test_sqlite_live_sidecars_are_rejected(self):
        database = self.sqlite_bytes()
        for suffix in ["-wal", "-shm", "-journal"]:
            with self.subTest(suffix=suffix):
                self.manifest["components"] = []
                self.add_component([regular("database", database), regular("database" + suffix)])
                with self.assertRaisesRegex(RestoreError, "standalone database"):
                    verify_snapshot(self.snapshot)

    def test_declared_sqlite_inventory_must_point_to_regular_members(self):
        self.add_component([regular("file")], sqlite_databases=["missing.sqlite"])
        with self.assertRaisesRegex(RestoreError, "SQLite inventory"):
            verify_snapshot(self.snapshot)

    def test_cli_output_is_summary_only(self):
        sensitive = b"synthetic secret fixture"
        self.add_component([regular("private.txt", sensitive)])
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            result = main(["verify", str(self.snapshot), "--extract", str(self.destination)])
        self.assertEqual(result, 0)
        self.assertIn("1 components", output.getvalue())
        self.assertNotIn(sensitive.decode(), output.getvalue() + error.getvalue())
        self.assertNotIn("private.txt", output.getvalue() + error.getvalue())

    def test_cli_failure_does_not_print_untrusted_member_names(self):
        self.add_component([regular("../synthetic_sensitive_name")])
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            result = main(["verify", str(self.snapshot)])
        self.assertEqual(result, 1)
        self.assertNotIn("synthetic_sensitive_name", output.getvalue() + error.getvalue())


if __name__ == "__main__":
    unittest.main()
