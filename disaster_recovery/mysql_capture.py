"""Verify captures for the supported managed K3s MySQL layout, without backend discovery."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

CLIENT_IMAGE = 'docker.io/library/mysql:8.4.10@sha256:8dbcf531a03aade657e181b9cf2f1d1803ce621a1d55610cb44cb531ab7d7db6'
SQLITE_EXCLUDES = ('db/state.db', 'db/state.db-wal', 'db/state.db-shm', 'db/state.db-journal')
MAX_RECEIPT = 64 * 1024
MAX_CIPHER = 2 * 1024**3
_RECEIPT_FIELDS = {'status', 'format_version', 'started_epoch', 'completed_epoch', 'schema',
                   'client_version', 'client_image', 'sql_sha256', 'cipher_sha256', 'cipher_bytes', 'proof'}
_PROOF_FIELDS = {'rows', 'revision', 'compact_revision', 'next_id', 'typed_rows_sha256'}


def _identity(info):
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _version(info):
    return _identity(info) + (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _directory(path, *, parent=None):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            os.close(descriptor)
            raise ValueError('Capture directory must be an ordinary directory')
        return descriptor
    except OSError:
        raise ValueError('Capture directory is missing or unsafe') from None


def _regular(directory, name, limit):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 <= info.st_size <= limit or info.st_nlink < 1:
            raise ValueError('Capture input must be a bounded regular file')
        return descriptor, info
    except BaseException:
        os.close(descriptor)
        raise


def _linked(directory, name, descriptor, before):
    current = os.fstat(descriptor)
    linked = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if _version(current) != _version(before) or _version(linked) != _version(before):
        raise ValueError('Capture input changed during verification')


def _read(descriptor, before, limit):
    pieces = []
    size = 0
    # Read no more than the declared initial size plus one byte. A writer cannot
    # turn a bounded regular file into an unbounded hashing/read workload.
    while True:
        block = os.read(descriptor, min(1024 * 1024, before.st_size + 1 - size))
        if not block:
            break
        pieces.append(block)
        size += len(block)
        if size > limit or size > before.st_size:
            raise ValueError('Capture input grew during verification')
    if size != before.st_size or _version(os.fstat(descriptor)) != _version(before):
        raise ValueError('Capture input changed during verification')
    return b''.join(pieces)


def _host_file(root, parts, limit):
    """Read one fixed relative path without traversing untrusted symlinks."""
    directories = []
    links = []
    current = root
    descriptor = None
    try:
        for component in parts[:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            directories.append(child)
            links.append((current, component, child))
            current = child
        descriptor, before = _regular(current, parts[-1], limit)
        data = _read(descriptor, before, limit)
        _linked(current, parts[-1], descriptor, before)
        for parent, name, child in links:
            if _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _identity(os.fstat(child)):
                raise ValueError('Managed datastore path changed during verification')
        return data
    except FileNotFoundError:
        if descriptor is not None:
            raise ValueError('Managed datastore path changed during verification') from None
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for directory in reversed(directories):
            os.close(directory)


def mysql_schema(host):
    """Parse only the supported managed assignment; never expose its credentials."""
    root = _directory(host)
    try:
        data = _host_file(root, ('etc', 'rancher', 'k3s', 'mysql-datastore.env'), MAX_RECEIPT)
        if data is None:
            marker = _host_file(root, ('etc', 'systemd', 'system', 'k3s.service.d',
                                       '92-kine-mysql.conf'), MAX_RECEIPT)
            if marker is not None:
                raise ValueError('Managed MySQL service dependency has no endpoint configuration')
            return None
        try:
            text = data.decode('utf-8')
        except UnicodeError:
            raise ValueError('Managed datastore configuration is not supported UTF-8') from None
        assignments = [line for line in text.splitlines()
                       if line.strip() and not line.lstrip().startswith('#')
                       and line.split('=', 1)[0].strip() == 'K3S_DATASTORE_ENDPOINT']
        if len(assignments) != 1:
            raise ValueError('Missing or ambiguous managed K3s endpoint')
        match = re.fullmatch(r'K3S_DATASTORE_ENDPOINT=(?:"([^"\\\r\n]*)"|([^"\s\\]+))', assignments[0])
        endpoint = (match.group(1) if match.group(1) is not None else match.group(2)) if match else ''
        target = re.fullmatch(r'mysql://[^\r\n]+@tcp\(127\.0\.0\.1:13306\)/([a-z][a-z0-9_]{0,63})', endpoint)
        if not target:
            raise ValueError('Malformed or unsupported managed K3s endpoint')
        return target.group(1)
    except OSError:
        raise ValueError('Managed datastore configuration is missing or unsafe') from None
    finally:
        os.close(root)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate capture receipt field')
        result[key] = value
    return result


def _receipt(receipt, now):
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise ValueError('K3s MySQL receipt fields differ')
    proof = receipt['proof']
    if not isinstance(proof, dict) or set(proof) != _PROOF_FIELDS:
        raise ValueError('K3s MySQL proof fields differ')
    if (receipt['status'] != 'qualified' or type(receipt['format_version']) is not int
            or receipt['format_version'] != 1 or receipt['client_version'] != '8.4.10'
            or receipt['client_image'] != CLIENT_IMAGE
            or type(receipt['started_epoch']) is not int or type(receipt['completed_epoch']) is not int
            or not 0 <= now - receipt['completed_epoch'] <= 1800
            or not 0 <= receipt['completed_epoch'] - receipt['started_epoch'] <= 660
            or not isinstance(receipt['schema'], str)
            or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', receipt['schema'])):
        raise ValueError('K3s MySQL capture is not qualified and fresh')
    for key in ('rows', 'revision', 'compact_revision', 'next_id'):
        if type(proof[key]) is not int or proof[key] < 0:
            raise ValueError('Invalid K3s MySQL snapshot proof')
    if not (proof['rows'] > 0 and proof['compact_revision'] <= proof['revision'] < proof['next_id']):
        raise ValueError('Invalid K3s MySQL revision bounds')
    for value in (receipt['sql_sha256'], receipt['cipher_sha256'], proof['typed_rows_sha256']):
        if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
            raise ValueError('Missing K3s MySQL digest proof')
    if type(receipt['cipher_bytes']) is not int or not 0 < receipt['cipher_bytes'] <= MAX_CIPHER:
        raise ValueError('Invalid K3s MySQL capture size')


def _inventory(directory):
    names = set()
    with os.scandir(directory) as entries:
        for entry in entries:
            names.add(entry.name)
            if len(names) > 2:
                raise ValueError('K3s MySQL capture inventory differs')
    if names != {'capture.json', 'k3s-mysql.sql.gz.age'}:
        raise ValueError('K3s MySQL capture inventory differs')


@contextmanager
def verified_capture(folder, *, now=None):
    """Keep verified files and their directory bindings stable across consumption."""
    if now is not None and (type(now) not in (int, float) or not math.isfinite(now)):
        raise ValueError('Capture verification requires a finite clock')
    clock = time.time if now is None else lambda: now
    folder = Path(folder).absolute()
    parent = _directory(folder.parent)
    try:
        directory = _directory(folder.name, parent=parent)
    except BaseException:
        os.close(parent)
        raise
    descriptors = []
    consumer_failure = None
    try:
        initial_parent = os.fstat(parent)
        initial_directory = os.fstat(directory)
        _inventory(directory)
        receipt_fd, receipt_before = _regular(directory, 'capture.json', MAX_RECEIPT)
        descriptors.append(receipt_fd)
        try:
            receipt = json.loads(_read(receipt_fd, receipt_before, MAX_RECEIPT), object_pairs_hook=_unique)
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            raise ValueError('K3s MySQL capture receipt is invalid') from None
        _receipt(receipt, clock())
        cipher_fd, cipher_before = _regular(directory, 'k3s-mysql.sql.gz.age', MAX_CIPHER)
        descriptors.append(cipher_fd)
        if cipher_before.st_size != receipt['cipher_bytes']:
            raise ValueError('K3s MySQL capture size mismatch')
        digest = hashlib.sha256()
        size = 0
        while True:
            block = os.read(cipher_fd, min(1024 * 1024, receipt['cipher_bytes'] + 1 - size))
            if not block:
                break
            size += len(block)
            if size > receipt['cipher_bytes']:
                raise ValueError('K3s MySQL capture grew during verification')
            digest.update(block)
        if size != receipt['cipher_bytes'] or digest.hexdigest() != receipt['cipher_sha256']:
            raise ValueError('K3s MySQL encrypted capture digest mismatch')
        def require_unchanged():
            _linked(directory, 'capture.json', receipt_fd, receipt_before)
            _linked(directory, 'k3s-mysql.sql.gz.age', cipher_fd, cipher_before)
            _inventory(directory)
            if (_version(os.fstat(directory)) != _version(initial_directory)
                    or _version(os.fstat(parent)) != _version(initial_parent)
                    or _identity(os.stat(folder.name, dir_fd=parent, follow_symlinks=False))
                    != _identity(initial_directory)
                    or _version(os.stat(folder.parent, follow_symlinks=False)) != _version(initial_parent)):
                raise ValueError('K3s MySQL capture directory changed during verification')
            _receipt(receipt, clock())

        require_unchanged()
        try:
            yield receipt
        except BaseException as error:
            consumer_failure = error
            raise
        # Body failures retain their original exception; a successful consumer
        # must pass the final bindings check before accepting its output.
        require_unchanged()
    except OSError:
        if consumer_failure is not None:
            raise
        raise ValueError('K3s MySQL capture is missing, unsafe or changed') from None
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
        os.close(directory)
        os.close(parent)


def validate_capture(folder, *, now=None):
    with verified_capture(folder, now=now) as receipt:
        return receipt


def prepare_capture(config, host):
    capture = config.get('k3s_mysql_capture')
    active = mysql_schema(host)
    if active and capture is None:
        raise ValueError('MySQL-backed K3s requires its own database capture; stale SQLite is not a backup')
    if capture is None:
        return None
    if capture != '/kine-capture/ready':
        raise ValueError('Use the reviewed K3s MySQL capture mount')
    if not active:
        raise ValueError('MySQL capture configured without the managed MySQL endpoint')
    receipt = validate_capture(capture)
    if receipt['schema'] != active:
        raise ValueError('K3s MySQL capture belongs to a different datastore schema')
    return receipt
