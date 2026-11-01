"""Bound opt-in local raw captures; final archives always remain on the NAS."""
from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import uuid


GIB = 1024 ** 3
RENDER_OVERHEAD = GIB
CHUNK = 1024 ** 2
DISK_FILESYSTEMS = {'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'zfs', 'f2fs'}
NONDISK_FILESYSTEMS = {'tmpfs', 'ramfs', 'nfs', 'nfs4', 'cifs', 'smb3', '9p', 'ceph'}


def validate_local_capture(value, *, staged_capture):
    if value is None:
        return None
    if staged_capture is not True:
        raise ValueError('local_capture requires staged_capture=true')
    if type(value) is not dict or set(value) != {'directory', 'max_bytes', 'minimum_free_bytes'}:
        raise ValueError('local_capture requires directory, max_bytes and minimum_free_bytes')
    directory = value['directory']
    if (not isinstance(directory, str) or not directory or directory == '/'
            or not directory.startswith('/') or directory.startswith('//')
            or str(PurePosixPath(directory)) != directory
            or '..' in PurePosixPath(directory).parts or '\\' in directory
            or any(ord(c) < 32 or ord(c) == 127 for c in directory)):
        raise ValueError('local_capture directory must be a canonical absolute path')
    for key in ('max_bytes', 'minimum_free_bytes'):
        if type(value[key]) is not int or not 1 <= value[key] <= 2 ** 63 - 1:
            raise ValueError('local_capture byte limits must be positive integers')
    if value['max_bytes'] + max(value['minimum_free_bytes'], RENDER_OVERHEAD) > 2 ** 63 - 1:
        raise ValueError('local_capture byte limits are too large')
    return dict(value)


def configure_local_capture(objects):
    """Add one explicit disk emptyDir only when the rendered runtime opts in."""
    options = []
    for obj in objects:
        if obj.get('kind') != 'ConfigMap' or 'config.json' not in obj.get('data', {}):
            continue
        config = json.loads(obj['data']['config.json'])
        for item in config.get('claims', []) + config.get('host_sources', []):
            option = validate_local_capture(item.get('local_capture'),
                                            staged_capture=item.get('staged_capture', False))
            if option is not None:
                options.append(option)
    if not options:
        return objects
    directories = {option['directory'] for option in options}
    if len(directories) != 1:
        raise ValueError('One recovery Job must use one local_capture directory')
    directory = directories.pop()
    matches = [obj for obj in objects if obj.get('kind') == 'CronJob'
               and obj.get('metadata', {}).get('name') == 'ha-disaster-recovery']
    if len(matches) != 1:
        raise ValueError('local_capture requires one recovery CronJob')
    pod = matches[0]['spec']['jobTemplate']['spec']['template']['spec']
    containers = [c for c in pod['containers'] if c['name'] == 'backup']
    if len(containers) != 1:
        raise ValueError('local_capture requires one backup container')
    container = containers[0]
    maximum = max(option['max_bytes'] for option in options)
    volume = {'name': 'capture-scratch', 'emptyDir': {'sizeLimit': str(maximum + RENDER_OVERHEAD)}}
    mount = {'name': 'capture-scratch', 'mountPath': directory}
    volumes = pod.setdefault('volumes', [])
    mounts = container.setdefault('volumeMounts', [])
    for existing in volumes:
        if existing['name'] == volume['name'] and existing != volume:
            raise ValueError('local_capture volume conflicts with existing storage')
    for existing in mounts:
        if existing == mount:
            continue
        path, target = PurePosixPath(existing['mountPath']), PurePosixPath(directory)
        if existing['name'] == mount['name'] or path.is_relative_to(target) or target.is_relative_to(path):
            raise ValueError('local_capture mount overlaps existing storage')
    for section, amount in (('requests', maximum), ('limits', maximum + RENDER_OVERHEAD)):
        values = container.setdefault('resources', {}).setdefault(section, {})
        if 'ephemeral-storage' in values and values['ephemeral-storage'] != str(amount):
            raise ValueError('local_capture conflicts with existing ephemeral-storage budget')
        values['ephemeral-storage'] = str(amount)
    if volume not in volumes:
        volumes.append(volume)
    if mount not in mounts:
        mounts.append(mount)
    return objects


def _mounts():
    """Read Linux mount identities, including bind mount backing roots."""
    path = Path('/proc/self/mountinfo')
    if not path.exists():
        if sys.platform == 'linux':
            raise ValueError('Linux mount identities are unavailable')
        return []
    result = []
    for line in path.read_text().splitlines():
        fields = line.split()
        separator = fields.index('-')
        if separator < 6 or len(fields) < separator + 4:
            raise ValueError('Malformed mount identity')
        decode = lambda text: re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), text)
        result.append((fields[2], PurePosixPath(decode(fields[3])),
                       PurePosixPath(decode(fields[4])), fields[separator + 1], fields[0]))
    return result


def _select_mount(path, mounts):
    path = PurePosixPath(str(path))
    candidates = [item for item in mounts if path.is_relative_to(item[2])]
    if not candidates:
        return None
    depth = max(len(item[2].parts) for item in candidates)
    closest = [item for item in candidates if len(item[2].parts) == depth]
    if len(closest) != 1:
        raise ValueError('Ambiguous local capture mount identity')
    return closest[0]


def _backing(path, mounts):
    selected = _select_mount(path, mounts)
    if selected is None:
        return None
    device, root, mountpoint, _kind, _identity = selected
    path = PurePosixPath(str(path))
    return device, root / path.relative_to(mountpoint)


def _overlap(first, second):
    return first.is_relative_to(second) or second.is_relative_to(first)


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino


def _check_directory_identity(path, descriptor):
    current = _open_directory(path)
    try:
        if _identity(os.fstat(current)) != _identity(os.fstat(descriptor)):
            raise OSError('Local capture mount changed')
    finally:
        os.close(current)


def _open_directory(path):
    """Traverse trusted absolute ancestors without following any symlink."""
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(path).parts[1:]:
            parent = os.fstat(descriptor)
            if parent.st_uid not in {0, os.geteuid()} or (
                    parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX):
                raise ValueError('Local capture has an untrusted writable ancestor')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class BoundedWriter:
    def __init__(self, handle, root_fd, maximum, floor, check_deadline):
        self.handle, self.root_fd = handle, root_fd
        self.maximum, self.floor, self.check_deadline = maximum, floor, check_deadline
        self.written = 0

    def write(self, value):
        if self.written + len(value) > self.maximum:
            raise OSError('Local raw capture exceeds its byte limit')
        view = memoryview(value)
        total = 0
        while total < len(view):
            self.check_deadline()
            chunk = view[total:total + CHUNK]
            space = os.fstatvfs(self.root_fd)
            allocation = ((len(chunk) + space.f_frsize - 1) // space.f_frsize) * space.f_frsize
            if space.f_bavail * space.f_frsize < self.floor + allocation:
                raise OSError('Local raw capture would cross its free-space floor')
            count = self.handle.write(chunk)
            if not isinstance(count, int) or not 0 < count <= len(chunk):
                raise OSError('Local raw capture write made no progress')
            total += count
            self.written += count
            space = os.fstatvfs(self.root_fd)
            if space.f_bavail * space.f_frsize < self.floor:
                raise OSError('Local raw capture crossed its free-space floor')
            self.check_deadline()
        return total

    def tell(self):
        return self.written

    def flush(self):
        return self.handle.flush()

    def close(self):
        return self.handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class LocalRaw:
    def __init__(self, root, root_fd, child, child_fd, options, check_deadline, mount_identity):
        self.root, self.root_fd, self.child, self.child_fd = root, root_fd, child, child_fd
        self.options, self.check_deadline = options, check_deadline
        self.mount_identity = mount_identity
        self.file_identity = None

    def check_identity(self):
        _check_directory_identity(self.root, self.root_fd)
        if self.mount_identity is not None and _select_mount(self.root, _mounts()) != self.mount_identity:
            raise OSError('Local capture mount identity changed')
        if _identity(os.stat(self.child, dir_fd=self.root_fd, follow_symlinks=False)) != _identity(os.fstat(self.child_fd)):
            raise OSError('Local capture directory changed')

    def writer(self):
        self.check_identity()
        descriptor = os.open('archive.tar', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=self.child_fd)
        self.file_identity = _identity(os.fstat(descriptor))
        handle = os.fdopen(descriptor, 'wb', buffering=0)
        return BoundedWriter(handle, self.root_fd, self.options['max_bytes'],
                             self.options['minimum_free_bytes'], self.check_deadline)

    def open(self, mode):
        if mode != 'rb':
            raise ValueError('Local raw capture can only be reopened for reading')
        self.check_identity()
        descriptor = os.open('archive.tar', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=self.child_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or _identity(metadata) != self.file_identity:
            os.close(descriptor)
            raise OSError('Local raw capture file changed')
        return os.fdopen(descriptor, 'rb')

    def unlink(self):
        self.check_identity()
        os.unlink('archive.tar', dir_fd=self.child_fd)


@contextmanager
def local_raw_capture(options, source, destination, check_deadline):
    root = Path(options['directory'])
    root_fd = _open_directory(root)
    child_fd = None
    child = None
    child_identity = None
    try:
        metadata = os.fstat(root_fd)
        if metadata.st_uid != os.geteuid():
            raise ValueError('Local capture directory must be owned by the worker')
        private = stat.S_IMODE(metadata.st_mode) == 0o700
        try:
            mounts = _mounts()
        except (ValueError, OSError):
            if not private or sys.platform == 'linux':
                raise ValueError('Cannot verify dedicated local capture mount') from None
            mounts = []
        dedicated = _select_mount(root, mounts)
        if sys.platform == 'linux' and dedicated is None:
            raise ValueError('Cannot verify local capture filesystem')
        if dedicated and (dedicated[3] in NONDISK_FILESYSTEMS or dedicated[3].startswith('fuse')):
            raise ValueError('Local capture cannot use a memory or network filesystem')
        for other in (source.resolve(strict=True), destination.parent.resolve(strict=True)):
            if (_overlap(root, other)
                    or _identity(metadata) in {_identity(p.stat()) for p in (other, *other.parents)}
                    or _identity(other.stat()) in {_identity(p.stat()) for p in (root, *root.parents)}):
                raise ValueError('Local capture overlaps source or destination')
            first, second = _backing(root, mounts), _backing(other, mounts)
            if first and second and first[0] == second[0] and _overlap(first[1], second[1]):
                raise ValueError('Local capture aliases source or destination')
        if not private:
            dedicated = [item for item in mounts if item[2] == PurePosixPath(str(root))
                         and item[1].parent.name == 'kubernetes.io~empty-dir'
                         and item[3] in DISK_FILESYSTEMS]
            if len(dedicated) != 1:
                raise ValueError('Local capture requires a private directory or dedicated disk emptyDir')
            dedicated = dedicated[0]
            _check_directory_identity(root, root_fd)
            if _select_mount(root, _mounts()) != dedicated:
                raise OSError('Local capture mount identity changed')
            os.fchmod(root_fd, 0o700)
        check_deadline()
        space = os.fstatvfs(root_fd)
        if space.f_bavail * space.f_frsize < options['max_bytes'] + options['minimum_free_bytes']:
            raise OSError('Local capture has insufficient initial free space')
        child = '.ha-dr-capture-' + uuid.uuid4().hex
        os.mkdir(child, mode=0o700, dir_fd=root_fd)
        child_identity = _identity(os.stat(child, dir_fd=root_fd, follow_symlinks=False))
        child_fd = os.open(child, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        if _identity(os.fstat(child_fd)) != child_identity:
            raise OSError('Local capture directory changed')
        result = LocalRaw(root, root_fd, child, child_fd, options, check_deadline, dedicated)
        result.check_identity()
        yield result
    finally:
        try:
            if child_fd is not None:
                try:
                    os.unlink('archive.tar', dir_fd=child_fd)
                except FileNotFoundError:
                    pass
                finally:
                    os.close(child_fd)
            if child_identity is not None:
                try:
                    if _identity(os.stat(child, dir_fd=root_fd, follow_symlinks=False)) == child_identity:
                        os.rmdir(child, dir_fd=root_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(root_fd)
