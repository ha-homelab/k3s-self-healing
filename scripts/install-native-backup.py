#!/usr/bin/env python3
"""Install the weekly recorder export followed by native HA backup. Token is supplied via a private file."""
import argparse
import json
from pathlib import Path
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[1]


def temporary_host_path(ha_pod, node, expected_path=None):
    """Mirror only HA's existing, writable dedicated hostPath on the same node."""
    if ha_pod['spec'].get('nodeName') != node:
        raise ValueError('Backup job must use the running HA pod node')
    container = ha_pod['spec']['containers'][0]
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
    import ipaddress
    ipaddress.ip_network(args.database_cidr, strict=True)
    if args.config_subpath and (Path(args.config_subpath).is_absolute() or '..' in Path(args.config_subpath).parts):
        parser.error('Configuration subpath must stay within its PVC')
    base = ['kubectl', '--kubeconfig', str(args.kubeconfig)]
    node = json.loads(subprocess.check_output(base + ['get', 'node', args.node, '-o', 'json']))
    node_ip = next(x['address'] for x in node['status']['addresses'] if x['type'] == 'InternalIP')
    clients = {'arm64': 'mysql@sha256:64a9d35fe6fed01159499be26c3279ac5ee16006c2ebb1f480d634bd5c61a053',
               'amd64': 'mysql@sha256:f015b98a954d6bb92c370d93f354b85f4bcea15642d9fb2975f841a00d400b65'}
    client_image = clients[node['status']['nodeInfo']['architecture']]
    k = base + ['-n', args.namespace]
    ha_pod = json.loads(subprocess.check_output(k + ['get', 'pod', args.ha_pod, '-o', 'json'], timeout=60))
    host_path = temporary_host_path(ha_pod, args.node, args.backup_temp_host_path)
    config_mount = next(m for m in ha_pod['spec']['containers'][0]['volumeMounts'] if m['mountPath'] == '/config')
    config_volume = next(v for v in ha_pod['spec']['volumes'] if v['name'] == config_mount['name'])
    if (config_volume.get('persistentVolumeClaim', {}).get('claimName') != args.config_pvc
            or config_mount.get('subPath', '') != args.config_subpath or config_mount.get('subPathExpr')):
        raise ValueError('Backup configuration PVC/subPath must match running HA')
    storage = json.loads(subprocess.check_output(k + ['exec', args.ha_pod, '--', 'python', '-c', STORAGE_PROBE], timeout=60))
    if host_path is not None:
        if not storage['mounted'] or not storage['separate']:
            raise ValueError('HA external backup staging must be mounted on a separate filesystem')
    elif storage['mounted'] or storage['separate']:
        raise ValueError('Unrecognized HA temporary-backup mount; refusing a same-filesystem budget')
    # Keep root-owned export files private; /config itself belongs to the restored HA owner.
    subprocess.run(k + ['exec', args.ha_pod, '--', 'python', '-c',
        "from pathlib import Path;p=Path('/config/recorder_backup');p.mkdir(mode=0o700,exist_ok=True);assert not p.is_symlink();assert p.stat().st_uid==0"], check=True)
    if args.token_file:
        if args.token_file.stat().st_mode & 0o077:
            parser.error('Token file must be private (mode 0600 or 0400)')
        import base64
        secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'ha-recorder-native-backup-api'},
                  'data': {'token': base64.b64encode(args.token_file.read_bytes().strip()).decode()}}
        subprocess.run(k + ['apply', '-f', '-'], input=json.dumps(secret), text=True, check=True)
    else:
        subprocess.run(k + ['get', 'secret', 'ha-recorder-native-backup-api', '-o', 'name'], check=True)
    config = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'ha-recorder-native-backup-code'},
              'data': {name: (ROOT / 'native_backup' / name).read_text()
                       for name in ('runner.py', 'dump_format.py', 'contract.py')}}
    subprocess.run(k + ['apply', '-f', '-'], input=json.dumps(config), text=True, check=True)
    objects = list(yaml.safe_load_all((ROOT / 'native_backup/cronjob.yaml').read_text()))
    for obj in objects:
        obj['metadata']['namespace'] = args.namespace
        if obj['kind'] == 'CronJob':
            obj['spec']['timeZone'] = args.timezone
            pod = obj['spec']['jobTemplate']['spec']['template']['spec']
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
    subprocess.run(k + ['apply', '--dry-run=server', '-f', '-'], input=manifest, text=True, check=True)
    subprocess.run(k + ['apply', '-f', '-'], input=manifest, text=True, check=True)


if __name__ == '__main__':
    main()
