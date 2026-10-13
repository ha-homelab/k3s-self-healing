"""Validated non-secret deployment profiles shared by renderers and installers."""
import ipaddress
import json
import re
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULTS = {
    'node': 'example-ha-node', 'lan_node': 'example-lan-node',
    'node_ipv4': '192.0.2.10', 'nas_ipv4': '192.0.2.20',
    'database_ipv4': '192.0.2.30', 'kubernetes_service_ipv4': '10.43.0.1',
    'nas_export': '/exports/k3s', 'storage_class': 'local-path',
    'recovery_storage_class': 'example-nfs', 'timezone': 'Etc/UTC',
    'deployment_repository': 'git@github.com:example/ha-deployment.git',
    'enable_music_backup': False, 'recovery_components': ['home-assistant', 'mosquitto'],
}


def load_profile(path, *, applying=False):
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or set(raw) != set(DEFAULTS):
        raise ValueError('Profile must contain exactly the documented non-secret fields')
    for key in ('node', 'lan_node', 'storage_class', 'recovery_storage_class'):
        if not isinstance(raw[key], str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', raw[key]):
            raise ValueError('Invalid Kubernetes name in profile: ' + key)
    for key in ('node_ipv4', 'nas_ipv4', 'database_ipv4', 'kubernetes_service_ipv4'):
        if not isinstance(raw[key], str):
            raise ValueError('Expected IPv4 string: ' + key)
        address = ipaddress.IPv4Address(raw[key])
        if applying and any(address in ipaddress.ip_network(net) for net in
                            ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24')):
            raise ValueError('Documentation addresses cannot be deployed')
    export = raw['nas_export']
    if (not isinstance(export, str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', export)
            or '..' in PurePosixPath(export).parts or str(PurePosixPath(export)) != export or export == '/'):
        raise ValueError('NAS export must be a canonical absolute directory')
    try:
        ZoneInfo(raw['timezone'])
    except (ZoneInfoNotFoundError, TypeError, ValueError) as error:
        raise ValueError('Invalid profile timezone') from error
    if not isinstance(raw['deployment_repository'], str) or not re.fullmatch(
            r'git@github\.com:[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+\.git', raw['deployment_repository']):
        raise ValueError('Deployment repository must be a GitHub SSH URL')
    if type(raw['enable_music_backup']) is not bool:
        raise ValueError('enable_music_backup must be boolean')
    components = raw['recovery_components']
    if (not isinstance(components, list) or not all(isinstance(x, str) for x in components)
            or len(set(components)) != len(components)
            or not {'home-assistant', 'mosquitto'}.issubset(components)
            or not set(components).issubset({'home-assistant', 'mosquitto', 'esphome', 'ring-mqtt', 'music-assistant-backups'})):
        raise ValueError('Invalid recovery component selection')
    if applying and any(raw[key].startswith('example-') for key in ('node', 'lan_node', 'recovery_storage_class')):
        raise ValueError('Replace example node and storage names before deployment')
    return raw


def materialize(value, profile):
    """Replace only explicit template sentinels; never evaluate expressions."""
    if isinstance(value, dict):
        return {key: materialize(item, profile) for key, item in value.items()}
    if isinstance(value, list):
        return [materialize(item, profile) for item in value]
    if not isinstance(value, str):
        return value
    pairs = {default: profile[key] for key, default in DEFAULTS.items() if isinstance(default, str)}
    # One pass prevents a configured value being rewritten by another sentinel.
    return re.sub('|'.join(re.escape(key) for key in sorted(pairs, key=len, reverse=True)),
                  lambda match: pairs[match.group()], value)


def recovery_objects(objects, profile):
    output = []
    for item in objects:
        name = item['metadata']['name']
        if not profile['enable_music_backup'] and name.startswith(('music-assistant-', 'music-recovery-source')):
            continue
        item = materialize(item, profile)
        if item['kind'] == 'ConfigMap' and 'config.json' in item.get('data', {}):
            config = json.loads(item['data']['config.json'])
            config['claims'] = [claim for claim in config['claims'] if claim['name'] in profile['recovery_components']]
            item['data']['config.json'] = json.dumps(config, indent=2)
        if item['kind'] == 'CronJob':
            pod = item['spec']['jobTemplate']['spec']['template']['spec']
            if name == 'ha-disaster-recovery' and 'volumes' in pod:
                keep = {'source', 'nas-source', 'output', 'host-k3s-server', 'host-pv-data'}
                pod['volumes'] = [entry for entry in pod['volumes'] if entry['name'] in keep]
                for container in pod['containers']:
                    container['volumeMounts'] = [entry for entry in container['volumeMounts'] if entry['name'] in keep]
                    container['volumeMounts'].append({'name': 'host-machine-id', 'mountPath': '/host/etc/machine-id', 'readOnly': True})
                pod['volumes'].append({'name': 'host-machine-id', 'hostPath': {'path': '/etc/machine-id', 'type': 'File'}})
        output.append(item)
    return output
