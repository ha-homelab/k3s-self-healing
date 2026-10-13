"""Prepare a transaction-consistent recorder dump and use HA's native NAS backup."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import io
import os
from pathlib import Path
import re
import shutil
import sys
import tarfile
import time

from dump_format import inspect_dump, known_dump, read_manifest, validate_metadata, verify_dump
from contract import verify_runtime_contract

CONFIG = Path(os.environ.get('HA_CONFIG', '/ha-config'))
WORK = Path(os.environ.get('WORK_DIR', '/work'))
STAGE = CONFIG / 'backups/recorder-export-staging'
OUTPUT = CONFIG / 'recorder_backup'


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as stream:
        stream.write(json.dumps(value, indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def active_export(cleanup=False):
    """Only a validated manifest may identify the active or obsolete exports."""
    if OUTPUT.is_symlink():
        raise ValueError('Regular recorder directory required')
    known = [p for p in OUTPUT.iterdir() if known_dump(p.name)] if OUTPUT.exists() else []
    if not (OUTPUT / 'manifest.json').exists() and not (OUTPUT / 'manifest.json').is_symlink() and not known:
        return None
    meta = read_manifest(OUTPUT)
    active = OUTPUT / meta['file']
    obsolete = [p for p in known if p != active]
    if obsolete:
        if not cleanup:
            raise RuntimeError('Inactive recorder exports must be cleaned before native backup')
        # Never discard another recovery generation until the active pair is proven.
        verify_dump(OUTPUT)
        for path in obsolete:
            if path.is_symlink() or not path.is_file():
                raise ValueError('Unsafe inactive recorder export')
            path.unlink()
        fsync_directory(OUTPUT)
    return active


def option(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'


GIB = 1024**3
LEGACY_SQL_CEILING = 3 * GIB
EXPANDED_SQL_CEILING = 5 * GIB
EXPANDED_FILESYSTEM_MIN = 53 * GIB
API_TIMEOUT_SECONDS = 60
NATIVE_TIMEOUT_SECONDS = 3600
POLL_INTERVAL_SECONDS = 15


def reserve_bytes(total):
    """Keep at least 3 GiB, and 1 GiB above an assumed 5% nodefs eviction threshold."""
    mib = 1024**2
    threshold = (total + 20 * mib - 1) // (20 * mib) * mib
    return max(3 * GIB, threshold + GIB)


def archive_overhead(measured):
    # 50% growth allowance for non-SQL config, plus tar/gzip/streaming overhead.
    # Default-zlib expansion of 5 GiB SQL is under 2 MiB. The retained 128 MiB
    # margin also covers gzip/tar framing and the reviewed securetar v3 overhead.
    return (3 * measured + 1) // 2 + 128 * 1024**2


def reviewed_export_ceiling(total):
    """Larger filesystems may use 5 GiB; live headroom still limits every run."""
    return EXPANDED_SQL_CEILING if total >= EXPANDED_FILESYSTEM_MIN else LEGACY_SQL_CEILING


def compressed_budget(free, reclaimable, total, measured):
    """Reserve new SQL and the one streamed Core/Synology native archive."""
    reserve = reserve_bytes(total)
    limit = min(free - reserve,
                (free + reclaimable - reserve - archive_overhead(measured)) // 2,
                reviewed_export_ceiling(total))
    if limit < 1024:
        raise RuntimeError('Insufficient free space for export and native backup reserve')
    return limit // 1024 * 1024


def reclaimable_export(path):
    if path.is_file() and not path.is_symlink():
        stat = path.stat()
        if stat.st_nlink == 1:
            return min(stat.st_size, stat.st_blocks * 512)
    return 0


def measure_config(exclusions=None):
    """Count native-included gzip bytes without storing/transferring an archive."""
    if exclusions is None:
        verify_runtime_contract()
        from homeassistant.components.backup.const import EXCLUDE_FROM_BACKUP
        exclusions = EXCLUDE_FROM_BACKUP
    active = active_export()
    active_member = 'data/' + active.relative_to(CONFIG).as_posix() if active else None
    started = time.monotonic()

    class Counter(io.RawIOBase):
        size = 0

        def writable(self):
            return True

        def write(self, block):
            if time.monotonic() - started > 600:
                raise TimeoutError('Native configuration size measurement exceeded ten minutes')
            self.size += len(block)
            return len(block)

    def select(member):
        # Match HA's native exclusions, including their exact path boundaries.
        # Nested backup/rollback trees remain covered; symlinks are not followed.
        path = Path(member.name)
        if member.name == active_member or any(
                path.full_match('data/' + pattern) for pattern in exclusions):
            return None
        return member

    sink = Counter()
    with tarfile.open(fileobj=sink, mode='w|gz', compresslevel=1,
                      format=tarfile.PAX_FORMAT, dereference=False) as archive:
        archive.add(CONFIG, arcname='data', filter=select)
    return sink.size


def validate_native_path(ha_config, backup_config):
    """Fail closed when the independently reviewed single-archive path changes."""
    contract = verify_runtime_contract()
    destinations = backup_config['create_backup']['agent_ids']
    if (ha_config.get('version') != contract['homeassistant_version']
            or 'hassio' in ha_config.get('components', [])
            or not destinations
            or not all(agent.startswith('synology_dsm.') for agent in destinations)):
        raise RuntimeError('Capacity budget requires matching reviewed HA Core and Synology-only destinations')
    return destinations


def check_native_capacity(measured):
    disk = shutil.disk_usage(CONFIG)  # Refresh after the potentially long scan.
    sql = active_export()
    ceiling = reviewed_export_ceiling(disk.total)
    if sql is None or sql.is_symlink() or not sql.is_file() or sql.stat().st_size > ceiling:
        raise RuntimeError('Native backup requires a regular, size-limited recorder export')
    sql_bytes = sql.stat().st_size
    required = reserve_bytes(disk.total) + sql_bytes + archive_overhead(measured)
    report = {'free_bytes': disk.free, 'reserve_bytes': reserve_bytes(disk.total),
              'measured_config_bytes': measured, 'required_bytes': required,
              'reviewed_ceiling_bytes': ceiling, 'total_bytes': disk.total,
              'sql_bytes': sql_bytes, 'archive_allowance_bytes': archive_overhead(measured),
              'shortfall_bytes': max(0, required - disk.free)}
    print(json.dumps({'native_capacity': report}), flush=True)
    if disk.free < required:
        diagnostic = dict(report, schema_version=1, code='native_backup_capacity',
                          observed_at=datetime.now(timezone.utc).isoformat(),
                          sql_export_retained=True, **update_receipt_context())
        # A failed Pod retains this bounded, nonsecret message after its emptyDir
        # disappears. Never put raw exceptions, configuration or SQL in it.
        encoded = json.dumps(diagnostic).encode()
        if len(encoded) <= 4096:
            try:
                descriptor = os.open('/dev/termination-log', os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(encoded)
            except OSError:
                pass  # Logging must not change the capacity decision.
        print(json.dumps({'backup_failure': diagnostic}), flush=True)
        raise RuntimeError('Insufficient free space for native backup; verified SQL export retained')
    return report


def prepare():
    import sqlalchemy
    import yaml
    node = yaml.compose((CONFIG / 'secrets.yaml').read_text())
    url = next(value.value for key, value in node.value if key.value == 'recorder_db_url')
    parsed = sqlalchemy.engine.make_url(url)
    if parsed.get_backend_name() != 'mysql' or not re.fullmatch(r'[A-Za-z0-9_]+', parsed.database or ''):
        raise ValueError('Only the reviewed MySQL recorder schema is supported')
    STAGE.mkdir(parents=True, exist_ok=True, mode=0o700)
    WORK.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Kubernetes creates the mounted emptyDir before this process starts.
    WORK.chmod(0o700)
    if not OUTPUT.is_dir():
        raise RuntimeError('Installer must create /ha-config/recorder_backup owned by root first')
    for name in ('recorder.sql.gz.pending', 'recorder.sql.xz.pending'):
        (STAGE / name).unlink(missing_ok=True)
    active = active_export(cleanup=True)
    measured = measure_config()
    disk = shutil.disk_usage(CONFIG)
    reclaimable = reclaimable_export(active) if active else 0
    limit = compressed_budget(disk.free, reclaimable, disk.total, measured)
    print(json.dumps({'export_capacity': {'free_bytes': disk.free,
          'reserve_bytes': reserve_bytes(disk.total), 'measured_config_bytes': measured,
          'compressed_limit_bytes': limit, 'reclaimable_bytes': reclaimable,
          'reviewed_ceiling_bytes': reviewed_export_ceiling(disk.total)}}), flush=True)
    (WORK / 'limit-blocks').write_text(str(limit // 1024))
    settings = {'host': parsed.host, 'port': parsed.port or 3306,
                'user': parsed.username, 'password': parsed.password}
    (WORK / 'client.cnf').write_text('[client]\n' + ''.join(k + '=' + option(v) + '\n' for k, v in settings.items()))
    (WORK / 'database').write_text(parsed.database)
    engine = sqlalchemy.create_engine(url)
    with engine.connect() as connection:
        version = connection.exec_driver_sql('SELECT VERSION()').scalar()
        tables = [dict(row._mapping) for row in connection.exec_driver_sql(
            'SELECT TABLE_NAME AS name, ENGINE AS engine FROM information_schema.tables '
            "WHERE table_schema=DATABASE() AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME")]
        if not tables or any(row['engine'] != 'InnoDB' for row in tables):
            raise ValueError('All recorder tables must be InnoDB for a consistent online export')
    engine.dispose()
    write_json(WORK / 'manifest.json', {'format': 2, 'compression': 'xz', 'database': parsed.database,
        'started_at': datetime.now(timezone.utc).isoformat(), 'server_version': version,
        'tables': tables, 'method': 'mysqldump --single-transaction --quick --hex-blob',
        'schema_included': True, 'users_and_grants_included': False})
    shutil.copyfile(WORK / 'manifest.json', STAGE / 'manifest.json')
    print('Prepared recorder export; credentials remain in the private temporary volume.', flush=True)


def publish():
    source = STAGE / 'recorder.sql.xz.pending'
    if source.is_symlink() or not source.is_file():
        raise ValueError('Regular staged recorder export required')
    before = source.stat()
    inspected = inspect_dump(source, 2)
    total = inspected['uncompressed_bytes']
    if total < 1024:
        raise ValueError('Database export is unexpectedly small')
    digest = inspected['sha256']
    manifest = json.loads((STAGE / 'manifest.json').read_text())
    manifest.update({'completed_at': datetime.now(timezone.utc).isoformat(),
                     'sha256': digest, 'compressed_bytes': inspected['compressed_bytes'],
                     'uncompressed_bytes': total, 'file': f'recorder-{digest}.sql.xz'})
    if manifest.get('format') != 2 or manifest.get('compression') != 'xz':
        raise ValueError('Staging manifest requires XZ format 2')
    after = source.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('Recorder export changed during publication')
    validate_metadata(manifest)
    OUTPUT.mkdir(exist_ok=True, mode=0o700)
    active_export()  # Refuse corrupt/missing prior manifests or unresolved orphans.
    target = OUTPUT / manifest['file']
    with source.open('rb') as stream:
        os.fsync(stream.fileno())
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise ValueError('Unsafe recorder generation')
        with target.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                raise ValueError('Recorder generation checksum mismatch')
            os.fsync(stream.fileno())
        source.unlink()  # An identical immutable generation is already durable.
    else:
        os.replace(source, target)
    fsync_directory(OUTPUT)
    write_json(OUTPUT / 'manifest.json', manifest)
    active_export(cleanup=True)
    (WORK / 'client.cnf').unlink(missing_ok=True)
    print(json.dumps({'export_completed': manifest['completed_at'], 'compressed_bytes': manifest['compressed_bytes'],
                      'tables': len(manifest['tables']), 'sha256': digest}), flush=True)


def update_receipt_context():
    """Correlate an updater-triggered backup without changing weekly receipts."""
    run_id = os.environ.get('HA_UPDATE_RUN_ID')
    job_uid = os.environ.get('HA_UPDATE_JOB_UID')
    context = {}
    if run_id is not None:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', run_id):
            raise RuntimeError('Invalid update backup run identifier')
        context['update_run_id'] = run_id
    if job_uid is not None:
        if run_id is None or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', job_uid):
            raise RuntimeError('Invalid update backup Job UID')
        context['update_job_uid'] = job_uid
    return context


async def native_backup():
    import aiohttp
    update_context = update_receipt_context()
    # Scan before opening the WebSocket: aiohttp processes HA heartbeat pings
    # while receiving, so a long scan inside an idle connection can close it.
    measured = await asyncio.to_thread(measure_config)
    token = Path(os.environ['HA_TOKEN_FILE']).read_text().strip()
    url = os.environ['HA_URL'].rstrip('/')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        async with session.ws_connect(url.replace('http', 'ws', 1) + '/api/websocket') as socket:
            await socket.receive_json(timeout=API_TIMEOUT_SECONDS)
            await socket.send_json({'type': 'auth', 'access_token': token})
            if (await socket.receive_json(timeout=API_TIMEOUT_SECONDS)).get('type') != 'auth_ok':
                raise RuntimeError('Home Assistant authentication failed')
            counter = 0
            async def call(kind, deadline=None):
                nonlocal counter
                counter += 1
                timeout = API_TIMEOUT_SECONDS
                if deadline is not None:
                    timeout = min(timeout, deadline - time.monotonic())
                if timeout <= 0:
                    raise TimeoutError('Native backup did not complete within one hour')
                # Bound the whole request, not each message separately: events
                # or unrelated replies must not keep extending the wait.
                async with asyncio.timeout(timeout):
                    await socket.send_json({'id': counter, 'type': kind})
                    while True:
                        reply = await socket.receive_json(timeout=timeout)
                        if reply.get('id') == counter:
                            if not reply.get('success'):
                                raise RuntimeError('Native backup API rejected ' + kind)
                            return reply.get('result')
            initial = await call('backup/info')
            if initial['state'] != 'idle':
                raise RuntimeError('Another native backup is active; leave the fresh export for a later backup')
            config = (await call('backup/config/info'))['config']
            destinations = validate_native_path(await call('get_config'), config)
            if (await call('backup/info'))['state'] != 'idle':
                raise RuntimeError('Another native backup became active during policy preflight')
            check_native_capacity(measured)
            deadline = time.monotonic() + NATIVE_TIMEOUT_SECONDS
            result = await call('backup/generate_with_automatic_settings', deadline)
            expected_id = result.get('backup_job_id') if isinstance(result, dict) else None
            if not isinstance(expected_id, str) or not expected_id.strip():
                raise RuntimeError('Native backup API did not return a backup identifier')
            print('Native Home Assistant backup requested.', flush=True)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Native backup did not complete within one hour')
                await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))
                info = await call('backup/info', deadline)
                if info['state'] != 'idle':
                    continue
                backup = next((b for b in info['backups'] if b['backup_id'] == expected_id), None)
                if backup is None:
                    raise RuntimeError('Requested native backup finished without its archive')
                if backup.get('failed_agent_ids') or not all(a in backup['agents'] for a in destinations):
                    raise RuntimeError('Native backup was not confirmed at every configured destination')
                receipt = {'backup_id': backup['backup_id'], 'date': backup['date'],
                           'agents': backup['agents'], 'recorder': json.loads((OUTPUT / 'manifest.json').read_text()),
                           **update_context}
                write_json(CONFIG / 'backups/recorder-native-receipt.json', receipt)
                print(json.dumps({'native_backup_completed': receipt['backup_id'], 'date': receipt['date'],
                                  'agents': list(receipt['agents'])}), flush=True)
                return


if __name__ == '__main__':
    os.umask(0o077)
    os.umask(0o077)
    if sys.argv[1] == 'prepare':
        prepare()
    elif sys.argv[1] == 'finish':
        publish()
        if os.environ.get('CREATE_NATIVE_BACKUP', 'true') == 'true':
            asyncio.run(native_backup())
    else:
        raise SystemExit('Usage: runner.py prepare|finish')
