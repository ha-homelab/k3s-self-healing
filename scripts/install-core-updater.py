#!/usr/bin/env python3
"""Render/check or install the separate, approval-gated Home Assistant updater.

No Home Assistant upgrade is performed by this installer. Secret values are
passed to kubectl on stdin and never included in preview output or argv.
"""
import argparse
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import stat
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]
NAME = 'ha-core-updater'
CREDENTIAL_KEYS = frozenset({
    'approval_token', 'mqtt_username', 'mqtt_password', 'ssh-privatekey', 'known_hosts',
})


def private_json(path):
    """Reject symlinks, non-owned files and group/world access before reading."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Credentials file must be owned by the current user, regular, and mode 0600 or 0400')
        return json.load(stream)


def credentials_secret(path, namespace):
    values = private_json(path)
    if not isinstance(values, dict) or set(values) != CREDENTIAL_KEYS:
        raise ValueError('Credentials JSON must contain exactly: ' + ', '.join(sorted(CREDENTIAL_KEYS)))
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise ValueError('Every credential must be a non-empty string')
    if len(values['approval_token']) < 32 or any(char.isspace() for char in values['approval_token']):
        raise ValueError('approval_token must contain at least 32 characters and no whitespace')
    if 'PRIVATE KEY' not in values['ssh-privatekey'] or '\n' not in values['ssh-privatekey']:
        raise ValueError('ssh-privatekey must contain the complete private key, including newlines')
    return {
        'apiVersion': 'v1', 'kind': 'Secret',
        'metadata': {'name': NAME + '-credentials', 'namespace': namespace},
        'type': 'Opaque',
        'data': {key: base64.b64encode(value.encode()).decode() for key, value in values.items()},
    }


def reject_inline_secrets(value):
    """Settings belong in a ConfigMap; credentials belong only in a Secret."""
    secret_names = {'approval_token', 'mqtt_password', 'password', 'token', 'ha_token',
                    'access_token', 'private_key', 'ssh-privatekey'}
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in secret_names:
                raise ValueError('Settings may reference credential file paths, but may not contain inline secrets')
            reject_inline_secrets(child)
    elif isinstance(value, list):
        for child in value:
            reject_inline_secrets(child)


def code_configmap(settings, namespace, root=ROOT):
    if not isinstance(settings, dict):
        raise ValueError('Settings JSON must be an object')
    reject_inline_secrets(settings)
    sources = sorted((root / 'core_updates').glob('*.py'))
    if not any(path.name == 'server.py' for path in sources):
        raise ValueError('core_updates/server.py is missing')
    if any(path.is_symlink() for path in sources):
        raise ValueError('Code files must not be symlinks')
    data = {path.name: path.read_text() for path in sources}
    contract = root / 'native_backup/contract.py'
    if contract.is_symlink():
        raise ValueError('Backup contract must not be a symlink')
    data['backup_contract.py'] = contract.read_text()
    data['settings.json'] = json.dumps(settings, indent=2, sort_keys=True) + '\n'
    if len(json.dumps(data).encode()) > 900_000:
        raise ValueError('Code/configuration is too large for a Kubernetes ConfigMap')
    return {'apiVersion': 'v1', 'kind': 'ConfigMap',
            'metadata': {'name': NAME + '-code', 'namespace': namespace}, 'data': data}


class Kubectl:
    def __init__(self, kubeconfig, namespace):
        self.base = ['kubectl', '--kubeconfig', str(kubeconfig), '--request-timeout=60s']
        self.namespace = namespace

    def run(self, args, *, namespace=None, data=None, confidential=False):
        command = self.base + ['-n', namespace or self.namespace] + args
        result = subprocess.run(command, input=data, text=True, capture_output=True, check=False)
        if result.returncode:
            # API errors may echo an invalid Secret/ConfigMap body. Never relay
            # stderr for a manifest apply, even in --dry-run mode.
            if confidential:
                raise RuntimeError('Kubernetes rejected a manifest; sensitive server output was suppressed')
            raise RuntimeError('kubectl failed: ' + result.stderr.strip()[:1000])
        return result.stdout

    def get(self, kind, name, *, namespace=None):
        return json.loads(self.run(['get', kind, name, '-o', 'json'], namespace=namespace))

    def require_secret(self, name, required_keys):
        # Return only field names; the command never requests or outputs values.
        template = '{{range $key, $value := .data}}{{$key}}{{"\\n"}}{{end}}'
        names = set(self.run(['get', 'secret', name, '-o', 'go-template=' + template]).splitlines())
        if not required_keys.issubset(names):
            raise ValueError(f'Secret {name} is missing required keys')

    def apply(self, objects, *, dry_run):
        args = ['apply', '--server-side', '--field-manager=ha-core-updater-installer']
        if dry_run:
            args.append('--dry-run=server')
        # Server-side apply avoids an additional full Secret copy in a
        # kubectl.kubernetes.io/last-applied-configuration annotation.
        args += ['-f', '-', '-o', 'name']
        output = self.run(args, data=yaml.safe_dump_all(objects, sort_keys=False), confidential=True)
        print(output, end='')


def host_cidr(address):
    ip = ipaddress.ip_address(address)
    if ip.version != 4:
        raise ValueError('This deployment policy currently supports IPv4 clusters only')
    return str(ip) + '/32'


def discover(kube, args):
    """Inspect the current deployment without editing its Helm values/objects."""
    statefulset = kube.get('statefulset', args.ha_statefulset)
    pod = statefulset['spec']['template']['spec']
    node_name = args.node or pod.get('nodeSelector', {}).get('kubernetes.io/hostname')
    if not node_name:
        existing_pod = kube.get('pod', args.ha_statefulset + '-0')
        node_name = existing_pod['spec']['nodeName']
    node = kube.get('node', node_name)
    node_ip = next(item['address'] for item in node['status']['addresses'] if item['type'] == 'InternalIP')
    if node['status']['nodeInfo']['architecture'] not in {'arm64', 'amd64'}:
        raise ValueError('Pinned Helm installer supports only Linux arm64 and amd64 nodes')
    mount = next((mount for container in pod['containers'] for mount in container.get('volumeMounts', [])
                  if mount['mountPath'] == '/config'), None)
    if not mount and not args.config_pvc:
        raise ValueError('Cannot discover HA /config volume; supply --config-pvc and --config-subpath')
    volumes = {item['name']: item for item in pod.get('volumes', [])}
    pvc = args.config_pvc
    if not pvc:
        volume = volumes.get(mount['name'], {})
        pvc = volume.get('persistentVolumeClaim', {}).get('claimName')
        if not pvc:
            templates = {item['metadata']['name'] for item in statefulset['spec'].get('volumeClaimTemplates', [])}
            if mount['name'] in templates:
                pvc = mount['name'] + '-' + args.ha_statefulset + '-0'
    if not pvc:
        raise ValueError('HA /config must be backed by a PVC')
    if pvc == NAME + '-state':
        raise ValueError('Updater state and HA configuration must use different PVCs')
    kube.get('pvc', pvc)
    subpath = args.config_subpath if args.config_subpath is not None else (mount or {}).get('subPath', '')
    if (mount or {}).get('subPathExpr'):
        raise ValueError('Dynamic HA subPathExpr requires an explicitly reviewed deployment')
    if subpath and (Path(subpath).is_absolute() or '..' in Path(subpath).parts):
        raise ValueError('Configuration subpath must remain within the PVC')
    ha_service = kube.get('service', args.ha_service)
    mqtt_service = kube.get('service', args.mqtt_service)
    if not ha_service['spec'].get('selector') or not mqtt_service['spec'].get('selector'):
        raise ValueError('HA and MQTT Services must select pods for this NetworkPolicy')
    api_service = kube.get('service', 'kubernetes', namespace='default')
    api_endpoints = kube.get('endpoints', 'kubernetes', namespace='default')
    api_addresses = {api_service['spec']['clusterIP']}
    api_ports = {443}
    for subset in api_endpoints.get('subsets', []):
        api_addresses.update(item['ip'] for item in subset.get('addresses', []))
        api_ports.update(item['port'] for item in subset.get('ports', []) if item.get('protocol', 'TCP') == 'TCP')
    if len(api_addresses) < 2:
        raise ValueError('Could not determine API backend address for NetworkPolicy')
    ha_address = args.ha_source_cidr or host_cidr(node_ip)
    network = ipaddress.ip_network(ha_address, strict=True)
    if network.version != 4 or network.prefixlen != 32:
        raise ValueError('--ha-source-cidr must identify exactly one IPv4 host (/32)')
    return {'node': node_name, 'pvc': pvc, 'subpath': subpath,
            'ha_address': ha_address, 'ha_selector': ha_service['spec']['selector'],
            'mqtt_selector': mqtt_service['spec']['selector'],
            'mqtt_ip': mqtt_service['spec']['clusterIP'],
            'api_addresses': sorted(map(host_cidr, api_addresses)), 'api_ports': sorted(api_ports)}


def render(args, cluster, configmap, root=ROOT, credential_checksum=None):
    objects = list(yaml.safe_load_all((root / 'core_updates/deployment.yaml').read_text()))
    checksum = hashlib.sha256(json.dumps(configmap['data'], sort_keys=True).encode()).hexdigest()
    for item in objects:
        item['metadata']['namespace'] = args.namespace
        if item['kind'] == 'RoleBinding':
            item['subjects'][0]['namespace'] = args.namespace
        elif item['kind'] == 'Role':
            for rule in item['rules']:
                if rule.get('resourceNames') == ['home-assistant']:
                    rule['resourceNames'] = [args.ha_statefulset if rule['apiGroups'] == ['apps'] else args.ha_service]
        elif item['kind'] == 'PersistentVolumeClaim':
            if args.storage_class is not None:
                item['spec']['storageClassName'] = args.storage_class
        elif item['kind'] == 'Deployment':
            template = item['spec']['template']
            template['metadata']['annotations'] = {'checksum/code-settings': checksum}
            if credential_checksum:
                template['metadata']['annotations']['checksum/credentials'] = credential_checksum
            pod = template['spec']
            pod['nodeSelector']['kubernetes.io/hostname'] = cluster['node']
            next(volume for volume in pod['volumes'] if volume['name'] == 'config')['persistentVolumeClaim']['claimName'] = cluster['pvc']
            runtime = pod['containers'][0]
            for env in runtime['env']:
                if env['name'] == 'HA_URL':
                    env['value'] = f'http://{args.ha_service}.{args.namespace}.svc.cluster.local:8123'
                elif env['name'] == 'MQTT_HOST':
                    env['value'] = f'{args.mqtt_service}.{args.namespace}.svc.cluster.local'
            if cluster['subpath']:
                next(mount for mount in runtime['volumeMounts'] if mount['name'] == 'config')['subPath'] = cluster['subpath']
        elif item['kind'] == 'NetworkPolicy' and item['metadata']['name'] == NAME:
            spec = item['spec']
            ha_peers = [{'ipBlock': {'cidr': cluster['ha_address']}},
                        {'podSelector': {'matchLabels': cluster['ha_selector']}}]
            spec['ingress'][0]['from'] = ha_peers
            spec['egress'][1]['to'] = [{'ipBlock': {'cidr': address}} for address in cluster['api_addresses']]
            spec['egress'][1]['ports'] = [{'protocol': 'TCP', 'port': port} for port in cluster['api_ports']]
            spec['egress'][2]['to'] = ha_peers
            spec['egress'][3]['to'] = [{'podSelector': {'matchLabels': cluster['mqtt_selector']}}]
            if cluster['mqtt_ip'] != 'None':
                spec['egress'][3]['to'].append({'ipBlock': {'cidr': host_cidr(cluster['mqtt_ip'])}})
    return [configmap, *objects]


def cluster_settings(settings, namespace, cluster):
    """Jobs and the controller must inspect the same namespace/config volume."""
    for key, expected in {'namespace': namespace, 'config_pvc': cluster['pvc'],
                          'config_subpath': cluster['subpath']}.items():
        if key in settings and settings[key] != expected:
            raise ValueError(f'Settings {key} conflicts with the discovered deployment; review the override')
        settings[key] = expected
    return settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', type=Path, required=True)
    parser.add_argument('--settings-file', type=Path, required=True, help='Non-secret updater settings JSON')
    parser.add_argument('--credentials-file', type=Path, help='Private JSON with all five credential values; omit to preserve the existing Secret')
    parser.add_argument('--namespace', default='homeassistant')
    parser.add_argument('--ha-statefulset', default='home-assistant')
    parser.add_argument('--ha-service', default='home-assistant')
    parser.add_argument('--mqtt-service', default='mosquitto')
    parser.add_argument('--config-pvc', help='Default: discover current HA /config PVC')
    parser.add_argument('--config-subpath', help='Default: discover current HA /config subPath')
    parser.add_argument('--node', help='Default: current HA node')
    parser.add_argument('--ha-source-cidr', help='Default: current HA node IPv4 /32')
    parser.add_argument('--storage-class', help='StorageClass for updater state PVC; default: cluster default')
    parser.add_argument('--apply', action='store_true', help='Apply only after successful server-side dry run')
    args = parser.parse_args()
    try:
        settings = json.loads(args.settings_file.read_text())
        if not isinstance(settings, dict):
            raise ValueError('Settings JSON must be an object')
        reject_inline_secrets(settings)
        secret = credentials_secret(args.credentials_file, args.namespace) if args.credentials_file else None
        kube = Kubectl(args.kubeconfig, args.namespace)
        kube.require_secret('ha-recorder-native-backup-api', {'token'})
        if secret is None:
            kube.require_secret(NAME + '-credentials', CREDENTIAL_KEYS)
        cluster = discover(kube, args)
        configmap = code_configmap(cluster_settings(settings, args.namespace, cluster), args.namespace)
        if secret:
            credential_checksum = hashlib.sha256(json.dumps(secret['data'], sort_keys=True).encode()).hexdigest()
        else:
            existing = kube.run(['get', 'deployment', NAME, '--ignore-not-found', '-o', 'json'])
            deployment = json.loads(existing) if existing.strip() else {}
            credential_checksum = deployment.get('spec', {}).get('template', {}).get('metadata', {}).get('annotations', {}).get('checksum/credentials')
        objects = render(args, cluster, configmap, credential_checksum=credential_checksum)
        if secret:
            objects.insert(0, secret)
        print(f"HA /config: PVC {cluster['pvc']}, subPath {cluster['subpath'] or '(root)'}, node {cluster['node']}")
        print('Secret values and ConfigMap source are omitted from this preview.')
        print('Server-side dry run:')
        kube.apply(objects, dry_run=True)
        if not args.apply:
            print('Dry run completed. No cluster resources changed. Add --apply to install the updater.')
            return
        print('Installing the separate updater; Home Assistant is not upgraded:')
        kube.apply(objects, dry_run=False)
        print(kube.run(['rollout', 'status', 'deployment/' + NAME, '--timeout=300s']), end='')
    except (OSError, ValueError, RuntimeError, StopIteration, KeyError, yaml.YAMLError) as error:
        # JSON/YAML parser diagnostics can embed credential source snippets.
        if isinstance(error, (json.JSONDecodeError, yaml.YAMLError, KeyError, StopIteration)):
            message = 'Invalid input or unexpected cluster object structure; source values suppressed'
        else:
            message = str(error)
        parser.exit(1, message + '\n')


if __name__ == '__main__':
    main()
