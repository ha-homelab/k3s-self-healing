#!/usr/bin/env python3
"""Install a stopped HA recovery workspace and restore the latest complete native NAS backup."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def latest_backup(ssh, directory):
    # Read only the requested backup folder, not arbitrary NAS configuration.
    code = '''import json,pathlib,sys
p=pathlib.Path(sys.argv[1]);items=[]
for f in p.glob('*_meta.json'):
 try:
  m=json.loads(f.read_text());bid=m['backup_id']
  if not bid or any(c not in '0123456789abcdef' for c in bid):continue
  t=f.with_name(f.name[:-len('_meta.json')]+'.tar')
  if t.is_file() and not t.is_symlink() and t.stat().st_size==m['size'] and not m.get('protected') and m.get('homeassistant_included'):
   item={k:m[k] for k in ['backup_id','date','size','homeassistant_version']};item['filename']=t.name;items.append(item)
 except (ValueError,KeyError,OSError):continue
if not items:raise SystemExit('No complete unencrypted native HA backup found')
print(json.dumps(max(items,key=lambda m:m['date'])))
'''
    raw = subprocess.check_output(ssh + [shlex.join(['sudo', '-n', 'python3', '-c', code, directory])])
    return json.loads(raw)


def validate_resume(stateful, pod, image):
    """Never repurpose an active HA or an unrelated pod during a retry."""
    owner = stateful.get('metadata', {}).get('annotations', {})
    container = pod['spec']['containers'][0]
    if (owner.get('meta.helm.sh/release-name') != 'ha-recovery'
            or stateful['spec'].get('replicas') != 0
            or container.get('image') != image
            or container.get('command') != ['sleep', 'infinity']
            or pod['metadata'].get('labels', {}).get('app') != 'ha-recovery-restore'):
        raise ValueError('Resume requires this stopped HA recovery workspace')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kubeconfig', type=Path, required=True)
    parser.add_argument('--namespace', required=True, help='A dedicated recovery namespace')
    parser.add_argument('--resume', action='store_true', help='Resume this installer after an image pull or transfer interruption')
    parser.add_argument('--node', required=True)
    parser.add_argument('--nas-host', required=True)
    parser.add_argument('--nas-directory', required=True)
    parser.add_argument('--storage-size', default='20Gi')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,49}', args.namespace) or args.namespace in ['homeassistant', 'default', 'kube-system']:
        parser.error('Use a new dedicated recovery namespace')
    k = ['kubectl', '--kubeconfig', str(args.kubeconfig)]
    # Do not interpret a connection or permission error as an absent namespace.
    namespaces = json.loads(subprocess.check_output(k + ['get', 'namespaces', '-o', 'json']))
    exists = any(x['metadata']['name'] == args.namespace for x in namespaces['items'])
    if exists and not args.resume:
        parser.error('Namespace already exists; use a new namespace or explicitly resume this stopped installer')
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'LogLevel=ERROR',
           '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15',
           '-o', 'ServerAliveCountMax=2', args.nas_host]
    backup = latest_backup(ssh, args.nas_directory)
    if not re.fullmatch(r'\d{4}\.\d{1,2}\.\d+', backup['homeassistant_version']):
        parser.error('Unexpected HA version in archive metadata')
    image = 'ghcr.io/home-assistant/home-assistant:' + backup['homeassistant_version']
    kn = k + ['-n', args.namespace]
    if exists:
        stateful = json.loads(subprocess.check_output(kn + ['get', 'statefulset', 'ha-recovery', '-o', 'json']))
        pod = json.loads(subprocess.check_output(kn + ['get', 'pod', 'ha-recovery-restore', '-o', 'json']))
        validate_resume(stateful, pod, image)
    else:
        subprocess.run(['helm', '--kubeconfig', str(args.kubeconfig), 'install', 'ha-recovery',
            str(ROOT / 'helm/native-recovery'), '--namespace', args.namespace, '--create-namespace',
            '--set-string', 'nodeName=' + args.node, '--set-string', 'image=' + image,
            '--set-string', 'storageSize=' + args.storage_size], check=True)
    guard = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'ha-smartthings-time-guard'},
             'data': {'50-smartthings-null-time.py': (ROOT / 'helm/smartthings-time-guard/patch.py').read_text()}}
    subprocess.run(kn + ['apply', '-f', '-'], input=json.dumps(guard), text=True, check=True)
    go2rtc_guard = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'ha-go2rtc-startup-guard'},
                    'data': {'51-go2rtc-startup.py': (ROOT / 'helm/go2rtc-startup-guard/patch.py').read_text()}}
    subprocess.run(kn + ['apply', '-f', '-'], input=json.dumps(go2rtc_guard), text=True, check=True)
    subprocess.run(kn + ['wait', '--for=condition=Ready', 'pod/ha-recovery-restore', '--timeout=900s'], check=True)
    # Stream without a second local archive. Keep remote stdout open until all
    # stdin is consumed: a last-command shell `cat > file` closes stdout early
    # and some kubectl/server versions then truncate stdin without a nonzero exit.
    if Path(backup['filename']).name != backup['filename']:
        raise ValueError('Invalid NAS filename')
    remote = str(Path(args.nas_directory) / backup['filename'])
    print('Transferring native backup ' + backup['backup_id'], flush=True)
    copy_code = 'import shutil,sys;f=open(sys.argv[1],"rb");shutil.copyfileobj(f,sys.stdout.buffer,1048576)'
    receive_code = 'import os,shutil,sys;os.umask(0o077);f=open(sys.argv[1],"wb");shutil.copyfileobj(sys.stdin.buffer,f,1048576);f.close();print("Archive transfer received")'
    source = subprocess.Popen(ssh + [shlex.join(['sudo', '-n', 'python3', '-c', copy_code, remote])],
                              stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    try:
        transfer = subprocess.run(kn + ['exec', '-i', 'ha-recovery-restore', '--', 'python', '-c',
            receive_code, '/restore/native-backup.tar.pending'], stdin=source.stdout, check=False)
        source.stdout.close()
        source_code = source.wait()
        if source_code or transfer.returncode:
            raise RuntimeError(f'Archive transfer failed (SSH {source_code}, kubectl {transfer.returncode}); HA remains stopped')
    finally:
        if source.poll() is None:
            source.terminate();source.wait()
    size = int(subprocess.check_output(kn + ['exec', 'ha-recovery-restore', '--', 'stat', '-c', '%s', '/restore/native-backup.tar.pending']))
    if size != backup['size']:
        raise RuntimeError('Transferred archive size does not match completed NAS metadata')
    subprocess.run(kn + ['exec', 'ha-recovery-restore', '--', 'mv', '/restore/native-backup.tar.pending', '/restore/native-backup.tar'], check=True)
    for name in ('restore.py', 'dump_format.py'):
        subprocess.run(kn + ['exec', '-i', 'ha-recovery-restore', '--', 'python', '-c',
            'import pathlib,sys;pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())', '/tmp/' + name],
            input=(ROOT / 'native_backup' / name).read_bytes(), check=True)
    subprocess.run(kn + ['exec', 'ha-recovery-restore', '--', 'mkdir', '-p', '-m', '700', '/restore/share'], check=True)
    subprocess.run(kn + ['exec', 'ha-recovery-restore', '--', 'python', '/tmp/restore.py',
        '/restore/native-backup.tar', '--destination', '/restore/config', '--report', '/restore/verification.json'],
        check=True, stdout=subprocess.DEVNULL)
    output = subprocess.check_output(kn + ['exec', 'ha-recovery-restore', '--', 'cat', '/restore/verification.json'])
    report = json.loads(output)
    if report['backup_id'] != backup['backup_id']:
        raise RuntimeError('Transferred backup identity differs from NAS metadata')
    report.update(namespace=args.namespace, node=args.node, native_archive_bytes=backup['size'])
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'external_links_skipped'}, indent=2))
    print('HA is stopped. Restore the recorder to an EMPTY target, fence the previous HA, then follow the runbook to start.')


if __name__ == '__main__':
    main()
