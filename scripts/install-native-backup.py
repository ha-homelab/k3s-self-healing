#!/usr/bin/env python3
"""Install the weekly recorder export followed by native HA backup. Token is supplied via a private file."""
import argparse
import json
from pathlib import Path
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[1]


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
