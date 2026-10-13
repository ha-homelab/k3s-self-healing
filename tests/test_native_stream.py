"""Bounded local fixtures measure actual compressed reads, without a SQL export."""
from contextlib import contextmanager
import gzip
import hashlib
import importlib.util
import json
import lzma
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'native_backup'))
import dump_format

spec = importlib.util.spec_from_file_location('stream_runner', ROOT / 'native_backup/runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class CountedFile:
    def __init__(self, stream, counts):
        self.stream, self.counts = stream, counts

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def read(self, size=-1):
        data = self.stream.read(size)
        self.counts['bytes'] += len(data)
        return data

    def readinto(self, buffer):
        count = self.stream.readinto(buffer)
        self.counts['bytes'] += count
        return count

    def seek(self, *args):
        raise AssertionError('Compressed verification must not reread or rewind')


class StreamTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sql = random.Random(2026).randbytes(512 * 1024)

    def test_each_codec_hashes_and_decodes_one_actual_compressed_read(self):
        for version, encoded in ((1, gzip.compress(self.sql)), (2, lzma.compress(self.sql, preset=0))):
            with self.subTest(format=version):
                path = self.root / 'dump';path.write_bytes(encoded)
                counts = {'bytes': 0, 'opens': 0}
                original = Path.open

                def opened(target, *args, **kwargs):
                    stream = original(target, *args, **kwargs)
                    if target == path:
                        counts['opens'] += 1
                        return CountedFile(stream, counts)
                    return stream

                with patch.object(Path, 'open', opened), patch.object(dump_format, 'open_dump', wraps=dump_format.open_dump) as decoder:
                    result = dump_format.inspect_dump(path, version)
                self.assertEqual(counts, {'bytes': len(encoded), 'opens': 1})
                self.assertEqual(decoder.call_count, 1)
                self.assertEqual(result, {'sha256': hashlib.sha256(encoded).hexdigest(),
                    'compressed_bytes': len(encoded), 'uncompressed_bytes': len(self.sql)})

    def test_truncated_and_crc_corrupt_streams_are_rejected(self):
        for version, good in ((1, gzip.compress(self.sql)), (2, lzma.compress(self.sql, preset=0))):
            bad_crc = bytearray(good)
            bad_crc[-8 if version == 1 else -5] ^= 1
            for data in (good[:-12], bytes(bad_crc)):
                with self.subTest(format=version, truncated=len(data) != len(good)):
                    path = self.root / 'dump';path.write_bytes(data)
                    with self.assertRaises((EOFError, gzip.BadGzipFile, lzma.LZMAError)):
                        dump_format.inspect_dump(path, version)

    def test_same_bytes_at_replaced_path_still_fail_identity_check(self):
        source = self.root / 'dump'
        encoded = lzma.compress(self.sql, preset=0);source.write_bytes(encoded)
        original = dump_format.open_dump

        @contextmanager
        def replacing(*args):
            with original(*args) as decoded:
                yield decoded
            source.rename(self.root / 'old-inode')
            source.write_bytes(encoded)

        with patch.object(dump_format, 'open_dump', replacing):
            with self.assertRaisesRegex(ValueError, 'changed'):
                dump_format.inspect_dump(source, 2)

    def test_checksum_covers_trailing_bytes_even_when_python_xz_stops_early(self):
        # xz -t remains the publication pipeline's strict framing guard. This
        # checks that hashing is never accidentally limited to decoded input.
        encoded = lzma.compress(self.sql, preset=0) + b'garbage' * 8192
        path = self.root / 'dump';path.write_bytes(encoded)
        result = dump_format.inspect_dump(path, 2)
        self.assertEqual(result['sha256'], hashlib.sha256(encoded).hexdigest())
        self.assertEqual(result['compressed_bytes'], len(encoded))

    def test_changed_publication_retains_independent_cleanup_decode(self):
        config = self.root / 'config';output = config / 'recorder_backup'
        stage = config / 'backups/recorder-export-staging';work = self.root / 'work'
        for directory in (output, stage, work):directory.mkdir(parents=True)
        old = gzip.compress(self.sql)
        (output / 'recorder.sql.gz').write_bytes(old)
        (output / 'manifest.json').write_text(json.dumps({'format': 1, 'file': 'recorder.sql.gz',
            'sha256': hashlib.sha256(old).hexdigest(), 'compressed_bytes': len(old),
            'uncompressed_bytes': len(self.sql), 'tables': [{'name': 'states'}]}))
        pending = stage / 'recorder.sql.xz.pending'
        encoded = lzma.compress(self.sql + b'next', preset=0);pending.write_bytes(encoded)
        (stage / 'manifest.json').write_text(json.dumps({'format': 2, 'compression': 'xz', 'tables': [{'name': 'states'}]}))
        counts = {'bytes': 0};original = Path.open

        def opened(target, *args, **kwargs):
            stream = original(target, *args, **kwargs)
            return CountedFile(stream, counts) if target.name.endswith(('.sql.xz', '.sql.xz.pending')) else stream

        with patch.multiple(runner, CONFIG=config, OUTPUT=output, STAGE=stage, WORK=work), patch.object(Path, 'open', opened), patch.object(dump_format, 'open_dump', wraps=dump_format.open_dump) as decoder:
            runner.publish()
        # The unchanged shell xz -t adds a third read/decode. Python publication
        # and post-pointer cleanup each perform one pass, down from two each.
        self.assertEqual(counts['bytes'], 2 * len(encoded))
        self.assertEqual(decoder.call_count, 2)
        self.assertFalse((output / 'recorder.sql.gz').exists())
        self.assertEqual(dump_format.verify_dump(output)['uncompressed_bytes'], len(self.sql) + 4)
