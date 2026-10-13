#!/usr/bin/env python3
"""Render, validate and optionally install the NAS recovery job from a clean commit."""
import argparse
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from site_profile import load_profile, materialize, recovery_objects
NAMESPACE = 'ha-disaster-recovery'
CLAIM = 'recovery-snapshots'
CRONJOB = 'ha-disaster-recovery'


def validate_output_volume(claim, volume, profile, storage_class):
    """Accept only this bound claim on the reviewed private NAS export."""
    claim_metadata = claim.get('metadata', {})
    claim_spec = claim.get('spec', {})
    volume_metadata = volume.get('metadata', {})
    volume_spec = volume.get('spec', {})
    reference = volume_spec.get('claimRef', {})
    if (claim_metadata.get('name') != CLAIM or claim_metadata.get('namespace') != NAMESPACE
            or not claim_metadata.get('uid') or claim_metadata.get('deletionTimestamp')
            or claim.get('status', {}).get('phase') != 'Bound'):
        raise ValueError('Recovery PVC identity is invalid or is not bound')
    if (volume_metadata.get('name') != claim_spec.get('volumeName')
            or not volume_metadata.get('uid') or volume_metadata.get('deletionTimestamp')
            or volume.get('status', {}).get('phase') != 'Bound'
            or reference.get('uid') != claim_metadata['uid']
            or reference.get('name') != CLAIM or reference.get('namespace') != NAMESPACE):
        raise ValueError('Recovery PVC/PV binding identity does not match')
    if (claim_spec.get('storageClassName') != storage_class
            or volume_spec.get('storageClassName') != storage_class):
        raise ValueError('Recovery volume does not use the reviewed storage class')
    if volume_spec.get('persistentVolumeReclaimPolicy') != 'Retain':
        raise ValueError('Recovery PV must use Retain')
    source = volume_spec.get('nfs', {})
    if source.get('server') != profile['nas_server'] or source.get('readOnly'):
        raise ValueError('Recovery PV must use the reviewed writable NAS server')
    raw_path = source.get('path')
    if not isinstance(raw_path, str):
        raise ValueError('Recovery PV has no NFS directory')
    path = PurePosixPath(raw_path)
    export = PurePosixPath(profile['nas_export'])
    if (not path.is_absolute() or '..' in path.parts or str(path) != raw_path
            or '\\' in raw_path or not export.is_absolute() or '..' in export.parts):
        raise ValueError('Recovery NFS directory must be canonical and absolute')
    try:
        suffix = path.relative_to(export)
    except ValueError:
        raise ValueError('Recovery NFS directory is outside the reviewed export') from None
    if str(suffix) == '.':
        raise ValueError('Recovery PV cannot use the shared NAS export root')
    return raw_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--run-now', action='store_true')
    args = parser.parse_args()
    site = load_profile(args.profile, applying=args.apply)
    if args.run_now and not args.apply:
        parser.error('--run-now requires --apply')
    if args.apply and subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT).strip():
        parser.error('Commit reviewed source before installing the backup job')
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    rendered = subprocess.check_output(['kubectl', 'kustomize', str(ROOT / 'disaster_recovery')], text=True)
    objects = recovery_objects(list(yaml.safe_load_all(rendered)), site)
    for obj in objects:
        if obj['kind'] == 'CronJob':
            env = obj['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['env']
            next(item for item in env if item['name'] == 'SOURCE_REVISION')['value'] = revision
    if not args.apply:
        sys.stdout.write(yaml.safe_dump_all(objects, sort_keys=False))
        return
    namespace = next(obj for obj in objects if obj['kind'] == 'Namespace')
    cronjob = next(obj for obj in objects if obj['kind'] == 'CronJob'
                   and obj['metadata']['name'] == CRONJOB)
    claim_definition = next(obj for obj in objects if obj['kind'] == 'PersistentVolumeClaim'
                            and obj['metadata']['name'] == CLAIM)
    profile = materialize(json.loads((ROOT / 'disaster_recovery/config.json').read_text()), site)
    # Suspend even an existing schedule before any later validation can fail.
    # A failure after this write deliberately leaves the schedule suspended.
    cronjob['spec']['suspend'] = True
    bundle = yaml.safe_dump_all(objects, sort_keys=False)
    command = ['kubectl', '--context', args.context]
    subprocess.run(command + ['apply', '-f', '-'], input=yaml.safe_dump(namespace), text=True, check=True)
    subprocess.run(command + ['apply', '-f', '-'], input=yaml.safe_dump(cronjob), text=True, check=True)
    subprocess.run(command + ['apply', '--dry-run=server', '-f', '-'], input=bundle, text=True, check=True)
    subprocess.run(command + ['apply', '-f', '-'], input=bundle, text=True, check=True)
    subprocess.run(command + ['-n', NAMESPACE, 'wait', '--for=jsonpath={.status.phase}=Bound',
                             'pvc/' + CLAIM, '--timeout=120s'], check=True)
    claim = json.loads(subprocess.check_output(command + ['-n', NAMESPACE, 'get', 'pvc', CLAIM, '-o', 'json']))
    volume = json.loads(subprocess.check_output(command + ['get', 'pv', claim['spec']['volumeName'], '-o', 'json']))
    try:
        directory = validate_output_volume(claim, volume, profile, claim_definition['spec']['storageClassName'])
    except ValueError as error:
        raise SystemExit(str(error) + '; recovery schedule remains suspended') from None
    subprocess.run(command + ['-n', NAMESPACE, 'patch', 'cronjob', CRONJOB, '--type=merge',
                             '-p', json.dumps({'spec': {'suspend': False}})], check=True)
    print('NAS recovery directory: ' + directory + '/snapshots')
    if args.run_now:
        from datetime import datetime, timezone
        name = 'ha-dr-manual-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
        subprocess.run(command + ['-n', NAMESPACE, 'create', 'job', name,
                                 '--from=cronjob/' + CRONJOB], check=True)
        print('Observe: kubectl --context ' + args.context + ' -n ' + NAMESPACE + ' logs -f job/' + name)


if __name__ == '__main__':
    main()
