#!/usr/local/bin/python3
"""Guard recorder orphan DELETEs against references newer than its read view.

Qualified on MySQL 26.10.0-cloud with intact FKs and two-connection races.
Only the exact HA 2026.11.0/2026.11.1 queries.py source is accepted.
"""
import ast
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile

TARGET = Path('/usr/src/homeassistant/homeassistant/components/recorder/queries.py')
SOURCE_SHA256 = '4267c337433edc3f984cbb485b408e83e3ecd8e24d6a078d17330b937cd0ff6c'

EVENT_BEFORE = '''            delete(EventData)
            .where(EventData.data_id.in_(data_ids))
            .execution_options(synchronize_session=False)'''
EVENT_AFTER = '''            delete(EventData)
            .where(EventData.data_id.in_(data_ids))
            .where(
                ~select(Events.event_id)
                .where(Events.data_id == EventData.data_id)
                .correlate(EventData)
                .exists()
            )
            .execution_options(synchronize_session=False)'''
ATTR_BEFORE = '''            delete(StateAttributes)
            .where(StateAttributes.attributes_id.in_(attributes_ids))
            .execution_options(synchronize_session=False)'''
ATTR_AFTER = '''            delete(StateAttributes)
            .where(StateAttributes.attributes_id.in_(attributes_ids))
            .where(
                ~select(States.state_id)
                .where(States.attributes_id == StateAttributes.attributes_id)
                .correlate(StateAttributes)
                .exists()
            )
            .execution_options(synchronize_session=False)'''


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def patched_source(source):
    if EVENT_AFTER in source and ATTR_AFTER in source:
        original = source.replace(EVENT_AFTER, EVENT_BEFORE, 1).replace(ATTR_AFTER, ATTR_BEFORE, 1)
        if digest(original) == SOURCE_SHA256:
            return source, 'already-patched'
    if digest(source) != SOURCE_SHA256:
        return source, 'unsupported'
    if source.count(EVENT_BEFORE) != 1 or source.count(ATTR_BEFORE) != 1:
        return source, 'unsupported'
    result = source.replace(EVENT_BEFORE, EVENT_AFTER, 1).replace(ATTR_BEFORE, ATTR_AFTER, 1)
    ast.parse(result)
    return result, 'patched'


def patch_file(path):
    if path.is_symlink() or not path.is_file():
        return 'unsupported'
    source = path.read_text()
    result, status = patched_source(source)
    if status != 'patched':
        return status
    mode = stat.S_IMODE(path.stat().st_mode)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(result)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.chmod(mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return status


if __name__ == '__main__':
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else TARGET
    status = patch_file(target)
    print(f'recorder-orphan-guard: {status}', flush=True)
    if status == 'unsupported':
        print('WARNING: recorder source is not the qualified version; left intact. Review this guard before relying on automatic purge.', file=sys.stderr, flush=True)
