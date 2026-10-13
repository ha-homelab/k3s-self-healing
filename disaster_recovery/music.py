"""Weekly local Music Assistant snapshot, using the stack's verified archive format."""
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re

from .restore import verify_snapshot
from .runner import PROFILE, archive_component, cleanup_completed, timestamp, write_json


def run(source=Path('/data'), root=Path('/backup/snapshots')):
    """Publish only a verified snapshot; keep the previous success on failure."""
    node = os.environ.get('NODE_NAME', '')
    if len(node) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                  for label in node.split('.')):
        raise ValueError('NODE_NAME must identify the source Kubernetes node')
    os.umask(0o077)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + os.urandom(4).hex()
        work = root / ('.incomplete-' + name)
        work.mkdir(mode=0o700)
        write_json(root / 'latest-attempt.json', {'state': 'running', 'snapshot': name})
        try:
            component = archive_component(source, work, 'music-assistant',
                {'type': 'local', 'node': node, 'namespace': 'homeassistant',
                 'claim': 'music-assistant-home-data'},
                ['.cache', 'musicassistant.log', 'musicassistant.log.1'])
            write_json(work / 'manifest.json', {
                'format_version': 1, 'profile': PROFILE, 'completed': True,
                'created_at': timestamp(), 'completed_at': timestamp(),
                'source_revision': os.environ.get('SOURCE_REVISION', 'unrecorded'),
                'components': [component],
                'metadata': {'scope': 'Music Assistant data only; no Plex media files'},
            })
            verify_snapshot(work)
            work.rename(root / name)
            cleanup_completed(root, 2)
            result = {'state': 'complete', 'snapshot': name, 'completed_at': timestamp(),
                      'archive_bytes': component['bytes']}
            write_json(root / 'latest-success.json', result)
            write_json(root / 'latest-attempt.json', result)
            print('Verified local music snapshot: ' + name, flush=True)
        except Exception as error:
            write_json(root / 'latest-attempt.json', {
                'state': 'failed', 'snapshot': name, 'error_type': type(error).__name__})
            raise


if __name__ == '__main__':
    run()
