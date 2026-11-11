"""Recorder storage backpressure in the existing updater; no database DML/DDL.

A durable pause suppresses raw event/state recording, not all database writes.
There is deliberately no automatic recorder.enable or destructive reclamation.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.request import Request
import uuid

from model import ACTIVE
from runtime import urlopen

TRIGGER = 3_500_000_000
PAUSE = 4_500_000_000
BUDGET = 5_000_000_000
INTERVAL = 300
DAY = 86400
TABLES = {
    'states': 'last_updated_ts', 'events': 'time_fired_ts',
    'statistics_short_term': 'start_ts', 'statistics_runs': 'start',
}
NOTIFICATION = 'recorder_storage_budget'


def recorder_url(config_dir):
    """Resolve only the reviewed Recorder include/secret, without constructing YAML."""
    import yaml
    directory = Path(config_dir).resolve()

    def node(path):
        resolved = path.resolve()
        if not resolved.is_relative_to(directory) or not resolved.is_file():
            raise ValueError('Recorder configuration escaped its mounted directory')
        return yaml.compose(resolved.read_text())

    def field(mapping, name):
        if not isinstance(mapping, yaml.MappingNode):
            raise ValueError('Expected a YAML mapping')
        values = [v for k, v in mapping.value if k.value == name]
        if len(values) != 1:
            raise ValueError('Required configuration field is missing or duplicated')
        return values[0]

    recorder = field(node(directory / 'configuration.yaml'), 'recorder')
    if recorder.tag == '!include':
        recorder = node(directory / recorder.value)
    value = field(recorder, 'db_url')
    if value.tag != '!secret' or value.value != 'recorder_db_url':
        raise ValueError('Expected the reviewed Recorder secret reference')
    secret = field(node(directory / 'secrets.yaml'), 'recorder_db_url')
    if not isinstance(secret, yaml.ScalarNode) or secret.tag != 'tag:yaml.org,2002:str':
        raise ValueError('Recorder database URL must be a string secret')
    return secret.value


def save(path, data):
    """Commit dispatch intent before any HTTP call; caller holds the process lock."""
    handle, name = tempfile.mkstemp(prefix='.recorder-budget-', dir=path.parent)
    try:
        with os.fdopen(handle, 'w') as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def collect(settings):
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool
    expected = settings['schema']
    if not re.fullmatch(r'[A-Za-z0-9_]+', expected):
        raise ValueError('Invalid exact HA schema')
    engine = create_engine(recorder_url(settings.get('config_dir', '/ha-config')),
                           poolclass=NullPool, connect_args={'connect_timeout': 10, 'read_timeout': 10, 'write_timeout': 10})
    try:
        if engine.dialect.name != 'mysql' or engine.url.database != expected:
            raise ValueError('Expected the exact MySQL HA schema')
        with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as db:
            db.execute(text('SET SESSION MAX_EXECUTION_TIME=5000'))
            db.execute(text("SET SESSION time_zone='+00:00'"))
            db.execute(text('SET SESSION information_schema_stats_expiry=0'))
            if db.execute(text('SELECT DATABASE()')).scalar() != expected:
                raise ValueError('Connected database identity mismatch')
            rows = list(db.execute(text('SELECT TABLE_NAME,DATA_LENGTH,INDEX_LENGTH,DATA_FREE '
                'FROM information_schema.TABLES WHERE TABLE_SCHEMA=:schema'), {'schema': expected}).mappings())
            names = {r['TABLE_NAME'] for r in rows}
            if 'kine' in names or not (set(TABLES) | {'statistics', 'statistics_meta', 'state_attributes'}) <= names:
                raise ValueError('Database does not match an isolated HA Recorder schema')
            now = float(db.execute(text('SELECT UNIX_TIMESTAMP()')).scalar())
            if abs(now - time.time()) > 30:
                raise ValueError('Database/controller clock disagreement')
            oldest = {}
            for table, column in TABLES.items():
                # Refuse a possibly unbounded MIN scan without an appropriate index.
                indices = list(db.execute(text(f'SHOW INDEX FROM `{table}`')).mappings())
                choices = [r['Key_name'] for r in indices if int(r['Seq_in_index']) == 1 and r['Column_name'] == column]
                if not choices or not re.fullmatch(r'[A-Za-z0-9_]+', choices[0]):
                    raise ValueError('Required timestamp-leading index is absent')
                expression = f'UNIX_TIMESTAMP(`{column}`)' if column == 'start' else f'`{column}`'
                sql = f'SELECT {expression} FROM `{table}` FORCE INDEX (`{choices[0]}`) WHERE `{column}` IS NOT NULL ORDER BY `{column}` LIMIT 1'
                value = db.execute(text(sql)).scalar()
                oldest[table] = float(value) if value is not None else None
            latest = db.execute(text('SELECT last_updated_ts FROM states ORDER BY state_id DESC LIMIT 1')).scalar()
            result = {
                'measured_at': now,
                'data_index_bytes': sum(int(r['DATA_LENGTH'] or 0) + int(r['INDEX_LENGTH'] or 0) for r in rows),
                # Do not treat this sum as distinct free extents in shared tablespaces.
                'data_free_row_sum_bytes': sum(int(r['DATA_FREE'] or 0) for r in rows),
                'oldest': oldest,
                'latest_state_age_seconds': now - float(latest) if latest is not None else None,
                'physical_bytes': None,
                'physical_status': 'unknown',
            }
            result.update(physical_usage(db.connection.driver_connection, expected))
            return result
    finally:
        engine.dispose()



def physical_usage(connection, schema):
    """Read only the DBA-defined schema probe; drain all CALL result sets."""
    if not re.fullmatch(r'[A-Za-z0-9_]+', schema):
        raise ValueError('Invalid exact Recorder schema')
    cursor = connection.cursor()
    try:
        cursor.execute(f'CALL `{schema}`.recorder_storage_probe()')
        columns = [column[0] for column in cursor.description or []]
        rows = cursor.fetchall()
        while cursor.nextset():
            cursor.fetchall()
    finally:
        cursor.close()
    if (columns != ['schema_name', 'allocated_bytes', 'file_bytes'] or len(rows) != 1
            or rows[0][0] != schema or any(value is None or int(value) < 0 for value in rows[0][1:])):
        raise ValueError('Storage probe did not prove the exact HA schema')
    return {'physical_bytes': int(rows[0][1]), 'file_bytes': int(rows[0][2]),
            'physical_status': 'measured_schema_probe'}

def measured_bytes(snapshot):
    """Use the larger known allocation; never equate DATA_FREE with reclaimed bytes."""
    return max(snapshot['data_index_bytes'], snapshot.get('physical_bytes') or 0)


class BudgetGuard:
    def __init__(self, settings, runtime, updater_store, path='/state/recorder-budget.json', collector=collect):
        self.settings = settings
        self.runtime = runtime
        self.updater_store = updater_store
        self.path = Path(path)
        self.collector = collector
        schema = settings.get('schema')
        if not isinstance(schema, str) or not re.fullmatch(r'[A-Za-z0-9_]+', schema):
            raise ValueError('Recorder guard requires the exact HA schema')
        self.identity = hashlib.sha256((schema + '\n' + runtime.ha_url).encode()).hexdigest()

    def service(self, domain, name, payload):
        request = Request(self.runtime.ha_url + '/api/services/' + domain + '/' + name,
                          data=json.dumps(payload).encode(), method='POST',
                          headers={'Authorization': 'Bearer ' + self.runtime.ha_token,
                                   'Content-Type': 'application/json'})
        with urlopen(request, timeout=15) as response:
            response.read(1024 * 1024)

    def persist(self, state):
        save(self.path, state)

    def assert_update_idle(self):
        """Caller holds updater Store.lock across this check and approval."""
        try:
            state = json.loads(self.path.read_text()) if self.path.exists() else {}
            if (state.get('pending') or state.get('identity', self.identity) != self.identity
                    or not isinstance(state, dict)):
                raise ValueError('Unresolved Recorder maintenance state')
        except Exception as error:
            raise RuntimeError('Recorder maintenance must be resolved before a Core update') from error

    def notify(self, state, message):
        # Stable notification ID replaces prior text instead of creating spam.
        try:
            self.service('persistent_notification', 'create', {
                'notification_id': NOTIFICATION,
                'title': 'Recorder storage needs attention', 'message': message})
        except Exception as error:
            state['notification_error'] = type(error).__name__

    def pause(self, state, now):
        if not state.get('pause'):
            state['pause'] = {'id': uuid.uuid4().hex, 'requested_at': now,
                              'phase': 'dispatching', 'reason': 'storage_at_or_above_4_5_GB'}
            self.persist(state)
        try:
            self.service('recorder', 'disable', {})
            state['pause']['phase'] = 'accepted'
            state['pause']['last_confirmed_at'] = now
        except Exception as error:
            state['pause']['phase'] = 'outcome_unknown'
            state['pause']['error_type'] = type(error).__name__
        self.persist(state)
        self.notify(state, 'Raw event/state recording is paused by the storage guard, or its pause '
                    'request is awaiting confirmation. The pause is retried every five minutes and '
                    'survives updater restarts. Statistics and other database activity can continue; '
                    'this is not a hard storage quota. Inspect /state/recorder-budget.json, repair '
                    'cleanup, verify storage below 3.5 GB, and follow the recovery runbook before resuming.')

    def run_once(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.path.with_suffix('.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                state = json.loads(self.path.read_text()) if self.path.exists() else {'schema_version': 1}
                if not isinstance(state, dict) or state.get('identity', self.identity) != self.identity:
                    raise ValueError('Recorder guard state belongs to another target')
            except Exception:
                self.notify({}, 'Recorder storage protection cannot read or trust its durable receipt. '
                            'The existing file was preserved. Inspect /state/recorder-budget.json; '
                            'no cleanup or automatic resumption is permitted until it is resolved.')
                return
            state['identity'] = self.identity
            # Reapply a durable latch even if SQL is temporarily unavailable or HA restarted.
            if state.get('pause'):
                self.pause(state, time.time())
            try:
                snapshot = self.collector(self.settings)
                now = snapshot['measured_at']
                if abs(time.time() - now) > 60 or set(snapshot['oldest']) != set(TABLES):
                    raise ValueError('Stale or incomplete Recorder measurement')
            except Exception as error:
                state['measurement_error'] = type(error).__name__
                self.notify(state, 'Recorder storage could not be measured. The budget is unverified. '
                            'An existing raw-recording pause remains latched; no cleanup is queued.')
                self.persist(state)
                return
            state.pop('measurement_error', None)
            state['snapshot'] = snapshot
            state['hard_quota_enforced'] = False
            size = measured_bytes(snapshot)
            if size >= PAUSE and not state.get('pause'):
                self.pause(state, now)
            if state.get('pause'):
                state['action'] = 'paused_manual_recovery_required'
                self.persist(state)
                return
            pending = state.get('pending')
            if pending:
                if all(value is None or value >= pending['cutoff'] for value in snapshot['oldest'].values()):
                    state['last_completed'] = {**state.pop('pending'), 'completed_at': now}
                    state['action'] = 'purge_completed'
                else:
                    state['action'] = 'purge_pending_or_failed'
                    if pending.get('last_oldest') != snapshot['oldest']:
                        pending['last_progress_at'] = now
                        pending['last_oldest'] = snapshot['oldest']
                    if now - pending['last_progress_at'] >= 1800:
                        self.notify(state, 'Recorder purge has no observed cutoff progress for 30 minutes. '
                                    'The request will not be repeated automatically; inspect HA logs. '
                                    'A successful service response only means the purge was queued.')
                self.persist(state)
                return
            if size >= TRIGGER:
                self.notify(state, 'Recorder allocation is at least 3.5 GB. Cleanup is being considered; '
                            'raw recording will pause at 4.5 GB. The 5 GB target is an operating budget, '
                            'not a physical quota. Purging does not necessarily shrink InnoDB files.')
            has_old_rows = any(value is not None and value < now - DAY for value in snapshot['oldest'].values())
            interval = 900 if size >= TRIGGER else 3600
            age = snapshot.get('latest_state_age_seconds')
            if (self.settings.get('purge_qualified') is not True or not has_old_rows
                    or age is None or not -30 <= age <= 120
                    or now - state.get('last_dispatch_at', 0) < interval
                    or self.updater_store.read()['stage'] in ACTIVE | {'recovery_required'}):
                state['action'] = 'watching'
                self.persist(state)
                return
            # Existing backup/HACS checks are reused. No backup/purge failure is retried blindly.
            try:
                self.runtime.maintenance_idle()
                if self.runtime.ha('/api/config').get('state') != 'RUNNING':
                    raise RuntimeError('HA is not running')
                if abs(time.time() - now) > 60:
                    raise RuntimeError('Preflight measurement expired')
            except Exception:
                state['action'] = 'maintenance_busy_or_unavailable'
                self.persist(state)
                return
            # The same lock protects Store.approve. Once this intent is durable,
            # approval refuses it until fixed-cutoff completion is confirmed.
            with self.updater_store.lock:
                if self.updater_store.read()['stage'] in ACTIVE | {'recovery_required'}:
                    state['action'] = 'core_update_started_during_preflight'
                    self.persist(state)
                    return
                state['pending'] = {'request_id': uuid.uuid4().hex, 'requested_at': now,
                                    'cutoff': now - DAY, 'phase': 'dispatching',
                                    'last_progress_at': now, 'last_oldest': snapshot['oldest']}
                state['last_dispatch_at'] = now
                state['action'] = 'purge_requested'
                self.persist(state)
            try:
                self.service('recorder', 'purge', {'keep_days': 1, 'repack': False, 'apply_filter': False})
                state['pending']['phase'] = 'accepted'
            except Exception as error:
                state['pending']['phase'] = 'outcome_unknown'
                state['pending']['error_type'] = type(error).__name__
            self.persist(state)


async def supervise_budget(guard):
    """Independent bounded polling must not delay version checks or approval HTTP."""
    while True:
        try:
            await asyncio.to_thread(guard.run_once)
        except Exception as error:
            print(json.dumps({'event': 'recorder_budget_failed', 'error_type': type(error).__name__}), flush=True)
        await asyncio.sleep(INTERVAL)
