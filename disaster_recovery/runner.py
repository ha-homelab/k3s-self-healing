"""Create a verified HA recovery set directly on the NAS; never restore production."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
import traceback

from .archive import create_archive, SourceChangedError
from .kubernetes import Kubernetes

PROFILE = 'ha-homelab-dr-v1'
SNAPSHOT_NAME = re.compile(r'\d{8}T\d{6}Z-[a-f0-9]{8}\Z')


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    data = (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def mapped_source(root, relative):
    relative = Path(relative)
    if not relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Source path must be absolute and cannot contain parent traversal')
    path = root / relative.relative_to('/')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Source escapes its read-only mount')
    return path


def resolve_claim(api, item, config, host, nfs):
    pvc = api.get(f"/api/v1/namespaces/{item['namespace']}/persistentvolumeclaims/{item['claim']}")
    if pvc['status']['phase'] != 'Bound':
        raise RuntimeError('Required application PVC is not bound')
    pv = api.get('/api/v1/persistentvolumes/' + pvc['spec']['volumeName'])
    if pv['spec']['claimRef']['uid'] != pvc['metadata']['uid']:
        raise RuntimeError('PVC/PV identity does not match')
    spec = pv['spec']
    if 'nfs' in spec:
        source = spec['nfs']
        if source['server'] != config['nas_server']:
            raise RuntimeError('Unreviewed NFS server; update the backup source mount first')
        suffix = Path(source['path']).relative_to(config['nas_export'])
        path = mapped_source(nfs, '/' + str(suffix))
        location = {'type': 'nfs', **source}
    else:
        source = spec.get('local', spec.get('hostPath'))
        if not source:
            raise RuntimeError('Unsupported PVC volume type')
        terms = spec.get('nodeAffinity', {}).get('required', {}).get('nodeSelectorTerms', [])
        # A source must be unambiguously pinned to the backup node.
        if not terms or any(not any(
            rule.get('key') == 'kubernetes.io/hostname' and rule.get('operator') == 'In'
            and rule.get('values') == [config['node']]
            for rule in term.get('matchExpressions', [])) for term in terms):
            raise RuntimeError('Local PVC is not exclusively on the backup node')
        path = mapped_source(host, source['path'])
        location = {'type': 'local', 'node': config['node'], **source}
    return path, {'namespace': item['namespace'], 'claim': item['claim'],
                  'claim_uid': pvc['metadata']['uid'], 'pv': pv['metadata']['name'],
                  'pv_uid': pv['metadata']['uid'], 'location': location}


def portable_object(value):
    value = json.loads(json.dumps(value))
    value.pop('status', None)
    metadata = value.get('metadata', {})
    for field in ('uid', 'resourceVersion', 'generation', 'creationTimestamp',
                  'deletionTimestamp', 'managedFields', 'selfLink'):
        metadata.pop(field, None)
    # Owner UIDs are cluster-specific. Controllers recreate children themselves.
    metadata.pop('ownerReferences', None)
    metadata.get('annotations', {}).pop('kubectl.kubernetes.io/last-applied-configuration', None)
    return value


def export_kubernetes(api, folder, config):
    folder.mkdir(mode=0o700)
    inventory = {}
    collections = list(config['api_collections'])
    crds = api.list('/apis/apiextensions.k8s.io/v1/customresourcedefinitions')
    for crd in crds['items']:
        spec = crd['spec']
        version = next(v['name'] for v in spec['versions'] if v.get('storage') and v.get('served'))
        collections.append('/apis/' + spec['group'] + '/' + version + '/' + spec['names']['plural'])
    for number, endpoint in enumerate(dict.fromkeys(collections)):
        result = crds if endpoint.endswith('/customresourcedefinitions') else api.list(endpoint)
        for item in result['items']:
            item.setdefault('kind', result['kind'].removesuffix('List'))
            item.setdefault('apiVersion', result['apiVersion'])
        filename = f'{number:03d}-' + endpoint.strip('/').replace('/', '_') + '.json'
        write_json(folder / filename, result)
        inventory[endpoint] = {'file': filename, 'count': len(result['items'])}
    write_json(folder / 'inventory.json', inventory)
    write_json(folder / 'version.json', api.get('/version'))
    # Portable exports are a fallback for a fresh cluster, not an instruction to
    # apply every saved Pod/Job or overwrite surviving NFS volume contents.
    portable = []
    portable_kinds = {'Deployment', 'StatefulSet', 'CronJob', 'ConfigMap', 'Secret',
                      'Service', 'ServiceAccount', 'Role', 'RoleBinding',
                      'NetworkPolicy', 'Ingress', 'PersistentVolumeClaim'}
    for entry in inventory.values():
        for item in json.loads((folder / entry['file']).read_text())['items']:
            if item.get('kind') not in portable_kinds:
                continue
            if item.get('metadata', {}).get('namespace') in config['namespaces']:
                clean = portable_object(item)
                if clean['kind'] in {'Deployment', 'StatefulSet'}:
                    clean.setdefault('metadata', {}).setdefault('annotations', {})[
                        'ha-homelab.io/restore-replicas'] = str(clean['spec'].get('replicas', 1))
                    clean['spec']['replicas'] = 0
                if clean['kind'] == 'CronJob':
                    clean['spec']['suspend'] = True
                portable.append(clean)
    write_json(folder / 'application-objects-stopped.json',
               {'apiVersion': 'v1', 'kind': 'List', 'items': portable})
    return inventory


def archive_component(source, work, name, metadata, excludes=(), generated_image_caches=()):
    destination = work / (name + '.tar.gz')
    try:
        # create_archive already retries three times under one shared deadline.
        # Repeating that operation here multiplied uploads and reset the budget.
        result = create_archive(source, destination, excludes=list(excludes), timeout_seconds=900,
                                generated_image_caches=list(generated_image_caches))
        return {'name': name, 'source': metadata, **result}
    except SourceChangedError as error:
        diagnostic = work / (name + '-source-change.private.json')
        write_json(diagnostic, {'component': name, 'error': str(error),
                   'cause': str(error.__cause__) if error.__cause__ else None})
        raise


def cleanup_completed(root, keep):
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 2:
        raise ValueError('Retention must preserve at least two completed snapshots')
    candidates = []
    for path in root.iterdir():
        if not SNAPSHOT_NAME.fullmatch(path.name) or not path.is_dir() or path.is_symlink():
            continue
        try:
            manifest = json.loads((path / 'manifest.json').read_text())
        except (OSError, ValueError):
            continue
        if isinstance(manifest, dict) and manifest.get('profile') == PROFILE and manifest.get('completed') is True:
            candidates.append(path)
    for path in sorted(candidates, reverse=True)[keep:]:
        shutil.rmtree(path)


def run(config, root, *, host=Path('/host'), nfs=Path('/nfs-source'), api=None):
    from .restore import verify_snapshot

    os.umask(0o077)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError('Backup root cannot be a symlink')
    root.chmod(0o700)
    with (root / '.run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        free = shutil.disk_usage(root).free
        if free < config['minimum_free_bytes']:
            raise RuntimeError('NAS free space is below the configured floor')
        api = api or Kubernetes()
        if os.environ.get('NODE_NAME', config['node']) != config['node']:
            raise RuntimeError('Backup is running on an unexpected node')
        node = api.get('/api/v1/nodes/' + config['node'])
        machine_id = (host / 'etc/machine-id').read_text().strip()
        if machine_id != node['status']['nodeInfo']['machineID']:
            raise RuntimeError('Mounted host does not match the Kubernetes node')
        name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + os.urandom(4).hex()
        work = root / ('.incomplete-' + name)
        work.mkdir(mode=0o700)
        published = None
        manifest = {'format_version': 1, 'profile': PROFILE, 'completed': False,
                    'created_at': timestamp(), 'source_revision': os.environ.get('SOURCE_REVISION', 'unrecorded'),
                    'components': [], 'metadata': {'node': config['node'], 'node_info': node['status']['nodeInfo']}}
        write_json(root / 'latest-attempt.json', {'state': 'running', 'snapshot': name, 'started_at': timestamp()})
        try:
            print('Starting private recovery snapshot', flush=True)
            state_root = work / '.kubernetes'
            collections = export_kubernetes(api, state_root, config)
            pods = api.list('/api/v1/pods')['items']
            app_pods = [p for p in pods if p['metadata']['namespace'] in config['namespaces']]
            manifest['metadata']['images'] = [
                {'namespace': p['metadata']['namespace'], 'pod': p['metadata']['name'],
                 'node': p['spec'].get('nodeName'), 'containers': p.get('status', {}).get('containerStatuses', [])}
                for p in app_pods]
            manifest['metadata']['collections'] = collections
            resolved = [(item, *resolve_claim(api, item, config, host, nfs)) for item in config['claims']]
            brokers = [p for p in app_pods if p['metadata']['namespace'] == 'homeassistant'
                       and any(c['name'] == 'mosquitto' for c in p['spec']['containers'])
                       and p.get('status', {}).get('phase') == 'Running'
                       and not p['metadata'].get('deletionTimestamp')]
            if len(brokers) != 1:
                raise RuntimeError('Expected exactly one live Mosquitto broker')
            mqtt_path = next(path for item, path, _ in resolved if item['name'] == 'mosquitto') / 'mosquitto.db'
            previous = mqtt_path.stat()
            manifest['metadata']['mqtt_checkpoint'] = api.checkpoint_mosquitto(brokers[0])
            for _ in range(150):
                current = mqtt_path.stat()
                if (current.st_ino, current.st_mtime_ns) != (previous.st_ino, previous.st_mtime_ns):
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError('MQTT checkpoint did not produce a new persistence file')
            for item, source, location in resolved:
                exclusions = list(item.get('exclude', []))
                if item['name'] == 'home-assistant':
                    exclusions += [p.name + '/**' for p in source.iterdir()
                                   if p.name.startswith(('.ha-homelab-stage-', '.ha-homelab-backup-'))]
                result = archive_component(source, work, item['name'], location, exclusions,
                                           item.get('generated_image_caches', []))
                manifest['components'].append(result)
                print('Captured ' + item['name'], flush=True)
            manifest['metadata']['optional_sources_absent'] = []
            for item in config['host_sources']:
                source = mapped_source(host, item['path'])
                if item.get('optional') and not source.exists():
                    manifest['metadata']['optional_sources_absent'].append(item['path'])
                    continue
                manifest['components'].append(archive_component(source, work, item['name'],
                    {'type': 'host', 'node': config['node'], 'path': item['path']}, item.get('exclude', [])))
                print('Captured ' + item['name'], flush=True)
            manifest['components'].append(archive_component(state_root, work, 'kubernetes',
                {'type': 'api', 'version': api.get('/version')['gitVersion']}))
            shutil.rmtree(state_root)
            recovery = work / '.recovery-tools'
            recovery.mkdir(mode=0o700)
            for path in Path(__file__).parent.iterdir():
                if path.suffix in {'.py', '.json', '.md'} and path.is_file():
                    (recovery / path.name).write_bytes(path.read_bytes())
            manifest['components'].append(archive_component(recovery, work, 'recovery-tools', {'type': 'backup-tool'}))
            shutil.rmtree(recovery)
            # PVC identity and pod replacement are checked again before acceptance.
            for item, _, location in resolved:
                _, current = resolve_claim(api, item, config, host, nfs)
                if current != location:
                    raise RuntimeError('Application volume changed during capture')
            before_pods = {(p['metadata']['namespace'], p['metadata']['name']): p['metadata']['uid'] for p in app_pods}
            after_pods = {(p['metadata']['namespace'], p['metadata']['name']): p['metadata']['uid']
                          for p in api.list('/api/v1/pods')['items'] if p['metadata']['namespace'] in config['namespaces']}
            if before_pods != after_pods:
                raise RuntimeError('Application pods changed during capture; retry after rollout completes')
            manifest['completed'] = True
            manifest['completed_at'] = timestamp()
            manifest['metadata']['scope_limits'] = [
                'HA recorder history and external recorder database are excluded.',
                'Other application PVCs outside the configured HA stack are not copied.',
                'SQLite online backups are transactional; exclusive-lock fallbacks use guarded physical copies. Services are not globally paused.',
                'NAS, external radio hardware, cloud accounts and upstream image registries must survive.',
            ]
            write_json(work / 'manifest.json', manifest)
            verify_snapshot(work)
            final = root / name
            work.rename(final)
            published = final
            cleanup_completed(root, config['retain_completed'])
            write_json(root / 'latest-success.json', {'state': 'complete', 'snapshot': name,
                'completed_at': manifest['completed_at'], 'components': len(manifest['components']),
                'archive_bytes': sum(c['bytes'] for c in manifest['components'])})
            write_json(root / 'latest-attempt.json', {'state': 'complete', 'snapshot': name, 'completed_at': timestamp()})
            print('Verified recovery snapshot: ' + name, flush=True)
            return final
        except Exception as error:
            diagnostic = (published or work) / 'failure-private.txt'
            try:
                diagnostic.write_text(traceback.format_exc())
                write_json(root / 'latest-attempt.json', {'state': 'failed', 'snapshot': name,
                    'snapshot_verified': published is not None,
                    'failed_at': timestamp(), 'error_type': type(error).__name__})
            except OSError:
                # A NAS outage must not replace the original failure with a
                # secondary diagnostic-write exception or claim success.
                pass
            print('Recovery snapshot failed; private diagnostic retained on NAS', file=sys.stderr)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).with_name('config.json'))
    parser.add_argument('--output', type=Path, default=Path('/backup/snapshots'))
    args = parser.parse_args()
    try:
        run(json.loads(args.config.read_text()), args.output)
    except Exception as error:
        # Do not send source data, credentials, or private API responses to logs.
        print("Recovery failed: " + type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
