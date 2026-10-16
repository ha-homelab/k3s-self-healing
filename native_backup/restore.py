"""Verify/extract an unencrypted native HA backup into an EMPTY offline directory."""
import argparse
import gzip
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile

from dump_format import verify_dump


def relative(name):
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or '\\' in name:
        raise ValueError('Unsafe archive path')
    return p


def backup_metadata(archive):
    with tarfile.open(archive, 'r:*') as outer:
        members = {}
        for member in outer:
            name = str(relative(member.name))
            if name in members:
                raise ValueError('Duplicate outer archive member')
            members[name] = member
        meta = json.load(outer.extractfile(members['backup.json']))
        if meta.get('protected'):
            raise ValueError('Encrypted backup: decrypt a private copy with Home Assistant before using this offline tool')
        if 'homeassistant.tar.gz' not in members or not meta.get('homeassistant'):
            raise ValueError('Backup does not contain Home Assistant')
        return meta


def verify_database(root):
    return verify_dump(root / 'recorder_backup')


def extract(archive, destination):
    destination = Path(destination)
    if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
        raise ValueError('Restore destination must be a new or empty directory')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Existing empty directories can be shared; protect credentials before extraction.
    destination.chmod(0o700)
    root = destination.resolve()
    meta = backup_metadata(archive)
    seen = set()
    links = []
    count = 0
    with tarfile.open(archive, 'r:*') as outer:
        member = next(m for m in outer if str(relative(m.name)) == 'homeassistant.tar.gz')
        with gzip.GzipFile(fileobj=outer.extractfile(member), mode='rb') as decompressed:
            with tarfile.open(fileobj=decompressed, mode='r|') as inner:
                for item in inner:
                    name = relative(item.name)
                    if not name.parts or name.parts[0] != 'data':
                        raise ValueError('Unexpected native backup root')
                    name = PurePosixPath(*name.parts[1:])
                    if str(name) == '.':
                        continue
                    if str(name) in seen:
                        raise ValueError('Duplicate archive member')
                    seen.add(str(name))
                    target = root / str(name)
                    if not target.resolve().is_relative_to(root):
                        raise ValueError('Archive path escaped destination')
                    if item.isdir():
                        target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    elif item.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        with target.open('xb') as output, inner.extractfile(item) as source:
                            shutil.copyfileobj(source, output, 4 * 1024**2)
                        target.chmod(item.mode & 0o777)
                        count += 1
                    elif item.issym() or item.islnk():
                        # Process only after regular files, so links cannot redirect writes.
                        links.append((str(name), item.linkname, item.islnk()))
                    else:
                        raise ValueError('Special archive member refused')
            # Tar end blocks precede the gzip trailer. Explicitly finish the
            # stream to validate every CRC/trailer before accepting this restore.
            while decompressed.read(4 * 1024**2):
                pass
    skipped = []
    for name, linkname, hardlink in links:
        target = root / name
        if target.exists() or target.is_symlink():
            raise ValueError('Link would replace an existing file')
        if hardlink:
            raw = relative(linkname)
            source = root / str(PurePosixPath(*raw.parts[1:])) if raw.parts[0] == 'data' else root / str(raw)
        elif linkname == '/config':
            source = root
        elif linkname.startswith('/config/'):
            source = root / linkname[len('/config/'):]
        else:
            source = target.parent / linkname
        if not source.resolve().is_relative_to(root):
            skipped.append(name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if hardlink:
            if not source.is_file() or source.is_symlink():
                raise ValueError('Invalid hardlink target')
            shutil.copy2(source, target)
        else:
            target.symlink_to(os.path.relpath(source, target.parent))
    for required in ['configuration.yaml', 'secrets.yaml', '.storage/core.config_entries', '.storage/core.entity_registry', '.storage/auth']:
        if not (root / required).is_file():
            raise ValueError('Required Home Assistant state missing: ' + required)
    db = verify_database(root)
    return {'status': 'verified_and_extracted', 'backup_id': meta['slug'],
            'backup_date': meta['date'], 'homeassistant_version': meta['homeassistant']['version'],
            'files': count, 'external_links_skipped': skipped,
            'database': {'sha256': db['sha256'], 'compressed_bytes': db['compressed_bytes'],
                         'tables': len(db['tables']), 'completed_at': db['completed_at']},
            'homeassistant_started': False, 'database_imported': False}


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = extract(args.archive, args.destination)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'external_links_skipped'}))
