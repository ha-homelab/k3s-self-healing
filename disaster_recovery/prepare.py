"""Prepare selected snapshot components in a NEW offline workspace; never apply them."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

if __package__:
    from . import restore
else:
    import restore


class PreparationError(ValueError):
    """Safe preparation failure; never includes file contents."""


@contextmanager
def _open_at(root_fd, relative, directory=False):
    parts = PurePosixPath(relative).parts
    descriptor = os.dup(root_fd)
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1 or directory:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _metadata(record):
    mode = record.get('original_mode')
    if not isinstance(mode, str) or not re.fullmatch(r'[0-7]{4}', mode):
        raise PreparationError('Invalid original mode')
    mode = int(mode, 8)
    forbidden = 0o4000 if record.get('type') == 'directory' else 0o7000
    if mode & forbidden:
        raise PreparationError('Special permission bits require individual manual review')
    for field in ('original_uid', 'original_gid'):
        if type(record.get(field)) is not int or not 0 <= record[field] < 2**32 - 1:
            raise PreparationError('Invalid original numeric ownership')
    mtime = record.get('original_mtime')
    if type(mtime) not in (int, float) or not math.isfinite(mtime) or abs(mtime) >= 2**63 / 10**9:
        raise PreparationError('Invalid original timestamp')
    return mode, record['original_uid'], record['original_gid'], int(mtime * 10**9)


def _plan(component, data_fd):
    records = component['members']
    by_path = {record['path']: record for record in records}
    if '.' not in by_path or by_path['.']['type'] != 'directory':
        raise PreparationError('Selected component lacks source-root directory metadata')
    plan, links = [], []
    groups = Counter(record.get('resolved_target', record['path']) for record in records
                     if record['type'] in {'file', 'hardlink'} and 'skip_reason' not in record)
    with _open_at(data_fd, component['name'], directory=True) as component_fd:
        for record in records:
            path, kind = record['path'], record['type']
            if restore._relative(path, allow_root=kind == 'directory') != path:
                raise PreparationError('Selected component has a noncanonical member path')
            metadata = _metadata(record)
            for ancestor in PurePosixPath(path).parents:
                if str(ancestor) not in by_path or by_path[str(ancestor)]['type'] != 'directory':
                    raise PreparationError('Selected component lacks ancestor directory metadata')
            if kind == 'symlink' or 'skip_reason' in record:
                links.append({'path': path, 'type': kind, 'link_target': record.get('link_target'),
                              'reason': record.get('skip_reason', 'safe_link_ownership_left_unchanged')})
                continue
            with _open_at(component_fd, path, directory=kind == 'directory') as descriptor:
                current = os.fstat(descriptor)
                if not (stat.S_ISDIR(current.st_mode) if kind == 'directory' else stat.S_ISREG(current.st_mode)):
                    raise PreparationError('Extracted member has an unexpected type')
                target = record.get('resolved_target', path)
                if kind != 'directory' and current.st_nlink != groups[target]:
                    raise PreparationError('Extracted file has an unaccounted hard link')
                plan.append({'path': path, 'kind': kind, 'metadata': metadata,
                             'identity': (current.st_dev, current.st_ino), 'links': current.st_nlink,
                             'target': target})
    identities = {entry['path']: entry for entry in plan}
    for entry in plan:
        if entry['kind'] == 'hardlink':
            target = identities.get(entry['target'])
            if not target or target['kind'] != 'file' or target['identity'] != entry['identity'] or target['metadata'] != entry['metadata']:
                raise PreparationError('Hard-link identity or original metadata is inconsistent')
    return plan, links


def _apply(plan, component_fd):
    files = [entry for entry in plan if entry['kind'] == 'file']
    directories = sorted((entry for entry in plan if entry['kind'] == 'directory'),
                         key=lambda entry: len(PurePosixPath(entry['path']).parts), reverse=True)
    for entry in files + directories:
        with _open_at(component_fd, entry['path'], directory=entry['kind'] == 'directory') as descriptor:
            current = os.fstat(descriptor)
            if ((current.st_dev, current.st_ino) != entry['identity']
                    or (entry['kind'] == 'file' and current.st_nlink != entry['links'])):
                raise PreparationError('Extracted member changed after preparation preflight')
            mode, uid, gid, mtime = entry['metadata']
            os.fchown(descriptor, uid, gid)
            os.utime(descriptor, ns=(mtime, mtime))
            os.fchmod(descriptor, mode)
    return {'regular_files': len(files), 'directories': len(directories),
            'hard_links': sum(entry['kind'] == 'hardlink' for entry in plan)}


def _receipt(root_fd, value):
    temporary = '.preparation.json.new'
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
    with os.fdopen(descriptor, 'w') as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(value, output, indent=2, ensure_ascii=True)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, 'preparation.json', src_dir_fd=root_fd, dst_dir_fd=root_fd)


def _check_workspace_owner(root_fd):
    if os.fstat(root_fd).st_uid != 0:
        raise PreparationError('Workspace must be owned by root on this filesystem')


def _check_trusted_directory(descriptor):
    value = os.fstat(descriptor)
    if value.st_uid != 0 or (value.st_mode & 0o022 and not value.st_mode & stat.S_ISVTX):
        raise PreparationError('Workspace ancestors must be root-owned and protected from non-root replacement')


@contextmanager
def _trusted_parent(path):
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _check_trusted_directory(descriptor)
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            _check_trusted_directory(descriptor)
        yield descriptor
    finally:
        os.close(descriptor)


def prepare_snapshot(snapshot, workspace, components):
    """Extract every archive safely, then prepare only explicitly selected components."""
    if os.geteuid() != 0:
        raise PreparationError('Root is required to restore original numeric ownership offline')
    selected = list(components)
    if not selected or len(set(selected)) != len(selected):
        raise PreparationError('Select one or more distinct components explicitly')
    for name in selected:
        restore._basename(name, component=True)
    snapshot = Path(snapshot).resolve()
    requested = Path(workspace)
    if requested.exists() or requested.is_symlink():
        raise PreparationError('Preparation requires a new, nonexistent workspace')
    workspace = requested.parent.resolve() / requested.name
    if workspace == snapshot or snapshot in workspace.parents or not workspace.parent.is_dir():
        raise PreparationError('Workspace must have an existing parent outside the snapshot')
    with _trusted_parent(workspace.parent) as parent_fd:
        os.mkdir(workspace.name, mode=0o700, dir_fd=parent_fd)
        root_fd = os.open(workspace.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    receipt = {'format_version': 1, 'status': 'preparing', 'completed': False,
               'created_at': datetime.now(timezone.utc).isoformat(), 'host_apply_performed': False,
               'selected_components': selected, 'components': [], 'links_not_prepared': [],
               'directory_special_modes': [],
               'directory_mode_policy': 'Preserve setgid (group inheritance) and sticky (restricted deletion); refuse setuid and all special file bits.'}
    try:
        os.fchmod(root_fd, 0o700)
        _check_workspace_owner(root_fd)
        _receipt(root_fd, receipt)
        report = restore.verify_snapshot(snapshot, extract_to=workspace / 'data')
        available = {component['name']: component for component in report['components']}
        if any(name not in available for name in selected):
            raise PreparationError('A selected component is absent from the snapshot')
        with _open_at(root_fd, 'data', directory=True) as data_fd:
            plans = {}
            # Validate every selected record before applying any original metadata.
            for name in selected:
                plans[name], links = _plan(available[name], data_fd)
                receipt['links_not_prepared'].extend({'component': name, **link} for link in links)
                receipt['directory_special_modes'].extend(
                    {'component': name, 'path': entry['path'], 'mode': format(entry['metadata'][0], '04o')}
                    for entry in plans[name] if entry['kind'] == 'directory' and entry['metadata'][0] & 0o3000)
            for name in selected:
                with _open_at(data_fd, name, directory=True) as component_fd:
                    counts = _apply(plans[name], component_fd)
                receipt['components'].append({'name': name, 'sha256': available[name]['sha256'], **counts})
        receipt.update(status='prepared', completed=True, completed_at=datetime.now(timezone.utc).isoformat())
        _receipt(root_fd, receipt)
        return receipt
    except Exception as error:
        receipt.update(status='failed', completed=False, error_type=type(error).__name__)
        try:
            _receipt(root_fd, receipt)
        except OSError:
            pass
        raise
    finally:
        os.close(root_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--component', action='append', required=True, dest='components')
    args = parser.parse_args(argv)
    try:
        result = prepare_snapshot(args.snapshot, args.workspace, args.components)
    except (PreparationError, restore.RestoreError, OSError, OverflowError):
        print('Offline preparation failed; inspect the private receipt if a new workspace was created.', file=sys.stderr)
        return 1
    print('Offline preparation complete: %d components; %d links need review; no host configuration applied.' % (
        len(result['components']), len(result['links_not_prepared'])))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
