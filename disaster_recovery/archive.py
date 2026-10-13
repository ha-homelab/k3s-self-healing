"""Create a private, verified component archive without writing to its source.

Exclusions are exact root-relative paths (a directory excludes its descendants).
An optional trailing ``/**`` also denotes a directory subtree. Other glob syntax,
absolute paths, empty components and ``..`` are rejected. In particular, callers
must name recorder files explicitly; ``*.db`` would also remove pairing state.
An explicit fixed generated-image policy can additionally omit only the reviewed
HA camera/mail cache names; no caller-supplied glob or regex is accepted.

SQLite databases are detected by their file header and copied with SQLite's
online backup API. Only the resulting standalone database is archived, without
its source WAL/SHM/journal sidecars. An inactive WAL-mode database that cannot be
opened read-only is copied to private scratch only when its main file and parent
directory stay unchanged and all sidecars remain absent. An exclusively locked
WAL-mode database can instead be copied with its WAL after a bounded busy wait,
but both files and their directory must stay unchanged throughout copying and
validation, and a second streaming read must match both captured files byte for
byte. SHM is a transient index rebuilt only for the private copy; the WAL
containing committed transactions is never discarded. This guarded physical
fallback detects observed changes; it does not offer the transactional guarantee
of SQLite's online backup API or an atomic filesystem snapshot. No archive is a
transaction spanning several independent application files or services.

Symlinks are recorded, never followed. Restore tooling must validate member paths
and skip unsafe link targets; the existing HA absolute self-aliases are retained
as evidence, not permission to extract outside the restore directory.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import gzip
import hashlib
import io
import json
import re
import math
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import tarfile
import tempfile
import time
from typing import BinaryIO, Iterator


SQLITE_HEADER = b"SQLite format 3\x00"
COPY_CHUNK = 1024 * 1024
MEMORY_FILE_LIMIT = 8 * 1024 * 1024
FILE_COPY_ATTEMPTS = 3
COMPONENT_ATTEMPTS = 3
SQLITE_BUSY_WAIT_SECONDS = 5.0


class ArchiveError(RuntimeError):
    """The source cannot be captured safely or the archive cannot be verified."""


class SourceChangedError(ArchiveError):
    """A regular file changed while it was copied."""


class _SQLiteBusyTimeout(sqlite3.OperationalError):
    """The online backup made no progress because its source remained locked."""

    def __init__(self, code: int):
        super().__init__("SQLite online backup remained busy beyond its lock-wait limit")
        self.sqlite_errorcode = code


class _MemoryFile(io.BytesIO):
    """Bound staging memory even if a file grows after its initial stat."""

    def write(self, value: bytes) -> int:
        if self.tell() + len(value) > MEMORY_FILE_LIMIT:
            raise SourceChangedError("Source file grew beyond the memory staging limit")
        return super().write(value)


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError("Component archive exceeded its time limit")


def _normalize_excludes(excludes: list[str] | None) -> tuple[str, ...]:
    normalized = []
    for original in excludes or []:
        if not isinstance(original, str):
            raise ValueError("Exclusions must be strings")
        value = original[:-3] if original.endswith("/**") else original
        parts = value.split("/")
        if (
            not value
            or any(part in ("", ".", "..") for part in parts)
            or any(character in value for character in ("*", "?", "[", "]", "\\", "\x00"))
        ):
            raise ValueError(f"Unsafe or unsupported exclusion: {original!r}")
        normalized.append(value)
    return tuple(sorted(set(normalized)))


def _excluded(relative: str, excludes: tuple[str, ...]) -> bool:
    return any(relative == item or relative.startswith(item + "/") for item in excludes)


@contextmanager
def _parent_fd(root_fd: int, relative: str) -> Iterator[tuple[int, str]]:
    """Resolve each source ancestor without traversing symlinks."""
    parts = PurePosixPath(relative).parts
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            following = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
            )
            os.close(current)
            current = following
        yield current, parts[-1]
    finally:
        os.close(current)


def _signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_mode,
        value.st_uid,
        value.st_gid,
    )


def _is_sqlite_sidecar(directory_fd: int, name: str) -> bool:
    """Recognize a live database's sidecars even when they disappear mid-scan."""
    for suffix in ("-wal", "-shm", "-journal"):
        if name.endswith(suffix):
            try:
                descriptor = os.open(
                    name[:-len(suffix)],
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
            except OSError as error:
                if error.errno in (errno.ENOENT, errno.ELOOP):
                    # A database alias is archived as a symlink, never followed.
                    # Its similarly named sidecars cannot be safely classified
                    # as part of a SQLite backup, so preserve their entries too.
                    return False
                raise
            with os.fdopen(descriptor, "rb") as database:
                return (
                    stat.S_ISREG(os.fstat(database.fileno()).st_mode)
                    and database.read(len(SQLITE_HEADER)) == SQLITE_HEADER
                )
    return False



GENERATED_IMAGE_CACHES = frozenset({'ha-camera-alerts', 'mail-and-packages'})
_CAMERA_IMAGE = re.compile(
    r'www/(?:(?:front|backdoor|garage|porch|ring)_camera_alert(?:[234])?\.jpg'
    r'|\.(?:front|backdoor|garage|porch|ring)_camera_alert\.pending\.jpg'
    r'|\.(?:front|backdoor|garage|porch|ring)_camera_alert(?:[234])?\.jpg[a-z0-9_]{8}'
    r'|\.\.(?:front|backdoor|garage|porch|ring)_camera_alert\.pending\.jpg[a-z0-9_]{8})'
)


def _generated_image(relative: str, groups: tuple[str, ...]) -> bool:
    if 'ha-camera-alerts' in groups and _CAMERA_IMAGE.fullmatch(relative):
        return True
    if 'mail-and-packages' in groups:
        path = PurePosixPath(relative)
        if path.parent == PurePosixPath('www/mail_and_packages'):
            return path.suffix in {'.gif', '.jpg', '.png'}
        if relative.startswith('custom_components/mail_and_packages/images/'):
            return path.suffix in {'.gif', '.jpg', '.png', '.mp4'}
    return False


def _tree_change_details(before: dict, after: dict) -> str:
    changes = {
        'added': sorted(after.keys() - before.keys()),
        'removed': sorted(before.keys() - after.keys()),
        'changed': sorted(name for name in before.keys() & after.keys() if before[name] != after[name]),
    }
    # Paths only, bounded and JSON escaped. The runner keeps this in private NAS
    # diagnostics, never source contents, credentials, or public container logs.
    return json.dumps({kind: {'count': len(paths), 'paths': paths[:10]} for kind, paths in changes.items()})


def _entries(
    root_fd: int, excludes: tuple[str, ...], deadline: float, generated_image_caches: tuple[str, ...] = ()
) -> list[tuple[str, os.stat_result]]:
    entries = []

    def visit(directory_fd: int, prefix: str) -> None:
        _check_deadline(deadline)
        with os.scandir(directory_fd) as scanned:
            names = sorted(entry.name for entry in scanned)
        for name in names:
            _check_deadline(deadline)
            relative = f"{prefix}/{name}" if prefix else name
            if _excluded(relative, excludes):
                continue
            if _is_sqlite_sidecar(directory_fd, name):
                continue
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                # Atomic image publishers unlink temporary names between scan
                # and stat. No ordinary source disappearance is ignored.
                if _generated_image(relative, generated_image_caches):
                    continue
                raise
            if stat.S_ISREG(metadata.st_mode) and _generated_image(relative, generated_image_caches):
                continue
            entries.append((relative, metadata))
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    if (os.fstat(child).st_dev, os.fstat(child).st_ino) != (
                        metadata.st_dev, metadata.st_ino
                    ):
                        raise SourceChangedError(f"Source directory changed: {relative}")
                    visit(child, relative)
                finally:
                    os.close(child)

    visit(root_fd, "")
    # A database sorts before its own -wal/-shm sidecars, including in subdirectories.
    return sorted(entries, key=lambda item: item[0])


def _tree_signatures(
    entries: list[tuple[str, os.stat_result]], sqlite_paths: set[str]
) -> dict[str, tuple[int, ...]]:
    signatures = {}
    sidecars = {
        name + suffix
        for name in sqlite_paths
        for suffix in ("-wal", "-shm", "-journal")
    }
    for relative, value in entries:
        if relative in sidecars:
            continue
        if relative in sqlite_paths or stat.S_ISDIR(value.st_mode):
            # WAL creation changes directory mtime; the member set catches actual
            # additions/removals. SQLite contents may change during online backup.
            signatures[relative] = (
                value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid
            )
        else:
            signatures[relative] = _signature(value)
    return signatures


def _copy_stream(source: BinaryIO, target: BinaryIO, deadline: float) -> int:
    length = 0
    while True:
        _check_deadline(deadline)
        block = source.read(COPY_CHUNK)
        if not block:
            return length
        target.write(block)
        length += len(block)


def _copy_regular(
    root_fd: int, relative: str, target: Path, deadline: float
) -> tuple[os.stat_result, bool, io.BytesIO | None]:
    """Stage stable small files in bounded RAM; databases and large files use NAS."""
    last_error: Exception | None = None
    for _ in range(FILE_COPY_ATTEMPTS):
        _check_deadline(deadline)
        memory = None
        try:
            with _parent_fd(root_fd, relative) as (parent, name):
                # Do not block if a producer replaced the regular file with a FIFO.
                descriptor = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                )
                with os.fdopen(descriptor, "rb") as source:
                    before = os.fstat(source.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise ArchiveError(f"Source is no longer a regular file: {relative}")
                    header = source.read(len(SQLITE_HEADER))
                    if header == SQLITE_HEADER:
                        return before, True, None
                    source.seek(0)
                    if before.st_size <= MEMORY_FILE_LIMIT:
                        memory = _MemoryFile()
                        copied = _copy_stream(source, memory, deadline)
                    else:
                        target.touch(mode=0o600, exist_ok=True)
                        with target.open("wb") as output:
                            copied = _copy_stream(source, output, deadline)
                    after = os.fstat(source.fileno())
                    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (
                    copied != before.st_size
                    or _signature(before) != _signature(after)
                    or _signature(before) != _signature(current)
                ):
                    raise SourceChangedError(f"Source file changed during copy: {relative}")
                if memory is not None:
                    memory.seek(0)
                payload, memory = memory, None  # Transfer ownership to the archive writer.
                return before, False, payload
        except (SourceChangedError, FileNotFoundError) as error:
            last_error = error
        finally:
            if memory is not None:
                memory.close()
    raise SourceChangedError(
        f"Source file did not stabilize after {FILE_COPY_ATTEMPTS} attempts: {relative}"
    ) from last_error


def _quiescent_sqlite_signature(root_fd: int, relative: str) -> tuple | None:
    """A raw database copy is allowed only with no journal or WAL state at all."""
    with _parent_fd(root_fd, relative) as (parent, name):
        before = os.fstat(parent)
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode):
            raise SourceChangedError(f"SQLite source was replaced: {relative}")
        for suffix in ("-wal", "-shm", "-journal"):
            try:
                os.stat(name + suffix, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            return None
        after = os.fstat(parent)
        if _signature(before) != _signature(after):
            raise SourceChangedError(f"SQLite source directory changed: {relative}")
        return _signature(current), _signature(after)


def _require_quiescent_sqlite(root_fd: int, relative: str, expected: tuple) -> None:
    if _quiescent_sqlite_signature(root_fd, relative) != expected:
        raise SourceChangedError(f"Inactive SQLite source changed: {relative}")


def _snapshot_quiescent_sqlite(
    root_fd: int, relative: str, target: Path, deadline: float, expected: tuple
) -> None:
    """Normalize a guarded raw copy on scratch, never bypass active source WAL."""
    _require_quiescent_sqlite(root_fd, relative, expected)
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    with _parent_fd(root_fd, relative) as (parent, name):
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        with os.fdopen(descriptor, "rb") as source:
            if _signature(os.fstat(source.fileno())) != expected[0]:
                raise SourceChangedError(f"Inactive SQLite source changed: {relative}")
            target.touch(mode=0o600, exist_ok=False)
            with target.open("wb") as output:
                copied = _copy_stream(source, output, deadline)
            if copied != expected[0][2] or _signature(os.fstat(source.fileno())) != expected[0]:
                raise SourceChangedError(f"Inactive SQLite source changed during copy: {relative}")
    _require_quiescent_sqlite(root_fd, relative, expected)
    # Opening this private NAS copy writable can create the missing WAL/SHM.
    # No immutable connection is ever made to the live source.
    output = sqlite3.connect(target, timeout=1.0)
    try:
        output.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 1000)
        if output.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise ArchiveError(f"Cannot make inactive SQLite snapshot standalone: {relative}")
        if output.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ArchiveError(f"SQLite integrity check failed: {relative}")
    finally:
        output.close()
    _check_deadline(deadline)
    _require_quiescent_sqlite(root_fd, relative, expected)


def _snapshot_sqlite_online(
    source_root: Path, relative: str, target: Path, deadline: float
) -> None:
    """Read the actual source pathname so SQLite incorporates every committed WAL page."""
    source_uri = (source_root / relative).as_uri() + "?mode=ro"
    source = sqlite3.connect(source_uri, uri=True, timeout=0.1)
    try:
        source.execute("PRAGMA query_only=ON")
        output = sqlite3.connect(target)
        try:
            output.set_progress_handler(
                lambda: 1 if time.monotonic() >= deadline else 0, 1000
            )

            busy_since = None

            def progress(status: int, _remaining: int, _total: int) -> None:
                nonlocal busy_since
                _check_deadline(deadline)
                if status & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    now = time.monotonic()
                    if busy_since is None:
                        busy_since = now
                    elif now - busy_since >= SQLITE_BUSY_WAIT_SECONDS:
                        raise _SQLiteBusyTimeout(status)
                else:
                    # Healthy large backups retain the full component deadline.
                    busy_since = None

            source.backup(output, pages=128, progress=progress, sleep=0.05)
            _check_deadline(deadline)
            result = output.execute("PRAGMA quick_check").fetchall()
            if result != [("ok",)]:
                raise ArchiveError(f"SQLite integrity check failed: {relative}")
            output.execute("PRAGMA journal_mode=DELETE")
        finally:
            output.close()
    finally:
        source.close()


def _stable_wal_signature(root_fd: int, relative: str) -> tuple:
    """Capture one guard for the main file, optional WAL and their directory."""
    with _parent_fd(root_fd, relative) as (parent, name):
        before = os.fstat(parent)
        try:
            os.stat(name + "-journal", dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ArchiveError(f"Physical SQLite copy refuses a rollback journal: {relative}")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as source:
            main = os.fstat(source.fileno())
            if not stat.S_ISREG(main.st_mode):
                raise SourceChangedError(f"SQLite source was replaced: {relative}")
            header = source.read(20)
            if not header.startswith(SQLITE_HEADER) or header[18:20] != b"\x02\x02":
                raise ArchiveError(f"Physical SQLite copy requires WAL mode: {relative}")
        try:
            wal = os.stat(name + "-wal", dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            wal = None
        if wal is not None and not stat.S_ISREG(wal.st_mode):
            raise ArchiveError(f"SQLite WAL must be a regular file: {relative}")
        if wal is not None and 0 < wal.st_size < 32:
            raise SourceChangedError(f"SQLite WAL header is incomplete: {relative}")
        wal_header = None
        if wal is not None:
            descriptor = os.open(
                name + "-wal", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
            with os.fdopen(descriptor, "rb") as source:
                if _signature(os.fstat(source.fileno())) != _signature(wal):
                    raise SourceChangedError(f"SQLite WAL changed before its header read: {relative}")
                # A reset changes the salts/generation even on filesystems with
                # coarse timestamps and an unchanged physical WAL file size.
                wal_header = source.read(32)
        after = os.fstat(parent)
        if _signature(before) != _signature(after):
            raise SourceChangedError(f"SQLite source directory changed: {relative}")
        return (_signature(main), _signature(wal) if wal is not None else None,
                _signature(after), wal_header)


def _require_stable_wal(root_fd: int, relative: str, expected: tuple) -> None:
    try:
        observed = _stable_wal_signature(root_fd, relative)
    except ArchiveError as error:
        raise SourceChangedError(f"SQLite source layout changed during physical copy: {relative}") from error
    if observed != expected:
        raise SourceChangedError(f"SQLite main file or WAL changed during physical copy: {relative}")


def _stream_digest(source: BinaryIO, deadline: float) -> bytes:
    digest = hashlib.sha256()
    while True:
        _check_deadline(deadline)
        block = source.read(COPY_CHUNK)
        if not block:
            return digest.digest()
        digest.update(block)


def _verify_copied_sqlite_part(
    root_fd: int, relative: str, copied_path: Path, expected: tuple, deadline: float
) -> None:
    """A second streaming source read must agree with the entire physical copy."""
    with copied_path.open("rb") as copied:
        copied_digest = _stream_digest(copied, deadline)
    with _parent_fd(root_fd, relative) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as source:
            if _signature(os.fstat(source.fileno())) != expected:
                raise SourceChangedError(f"SQLite source changed before second read: {relative}")
            actual_digest = _stream_digest(source, deadline)
            if _signature(os.fstat(source.fileno())) != expected:
                raise SourceChangedError(f"SQLite source changed during second read: {relative}")
    if actual_digest != copied_digest:
        raise SourceChangedError(f"SQLite source bytes changed between reads: {relative}")


def _snapshot_stable_wal(root_fd: int, relative: str, target: Path, deadline: float) -> None:
    """Recover a physically unchanged main/WAL pair exclusively on private scratch.

    One validation window covers BOTH copies: signatures are captured for both
    before either is read, rechecked after copying, full second-pass source hashes,
    and validation. Equal byte hashes strengthen metadata checks against coarse
    timestamps and in-place WAL resets. These checks are optimistic change
    detection, not a substitute for a SQLite lock or an atomic filesystem snapshot.
    Source SHM is neither copied nor consulted: SQLite reconstructs the transient
    WAL index from the copied WAL's commit/checksum records. No source lock, SQL
    write, checkpoint, immutable read or service pause is performed.
    """
    expected = _stable_wal_signature(root_fd, relative)
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    for suffix, signature in (("", expected[0]), ("-wal", expected[1])):
        if signature is None:
            continue
        _check_deadline(deadline)
        with _parent_fd(root_fd, relative + suffix) as (parent, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as source:
                if _signature(os.fstat(source.fileno())) != signature:
                    raise SourceChangedError(f"SQLite source changed before physical copy: {relative}")
                copied_path = Path(str(target) + suffix)
                copied_path.touch(mode=0o600, exist_ok=False)
                with copied_path.open("wb") as output:
                    copied = _copy_stream(source, output, deadline)
                if copied != signature[2] or _signature(os.fstat(source.fileno())) != signature:
                    raise SourceChangedError(f"SQLite source changed during physical copy: {relative}")
    _require_stable_wal(root_fd, relative, expected)
    for suffix, signature in (("", expected[0]), ("-wal", expected[1])):
        if signature is not None:
            _verify_copied_sqlite_part(
                root_fd, relative + suffix, Path(str(target) + suffix), signature, deadline
            )
    _require_stable_wal(root_fd, relative, expected)
    output = sqlite3.connect(target, timeout=1.0)
    try:
        output.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 1000)
        # There is exactly one connection to this private copy. Exclusive mode
        # avoids shared-memory coordination while replaying its WAL on NAS.
        output.execute("PRAGMA locking_mode=EXCLUSIVE")
        if output.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise ArchiveError(f"Cannot make copied WAL database standalone: {relative}")
        if output.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ArchiveError(f"Copied WAL database failed its integrity check: {relative}")
    finally:
        output.close()
    _check_deadline(deadline)
    _require_stable_wal(root_fd, relative, expected)


def _snapshot_sqlite(
    source_root: Path,
    root_fd: int,
    relative: str,
    original: os.stat_result,
    target: Path,
    deadline: float,
) -> tuple[str, tuple | None]:
    """Use the source's real pathname so SQLite can read its live WAL correctly."""
    _check_deadline(deadline)
    # The path must continue to refer to the database we inspected. The source is
    # mounted read-only by the job; mode=ro and query_only additionally forbid SQL writes.
    with _parent_fd(root_fd, relative) as (parent, name):
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (
            current.st_dev, current.st_ino
        ) != (original.st_dev, original.st_ino):
            raise SourceChangedError(f"SQLite source was replaced: {relative}")
    target.unlink(missing_ok=True)
    target.touch(mode=0o600, exist_ok=False)
    quiescent = _quiescent_sqlite_signature(root_fd, relative)
    fallback_guard = None
    method = "online"
    try:
        _snapshot_sqlite_online(source_root, relative, target, deadline)
    except sqlite3.OperationalError as error:
        error.add_note(f"SQLite snapshot source: {relative}")
        # A read-only mount cannot initialize absent sidecars for an inactive
        # WAL-mode database. Never discard WAL state or fall back for corruption.
        code = getattr(error, "sqlite_errorcode", 0) & 0xFF
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            _snapshot_stable_wal(root_fd, relative, target, deadline)
            method = "stable_wal"
        elif code in (sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_READONLY) and quiescent is not None:
            _snapshot_quiescent_sqlite(root_fd, relative, target, deadline, quiescent)
            fallback_guard = quiescent
            method = "quiescent"
        else:
            raise
    target.chmod(0o600)
    with _parent_fd(root_fd, relative) as (parent, name):
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (
            current.st_dev, current.st_ino
        ) != (original.st_dev, original.st_ino):
            raise SourceChangedError(f"SQLite source changed identity: {relative}")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(target) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ArchiveError(f"SQLite snapshot has an unexpected sidecar: {relative}")
    return method, fallback_guard


def _tar_info(relative: str, metadata: os.stat_result) -> tarfile.TarInfo:
    info = tarfile.TarInfo(relative)
    info.mode = stat.S_IMODE(metadata.st_mode)
    info.uid = metadata.st_uid
    info.gid = metadata.st_gid
    info.mtime = metadata.st_mtime
    return info


def _create_archive_attempt(
    source: Path,
    destination: Path,
    exclusions: tuple[str, ...],
    deadline: float,
    generated_image_caches: tuple[str, ...] = (),
) -> dict:
    parent = destination.parent
    metadata: dict = {
        "archive": destination.name,
        "files": 0,
        "directories": 1,  # Includes the source root, stored as the '.' member.
        "uncompressed_bytes": 0,
        "sqlite_databases": [],
        "sqlite_quiescent_copies": [],
        "sqlite_stable_wal_copies": [],
        "symlinks": [],
        "excludes": list(exclusions),
        "generated_image_caches": list(generated_image_caches),
    }
    root_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        root_metadata = os.fstat(root_fd)
        entries = _entries(root_fd, exclusions, deadline, generated_image_caches)
        with tempfile.TemporaryDirectory(prefix=".ha-dr-archive-", dir=parent) as scratch:
            temporary = Path(scratch)
            draft = temporary / "archive.tar.gz"
            draft.touch(mode=0o600)
            staged = temporary / "file"
            skipped_sidecars: set[str] = set()
            quiescent_guards: dict[str, tuple] = {}
            names = ["."]
            with tarfile.open(draft, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                root_info = _tar_info(".", root_metadata)
                root_info.type = tarfile.DIRTYPE
                archive.addfile(root_info)
                for relative, initial in entries:
                    _check_deadline(deadline)
                    if relative in skipped_sidecars:
                        continue
                    info = _tar_info(relative, initial)
                    if stat.S_ISDIR(initial.st_mode):
                        info.type = tarfile.DIRTYPE
                        archive.addfile(info)
                        metadata["directories"] += 1
                    elif stat.S_ISLNK(initial.st_mode):
                        with _parent_fd(root_fd, relative) as (ancestor, name):
                            target = os.readlink(name, dir_fd=ancestor)
                            current = os.stat(name, dir_fd=ancestor, follow_symlinks=False)
                        if _signature(current) != _signature(initial):
                            raise SourceChangedError(f"Source symlink changed: {relative}")
                        info.type = tarfile.SYMTYPE
                        info.linkname = target
                        archive.addfile(info)
                        metadata["symlinks"].append({"path": relative, "target": target})
                    elif stat.S_ISREG(initial.st_mode):
                        original, is_sqlite, memory = _copy_regular(root_fd, relative, staged, deadline)
                        if is_sqlite:
                            method, guard = _snapshot_sqlite(source, root_fd, relative, original, staged, deadline)
                            if method == "stable_wal":
                                metadata["sqlite_stable_wal_copies"].append(relative)
                            if guard is not None:
                                quiescent_guards[relative] = guard
                                metadata["sqlite_quiescent_copies"].append(relative)
                            metadata["sqlite_databases"].append(relative)
                            skipped_sidecars.update(
                                relative + suffix for suffix in ("-wal", "-shm", "-journal")
                            )
                        info = _tar_info(relative, original)
                        if memory is not None:
                            info.size = original.st_size
                            with memory:
                                archive.addfile(info, memory)
                        else:
                            info.size = staged.stat().st_size
                            with staged.open("rb") as payload:
                                archive.addfile(info, payload)
                            staged.unlink()
                        metadata["files"] += 1
                        metadata["uncompressed_bytes"] += info.size
                    else:
                        raise ArchiveError(f"Unsupported special source file: {relative}")
                    names.append(relative)

            current_entries = _entries(root_fd, exclusions, deadline, generated_image_caches)
            for relative, guard in quiescent_guards.items():
                _require_quiescent_sqlite(root_fd, relative, guard)
            sqlite_paths = set(metadata["sqlite_databases"])
            before = _tree_signatures([(".", root_metadata), *entries], sqlite_paths)
            after = _tree_signatures([(".", os.fstat(root_fd)), *current_entries], sqlite_paths)
            if before != after:
                raise SourceChangedError("Source tree changed during component capture: " + _tree_change_details(before, after))

            # Read through the gzip trailer (tar EOF alone does not check its CRC).
            with gzip.open(draft, "rb") as compressed:
                while compressed.read(COPY_CHUNK):
                    _check_deadline(deadline)
            with tarfile.open(draft, "r:gz") as archive:
                actual_names = [item.name for item in archive]
            if actual_names != names or len(set(actual_names)) != len(actual_names):
                raise ArchiveError("Archive member verification failed")
            digest = hashlib.sha256()
            with draft.open("rb") as output:
                while chunk := output.read(COPY_CHUNK):
                    _check_deadline(deadline)
                    digest.update(chunk)
                os.fsync(output.fileno())
            metadata.update(sha256=digest.hexdigest(), bytes=draft.stat().st_size)
            _check_deadline(deadline)
            os.link(draft, destination)
            draft.unlink()
    finally:
        os.close(root_fd)
    return metadata


def create_archive(
    source: Path,
    destination: Path,
    excludes: list[str] | None = None,
    timeout_seconds: float = 120,
    generated_image_caches: list[str] | None = None,
) -> dict:
    """Atomically publish a gzip tar and return its checksum and capture metadata.

    Destination must not already exist or be inside source. Scratch files live
    only in destination.parent (the NAS workspace). Publication uses a hard link
    on that same filesystem, so concurrent writers cannot replace an existing
    completed archive. Special files (devices, sockets, FIFOs) fail closed.

    Ordinary source files and tree membership must be unchanged between the
    initial inventory and final check. A changed tree retries the whole component
    up to three times under ONE shared deadline, then raises SourceChangedError.
    Ordinary files at most 8 MiB are staged in bounded memory to avoid a separate
    NAS write/read/unlink cycle; SQLite and larger files always use NAS scratch.
    Online SQLite snapshots exclude changing database content and sidecars from
    that comparison, while still detecting database replacement/removal.
    Inactive databases copied after a read-only WAL open failure retain strict
    file/parent signatures and absent-sidecar checks through the final inventory.
    Busy WAL databases use guarded physical copies with streaming second-pass
    hashes; this fallback is explicitly recorded and is not transactional backup.
    The '.' directory member records the source root's ownership, mode and mtime;
    the returned directories count includes that root.
    """
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    deadline = time.monotonic() + timeout_seconds
    exclusions = _normalize_excludes(excludes)
    groups = tuple(generated_image_caches or ())
    if any(group not in GENERATED_IMAGE_CACHES for group in groups):
        raise ValueError('Unknown generated image cache policy')
    source = Path(source).absolute()
    destination = Path(destination).absolute()
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Source must be a directory, not a symlink")
    source = source.resolve(strict=True)
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    if parent.is_relative_to(source):
        raise ValueError("Destination must be outside the source directory")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    last_error: Exception | None = None
    for attempt in range(1, COMPONENT_ATTEMPTS + 1):
        _check_deadline(deadline)
        try:
            metadata = _create_archive_attempt(source, destination, exclusions, deadline, groups)
            metadata["attempts"] = attempt
            return metadata
        except (SourceChangedError, FileNotFoundError) as error:
            last_error = error
    raise SourceChangedError(
        f"Source tree did not stabilize after {COMPONENT_ATTEMPTS} component attempts"
    ) from last_error
