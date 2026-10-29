#!/usr/bin/env python3
"""Install the weekly recorder export followed by native HA backup. Token is supplied via a private file."""
import argparse
import base64
import ipaddress
import json
from pathlib import Path
import re
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[1]
CODE_CONFIG_MAP = 'ha-recorder-native-backup-code'


def ha_container(ha_pod):
    """Select HA by its unique configuration mount, never container order."""
    containers = [container for container in ha_pod['spec']['containers']
                  if any(m['mountPath'] == '/config'
                         for m in container.get('volumeMounts', []))]
    if len(containers) != 1:
        raise ValueError('A unique HA container with a /config mount is required')
    return containers[0]


def validate_images(ha_pod, pod):
    """Keep both Python workers on the immutable image actually running HA."""
    container = ha_container(ha_pod)
    image = container.get('image', '')
    match = re.fullmatch(r'\S+@sha256:([a-f0-9]{64})', image)
    if not match:
        raise ValueError('Running HA must use an immutable sha256 image')
    statuses = [s for s in ha_pod.get('status', {}).get('containerStatuses', [])
                if s['name'] == container['name']]
    if (ha_pod.get('status', {}).get('phase') != 'Running' or len(statuses) != 1
            or not statuses[0].get('ready') or 'running' not in statuses[0].get('state', {})):
        raise ValueError('Selected HA container must be running and ready')
    workers = [c for c in pod['initContainers'] + pod['containers']
               if c['name'] in ('prepare', 'native-backup')]
    if {c['name'] for c in workers} != {'prepare', 'native-backup'} or len(workers) != 2 or any(
            not re.fullmatch(r'\S+@sha256:' + match[1], c['image']) for c in workers):
        raise ValueError('Backup HA images must match the running HA digest; '
                         'review and align the native backup template before installing')


def check_idle(k):
    """Refuse code replacement while any pending or running Job consumes it."""
    jobs = json.loads(subprocess.check_output(k + ['get', 'jobs', '-o', 'json'], timeout=60))
    for job in jobs['items']:
        volumes = job['spec']['template']['spec'].get('volumes', [])
        consumes_code = any(
            volume.get('configMap', {}).get('name') == CODE_CONFIG_MAP
            or any(source.get('configMap', {}).get('name') == CODE_CONFIG_MAP
                   for source in volume.get('projected', {}).get('sources', []))
            for volume in volumes)
        terminal = any(c.get('type') in ('Complete', 'Failed') and c.get('status') == 'True'
                       for c in job.get('status', {}).get('conditions', []))
        if consumes_code and not terminal:
            raise ValueError('An unfinished Job uses the native backup code; wait before installing')


def temporary_host_path(ha_pod, node, expected_path=None):
    """Mirror only HA's existing, writable dedicated hostPath on the same node."""
    if ha_pod['spec'].get('nodeName') != node:
        raise ValueError('Backup job must use the running HA pod node')
    container = ha_container(ha_pod)
    mounts = [m for m in container.get('volumeMounts', []) if m['mountPath'] == '/config/tmp_backups']
    if not mounts:
        if expected_path is not None:
            raise ValueError('HA has no dedicated temporary-backup mount')
        return None
    if len(mounts) != 1:
        raise ValueError('Ambiguous HA temporary-backup mounts')
    mount = mounts[0]
    volume = next(v for v in ha_pod['spec']['volumes'] if v['name'] == mount['name'])
    host = volume.get('hostPath', {})
    path = host.get('path', '')
    if (mount.get('readOnly') or mount.get('subPath') or mount.get('subPathExpr')
            or host.get('type') != 'Directory' or not Path(path).is_absolute()
            or '..' in Path(path).parts or path == '/'):
        raise ValueError('Temporary backup storage requires a writable Directory hostPath without subPath')
    if expected_path is not None and path != expected_path:
        raise ValueError('Requested temporary-backup hostPath differs from running HA')
    return path


def configure_temporary_storage(pod, host_path, device):
    """Read-only space probe; HA itself remains the sole native-archive writer."""
    if host_path is None:
        return
    pod['volumes'].append({'name': 'backup-temporary',
                          'hostPath': {'path': host_path, 'type': 'Directory'}})
    for container in pod['initContainers'] + pod['containers']:
        if container['name'] not in ('prepare', 'native-backup'):
            continue
        # The YAML template reuses its mount list with the MySQL container.
        container['volumeMounts'] = list(container['volumeMounts']) + [
            {'name': 'backup-temporary', 'mountPath': '/ha-backup-tmp', 'readOnly': True}]
        container.setdefault('env', []).extend([
            {'name': 'HA_BACKUP_TEMP_DIR', 'value': '/ha-backup-tmp'},
            {'name': 'HA_BACKUP_TEMP_DEVICE', 'value': device}])


STORAGE_PROBE = '''import json, os, re
from pathlib import Path
config = Path('/config')
temp = config / 'tmp_backups'
if temp.is_symlink():
    raise RuntimeError('Temporary backup path must not be a symlink')
if temp.exists() and not temp.is_dir():
    raise RuntimeError('Temporary backup path must be a directory')
device = temp.stat().st_dev if temp.exists() else config.stat().st_dev
mounted = any(len(f := line.split()) > 5 and
              re.sub(r'\\\\([0-7]{3})', lambda m: chr(int(m[1], 8)), f[4]) == str(temp)
              for line in Path('/proc/self/mountinfo').read_text().splitlines())
print(json.dumps({'device': f'{os.major(device)}:{os.minor(device)}',
                  'separate': device != config.stat().st_dev, 'mounted': mounted}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', type=Path, required=True)
    parser.add_argument('--token-file', type=Path, help='Required only if the dedicated API Secret does not already exist')
    parser.add_argument('--namespace', default='homeassistant')
    parser.add_argument('--ha-pod', default='home-assistant-0')
    parser.add_argument('--ha-service', default='home-assistant')
    parser.add_argument('--config-pvc', default='home-assistant-home-assistant-0')
    parser.add_argument('--config-subpath', default='')
    parser.add_argument('--node', required=True)
    parser.add_argument('--database-cidr', required=True)
    parser.add_argument('--timezone', default='Etc/UTC')
    parser.add_argument('--backup-temp-host-path', help='Expected existing HA temporary-backup hostPath; discovered from the HA pod when omitted')
    args = parser.parse_args()
    ipaddress.ip_network(args.database_cidr, strict=True)
    if args.config_subpath and (Path(args.config_subpath).is_absolute() or '..' in Path(args.config_subpath).parts):
        parser.error('Configuration subpath must stay within its PVC')
    base = ['kubectl', '--kubeconfig', str(args.kubeconfig)]
    objects = []
    if args.token_file:
        if not args.token_file.is_file() or args.token_file.stat().st_mode & 0o077:
            parser.error('Token file must be private (mode 0600 or 0400)')
        token = args.token_file.read_bytes().strip()
        if not token:
            parser.error('Token file must not be empty')
        objects.append({'apiVersion': 'v1', 'kind': 'Secret',
                        'metadata': {'name': 'ha-recorder-native-backup-api'},
                        'data': {'token': base64.b64encode(token).decode()}})
    objects.append({'apiVersion': 'v1', 'kind': 'ConfigMap',
                    'metadata': {'name': CODE_CONFIG_MAP},
                    'data': {name: (ROOT / 'native_backup' / name).read_text()
                             for name in ('runner.py', 'dump_format.py', 'contract.py')}})
    objects.extend(yaml.safe_load_all((ROOT / 'native_backup/cronjob.yaml').read_text()))
    template = next(o for o in objects if o['kind'] == 'CronJob')
    pod = template['spec']['jobTemplate']['spec']['template']['spec']
    node = json.loads(subprocess.check_output(base + ['get', 'node', args.node, '-o', 'json'], timeout=60))
    node_ip = next(x['address'] for x in node['status']['addresses'] if x['type'] == 'InternalIP')
    clients = {'arm64': 'mysql@sha256:8ab2115abe8ca3d2745962f4e75f2f86fb8182ae19908773cd4cce811f3c106e',
               'amd64': 'mysql@sha256:5cf1e1293ea42e077e6a22114e6773b9aae3bb8f9933a2acd5a9721518ff8f5b'}
    client_image = clients[node['status']['nodeInfo']['architecture']]
    k = base + ['-n', args.namespace]
    ha_pod = json.loads(subprocess.check_output(k + ['get', 'pod', args.ha_pod, '-o', 'json'], timeout=60))
    container = ha_container(ha_pod)
    validate_images(ha_pod, pod)
    host_path = temporary_host_path(ha_pod, args.node, args.backup_temp_host_path)
    config_mounts = [m for m in container['volumeMounts'] if m['mountPath'] == '/config']
    if len(config_mounts) != 1 or config_mounts[0].get('readOnly'):
        raise ValueError('A unique writable HA configuration mount is required')
    config_mount = config_mounts[0]
    config_volume = next(v for v in ha_pod['spec']['volumes'] if v['name'] == config_mount['name'])
    if (config_volume.get('persistentVolumeClaim', {}).get('claimName') != args.config_pvc
            or config_mount.get('subPath', '') != args.config_subpath or config_mount.get('subPathExpr')):
        raise ValueError('Backup configuration PVC/subPath must match running HA')
    execute = k + ['exec', args.ha_pod, '-c', container['name']]
    contract = subprocess.run(execute + ['-i', '--', 'python', '-'], check=True,
                              input=(ROOT / 'native_backup/contract.py').read_text(),
                              text=True, capture_output=True, timeout=60)
    if json.loads(contract.stdout).get('allowed') is not True:
        raise ValueError('Running HA does not satisfy the reviewed native backup contract')
    storage = json.loads(subprocess.check_output(execute + ['--', 'python', '-c', STORAGE_PROBE], timeout=60))
    if host_path is not None:
        if not storage['mounted'] or not storage['separate']:
            raise ValueError('HA external backup staging must be mounted on a separate filesystem')
    elif storage['mounted'] or storage['separate']:
        raise ValueError('Unrecognized HA temporary-backup mount; refusing a same-filesystem budget')
    if not args.token_file:
        subprocess.run(k + ['get', 'secret', 'ha-recorder-native-backup-api', '-o', 'name'],
                       check=True, timeout=60)
    check_idle(k)
    for obj in objects:
        obj['metadata']['namespace'] = args.namespace
        if obj['kind'] == 'CronJob':
            obj['spec']['timeZone'] = args.timezone
            pod['nodeSelector']['kubernetes.io/hostname'] = args.node
            next(v for v in pod['volumes'] if v['name'] == 'config')['persistentVolumeClaim']['claimName'] = args.config_pvc
            pod['initContainers'][1]['image'] = client_image
            configure_temporary_storage(pod, host_path, storage['device'])
            for container in pod['initContainers'] + pod['containers']:
                if args.config_subpath:
                    next(v for v in container['volumeMounts'] if v['name'] == 'config')['subPath'] = args.config_subpath
            next(v for v in pod['containers'][0]['env'] if v['name'] == 'HA_URL')['value'] = (
                'http://' + args.ha_service + '.' + args.namespace + '.svc.cluster.local:8123')
        elif obj['kind'] == 'NetworkPolicy':
            obj['spec']['egress'][0]['to'][0]['ipBlock']['cidr'] = args.database_cidr
            obj['spec']['egress'][1]['to'] = [
                {'ipBlock': {'cidr': node_ip + '/32'}},
                {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': args.namespace}}}]
    manifest = yaml.safe_dump_all(objects, sort_keys=False)
    subprocess.run(k + ['apply', '--dry-run=server', '-f', '-'],
                   input=manifest, text=True, check=True, timeout=60)
    # Recheck immediately before writes: admission and contract probes can take time.
    current = json.loads(subprocess.check_output(k + ['get', 'pod', args.ha_pod, '-o', 'json'], timeout=60))
    if (current['metadata']['uid'] != ha_pod['metadata']['uid']
            or current['spec'] != ha_pod['spec']):
        raise ValueError('HA pod changed during installation preflight; retry after the rollout')
    validate_images(current, pod)
    check_idle(k)
    # Keep root-owned export files private; /config itself belongs to the restored HA owner.
    subprocess.run(execute + ['--', 'python', '-c',
        "from pathlib import Path;p=Path('/config/recorder_backup');p.mkdir(mode=0o700,exist_ok=True);assert not p.is_symlink();assert p.stat().st_uid==0"],
        check=True, timeout=60)
    subprocess.run(k + ['apply', '-f', '-'], input=manifest, text=True, check=True, timeout=60)


if __name__ == '__main__':
    main()
