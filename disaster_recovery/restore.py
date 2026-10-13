"""Verify disaster snapshots and extract a private offline recovery workspace.

This module never applies a configuration to a host. Archive metadata is retained
in the private extraction report; extracted files are non-executable (0600), and
directories are private (0700). Links are considered only after regular files and
SQLite checks are complete.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
from typing import Any, BinaryIO
import zlib

CHUNK_SIZE = 1024 * 1024
SQLITE_HEADER = b"SQLite format 3\x00"
BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


class RestoreError(ValueError):
    """A snapshot cannot be accepted; messages never contain file contents."""


@dataclass
class Archive:
    name: str
    filename: str
    handle: BinaryIO
    expected_size: int
    expected_sha256: str
    declared_sqlite: set[str]
    members: list[dict[str, Any]]
    sqlite_files: set[str]


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise RestoreError("Manifest contains duplicate JSON keys")
        result[key] = value
    return result


def _basename(value: Any, *, component: bool = False) -> str:
    if not isinstance(value, str) or not BASENAME.fullmatch(value):
        raise RestoreError("Manifest names must be safe, single-component basenames")
    if component and value.casefold() == "report.json":
        raise RestoreError("Component name conflicts with the extraction report")
    return value


def _relative(value: Any, *, allow_root: bool = False) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RestoreError("Archive contains an unsafe member path")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise RestoreError("Archive contains an unsafe member path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", value):
        raise RestoreError("Archive contains an unsafe member path")
    result = str(path)
    if result == "." and not allow_root:
        raise RestoreError("Archive contains an invalid root member")
    return result


def _open_regular(path: Path) -> BinaryIO:
    """Refuse archive/manifest symlinks, including a replacement at open time."""
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
    except OSError as exc:
        raise RestoreError("Snapshot file is missing or cannot be opened safely") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode) or path.is_symlink():
        os.close(descriptor)
        raise RestoreError("Snapshot files must be regular files, not links")
    return os.fdopen(descriptor, "rb")


def _check_digest(archive: Archive) -> None:
    archive.handle.seek(0)
    digest = hashlib.sha256()
    size = 0
    while chunk := archive.handle.read(CHUNK_SIZE):
        size += len(chunk)
        digest.update(chunk)
    if size != archive.expected_size:
        raise RestoreError("Archive byte size does not match the manifest")
    if digest.hexdigest() != archive.expected_sha256:
        raise RestoreError("Archive SHA-256 does not match the manifest")
    archive.handle.seek(0)


def _link_target(path: str, target: str, *, hardlink: bool) -> tuple[str | None, str | None]:
    """Resolve a link within its component without touching the filesystem."""
    if not target or "\\" in target or any(ord(c) < 32 or ord(c) == 127 for c in target):
        return None, "invalid_target"
    link = PurePosixPath(target)
    if link.is_absolute() or re.match(r"^[A-Za-z]:", target):
        return None, "absolute_target"
    parts = [] if hardlink else list(PurePosixPath(path).parent.parts)
    for part in link.parts:
        if part == "..":
            if not parts:
                return None, "outside_component"
            parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts) or ".", None


def _describe(member: tarfile.TarInfo) -> dict[str, Any]:
    if member.isdir():
        kind = "directory"
    elif member.isreg():
        kind = "file"
    elif member.issym():
        kind = "symlink"
    elif member.islnk():
        kind = "hardlink"
    else:
        raise RestoreError("Archive contains a device, FIFO, or unsupported member type")
    path = _relative(member.name, allow_root=kind == "directory")
    if member.size < 0:
        raise RestoreError("Archive contains an invalid member size")
    record = {
        "path": path,
        "type": kind,
        "size": member.size,
        "original_mode": format(member.mode, "04o"),
        "original_uid": member.uid,
        "original_gid": member.gid,
        "original_mtime": member.mtime,
    }
    if kind in {"symlink", "hardlink"}:
        target, reason = _link_target(path, member.linkname, hardlink=kind == "hardlink")
        record.update(link_target=member.linkname, resolved_target=target)
        if reason:
            record["skip_reason"] = reason
    return record


def _parents(path: str):
    parent = PurePosixPath(path).parent
    while str(parent) != ".":
        yield str(parent)
        parent = parent.parent


def _validate_layout(records: list[dict[str, Any]]) -> None:
    members = {record["path"]: record for record in records}
    for record in records:
        for parent in _parents(record["path"]):
            if parent in members and members[parent]["type"] != "directory":
                raise RestoreError("Archive member is nested below a link or non-directory member")
    # A safe-looking link must not resolve through another link. This prevents a
    # relative link from becoming an escape through an absolute/skipped alias.
    for record in records:
        if record["type"] not in {"symlink", "hardlink"} or "skip_reason" in record:
            continue
        target = record["resolved_target"]
        chain = [target, *_parents(target)]
        if any(candidate in members and members[candidate]["type"] in {"symlink", "hardlink"} for candidate in chain):
            record["skip_reason"] = "target_traverses_link"
        elif record["type"] == "hardlink" and (
            target not in members or members[target]["type"] != "file"
        ):
            record["skip_reason"] = "hardlink_target_not_regular_file"


def _scan(archive: Archive) -> None:
    records = []
    seen = set()
    # Case-folded collisions are rejected too, so the same snapshot is safe on
    # case-insensitive recovery workstations as well as Linux filesystems.
    portable_names = set()
    detected_sqlite = set()
    archive.handle.seek(0)
    try:
        with gzip.GzipFile(fileobj=archive.handle, mode="rb") as decompressed:
            with tarfile.open(fileobj=decompressed, mode="r|") as tar:
                for member in tar:
                    record = _describe(member)
                    name = record["path"]
                    if name in seen or name.casefold() in portable_names:
                        raise RestoreError("Archive contains duplicate or case-colliding member paths")
                    seen.add(name)
                    portable_names.add(name.casefold())
                    records.append(record)
                    if member.isreg():
                        stream = tar.extractfile(member)
                        if stream is None:
                            raise RestoreError("Archive member payload is missing")
                        with stream:
                            if stream.read(len(SQLITE_HEADER)) == SQLITE_HEADER:
                                detected_sqlite.add(name)
            # Tar iteration can stop before the gzip trailer. Read every member
            # and trailer of the gzip stream so CRC/truncation errors cannot hide.
            while decompressed.read(CHUNK_SIZE):
                pass
    except (OSError, EOFError, tarfile.TarError, zlib.error) as exc:
        raise RestoreError("Archive gzip or tar stream is corrupt") from exc
    _validate_layout(records)
    regular = {record["path"] for record in records if record["type"] == "file"}
    if not archive.declared_sqlite <= regular:
        raise RestoreError("Manifest SQLite inventory does not identify regular archived files")
    archive.sqlite_files = detected_sqlite | archive.declared_sqlite
    for database in archive.sqlite_files:
        if any(database + suffix in seen for suffix in ("-wal", "-shm", "-journal")):
            raise RestoreError("SQLite archive includes live sidecars instead of a standalone database")
    archive.members = records
    _check_digest(archive)


def _directory(root: Path, parts: tuple[str, ...]) -> Path:
    current = root
    for part in parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            if current.is_symlink() or not current.is_dir():
                raise RestoreError("Extraction path is not a safe directory")
        current.chmod(0o700)
    return current


def _quick_check(path: Path) -> None:
    """Use an immutable read-only connection; never create WAL/journal files."""
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            result = connection.execute("PRAGMA quick_check").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RestoreError("Extracted SQLite database failed its read-only quick_check") from exc
    if result != [("ok",)]:
        raise RestoreError("Extracted SQLite database failed its read-only quick_check")


def _extract(archive: Archive, destination: Path) -> None:
    component = _directory(destination, (archive.name,))
    archive.handle.seek(0)
    try:
        with gzip.GzipFile(fileobj=archive.handle, mode="rb") as decompressed:
            with tarfile.open(fileobj=decompressed, mode="r|") as tar:
                index = 0
                for member in tar:
                    if index >= len(archive.members):
                        raise RestoreError("Archive changed during extraction")
                    expected = archive.members[index]
                    observed = _describe(member)
                    # skip_reason can be enriched by layout validation. Compare
                    # only the immutable header description from the scan.
                    if any(expected.get(key) != value for key, value in observed.items()):
                        raise RestoreError("Archive changed during extraction")
                    index += 1
                    path = PurePosixPath(expected["path"])
                    if member.isdir():
                        _directory(component, path.parts if str(path) != "." else ())
                    elif member.isreg():
                        parent = _directory(component, path.parent.parts)
                        descriptor = os.open(parent / path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(descriptor, "wb") as output:
                            stream = tar.extractfile(member)
                            if stream is None:
                                raise RestoreError("Archive member payload is missing")
                            with stream:
                                shutil.copyfileobj(stream, output, CHUNK_SIZE)
                            os.fchmod(output.fileno(), 0o600)
                    # No symlink or hardlink exists while payloads are written.
                if index != len(archive.members):
                    raise RestoreError("Archive changed during extraction")
            while decompressed.read(CHUNK_SIZE):
                pass
    except (OSError, EOFError, tarfile.TarError, zlib.error) as exc:
        raise RestoreError("Archive could not be extracted safely") from exc
    _check_digest(archive)
    for database in sorted(archive.sqlite_files):
        _quick_check(component / database)
    for record in archive.members:
        if record["type"] not in {"symlink", "hardlink"} or "skip_reason" in record:
            continue
        path = PurePosixPath(record["path"])
        parent = _directory(component, path.parent.parts)
        try:
            if record["type"] == "symlink":
                os.symlink(record["link_target"], parent / path.name)
            else:
                os.link(component / record["resolved_target"], parent / path.name, follow_symlinks=False)
        except OSError as exc:
            raise RestoreError("A reviewed in-tree link could not be created") from exc


def _sqlite_inventory(component: dict[str, Any]) -> set[str]:
    values = component.get("sqlite_databases", component.get("metadata", {}).get("sqlite_databases", []))
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise RestoreError("Manifest SQLite inventory must be a list of relative paths")
    return {_relative(value) for value in values}


def _write_report(destination: Path, report: dict[str, Any]) -> None:
    descriptor = os.open(destination / "report.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        # Escaping also supports Unix filenames containing surrogate-escaped
        # bytes, without exposing those names in terminal diagnostics.
        json.dump(report, output, indent=2, ensure_ascii=True)
        output.write("\n")
        os.fchmod(output.fileno(), 0o600)


def verify_snapshot(snapshot: Path | str, extract_to: Path | str | None = None) -> dict[str, Any]:
    """Verify a snapshot; optionally extract into a new directory outside it.

    Invalid archives are rejected before a destination is created. If an I/O or
    SQLite check fails during extraction, the private destination is retained
    with a failed report and must not be treated as a completed recovery copy.
    """
    snapshot = Path(snapshot).resolve()
    if not snapshot.is_dir():
        raise RestoreError("Snapshot must be an existing directory")
    destination = None
    if extract_to is not None:
        requested = Path(extract_to)
        if requested.exists() or requested.is_symlink():
            raise RestoreError("Extraction destination must not already exist")
        destination = requested.resolve()
        if destination == snapshot or snapshot in destination.parents:
            raise RestoreError("Extraction destination must be outside the snapshot directory")
        if not destination.parent.is_dir():
            raise RestoreError("Extraction destination parent must already exist")
    with ExitStack() as stack:
        try:
            manifest_file = stack.enter_context(_open_regular(snapshot / "manifest.json"))
            manifest = json.load(manifest_file, object_pairs_hook=_unique_pairs)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RestoreError("Manifest is not valid UTF-8 JSON") from exc
        if not isinstance(manifest, dict) or type(manifest.get("format_version")) is not int or manifest["format_version"] != 1:
            raise RestoreError("Unsupported snapshot manifest version")
        if manifest.get("completed") is not True:
            raise RestoreError("Snapshot manifest is not marked complete")
        components = manifest.get("components")
        if not isinstance(components, list) or not components:
            raise RestoreError("Snapshot manifest must contain components")
        archives = []
        names, filenames = set(), set()
        for component in components:
            if not isinstance(component, dict):
                raise RestoreError("Manifest component must be an object")
            name = _basename(component.get("name"), component=True)
            filename = _basename(component.get("archive"))
            if name.casefold() in names or filename.casefold() in filenames:
                raise RestoreError("Manifest contains duplicate component or archive names")
            names.add(name.casefold())
            filenames.add(filename.casefold())
            digest, size = component.get("sha256"), component.get("bytes")
            if not isinstance(digest, str) or not SHA256.fullmatch(digest):
                raise RestoreError("Manifest archive SHA-256 is invalid")
            if type(size) is not int or size <= 0:
                raise RestoreError("Manifest archive byte size is invalid")
            metadata = component.get("metadata", {})
            if not isinstance(metadata, dict):
                raise RestoreError("Manifest component metadata must be an object")
            handle = stack.enter_context(_open_regular(snapshot / filename))
            archive = Archive(name, filename, handle, size, digest.lower(), _sqlite_inventory(component), [], set())
            _check_digest(archive)
            _scan(archive)
            archives.append(archive)
        report = {
            "format_version": 1,
            "status": "verified",
            "completed": True,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_created_at": manifest.get("created_at"),
            "host_apply_performed": False,
            "extraction_permissions": {"files": "0600", "directories": "0700", "original_modes": "recorded only; not applied"},
            "sqlite_check_scope": "read-only quick_check during extraction" if destination else "not run; extraction is required for database quick_check",
            "components": [
                {"name": archive.name, "archive": archive.filename, "bytes": archive.expected_size,
                 "sha256": archive.expected_sha256, "members": archive.members,
                 "sqlite_databases": sorted(archive.sqlite_files),
                 "skipped_links": [record for record in archive.members if "skip_reason" in record],
                 "sqlite_quick_check": "pending" if destination and archive.sqlite_files else "not_run"}
                for archive in archives
            ],
        }
        if destination is not None:
            try:
                destination.mkdir(mode=0o700, exist_ok=False)
            except OSError as exc:
                raise RestoreError("Cannot create a new extraction destination") from exc
            destination.chmod(0o700)
            try:
                for archive, component_report in zip(archives, report["components"]):
                    _extract(archive, destination)
                    component_report["sqlite_quick_check"] = "ok" if archive.sqlite_files else "not_applicable"
                report["status"] = "extracted"
            except Exception:
                report["status"] = "failed"
                report["completed"] = False
                _write_report(destination, report)
                raise
            _write_report(destination, report)
        return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="Verify archives; optionally create an offline extraction")
    verify.add_argument("snapshot", type=Path)
    verify.add_argument("--extract", type=Path, metavar="DEST")
    args = parser.parse_args(argv)
    try:
        report = verify_snapshot(args.snapshot, args.extract)
    except (RestoreError, OSError) as exc:
        # Do not print archive contents, SQLite diagnostics, or untrusted paths.
        message = str(exc) if isinstance(exc, RestoreError) else "Local filesystem operation failed"
        print("Snapshot verification failed: " + message, file=sys.stderr)
        return 1
    skipped = sum(len(component["skipped_links"]) for component in report["components"])
    print(f"Snapshot {report['status']}: {len(report['components'])} components; {skipped} unsafe or unresolved links skipped; no host configuration applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
