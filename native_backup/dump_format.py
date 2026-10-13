"""Strict recorder export formats shared by publication and offline recovery."""
import gzip
import hashlib
import json
import lzma
import os
import re
import stat

LEGACY_FILE = 'recorder.sql.gz'
XZ_NAME = re.compile(r'recorder-([0-9a-f]{64})\.sql\.xz')


def known_dump(name):
    return name == LEGACY_FILE or XZ_NAME.fullmatch(name) is not None


def open_dump(source, metadata):
    return gzip.open(source, 'rb') if metadata['format'] == 1 else lzma.open(source, 'rb', format=lzma.FORMAT_XZ)


def inspect_dump(source, version):
    """Hash the compressed bytes as the codec reads them, using one file pass."""
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    before = source.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('Regular recorder export required')

    class HashReader:
        def __init__(self, stream):
            self.stream = stream
            self.digest = hashlib.sha256()
            self.size = 0

        def read(self, size=-1):
            data = self.stream.read(size)
            self.digest.update(data)
            self.size += len(data)
            return data

        def seekable(self):
            return False

    total = 0
    with source.open('rb') as stream:
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError('Recorder export changed before verification')
        reader = HashReader(stream)
        with open_dump(reader, {'format': version}) as decoded:
            while chunk := decoded.read(4 * 1024**2):
                total += len(chunk)
        # Some codecs stop before consuming trailing bytes. The checksum must
        # still cover the complete compressed file, including those bytes.
        # The export pipeline retains xz -t as its strict XZ framing check.
        while reader.read(4 * 1024**2):
            pass
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError('Recorder export changed during verification')
    if identity(source.lstat()) != identity(before) or reader.size != before.st_size:
        raise ValueError('Recorder export changed during verification')
    return {'sha256': reader.digest.hexdigest(), 'compressed_bytes': reader.size,
            'uncompressed_bytes': total}


def validate_metadata(meta):
    if not isinstance(meta, dict):
        raise ValueError('Invalid recorder manifest')
    if type(meta.get('format')) is not int:
        raise ValueError('Unsupported recorder export format')
    digest = meta.get('sha256', '')
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('Invalid recorder checksum')
    legacy = meta.get('format') == 1 and meta.get('file') == LEGACY_FILE
    modern = (meta.get('format') == 2 and meta.get('compression') == 'xz'
              and meta.get('file') == f'recorder-{digest}.sql.xz')
    if not (legacy or modern):
        raise ValueError('Unsupported recorder export format')
    for key in ('compressed_bytes', 'uncompressed_bytes'):
        if type(meta.get(key)) is not int or meta[key] <= 0:
            raise ValueError('Invalid recorder byte count')
    tables = meta.get('tables')
    if (not isinstance(tables, list) or not tables
            or any(not isinstance(table, dict) or not isinstance(table.get('name'), str)
                   or not table['name'] for table in tables)):
        raise ValueError('Recorder export is incomplete')


def read_manifest(directory):
    manifest = directory / 'manifest.json'
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError('Regular recorder manifest required')
    meta = json.loads(manifest.read_text())
    validate_metadata(meta)
    source = directory / meta['file']
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size != meta['compressed_bytes']:
        raise ValueError('Recorder export size mismatch')
    return meta


def verify_dump(directory):
    meta = read_manifest(directory)
    source = directory / meta['file']
    inspected = inspect_dump(source, meta['format'])
    if inspected['sha256'] != meta['sha256']:
        raise ValueError('Recorder export checksum mismatch')
    if (inspected['uncompressed_bytes'] != meta['uncompressed_bytes']
            or inspected['compressed_bytes'] != meta['compressed_bytes']):
        raise ValueError('Recorder export is incomplete or changed during verification')
    return meta
