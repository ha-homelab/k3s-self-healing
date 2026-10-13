"""Replay synthetic seeds and boundary mutations through the real pure functions."""

import json
import unittest
from pathlib import Path

from fuzz.oracles import recorder_metadata, runtime_contract

ROOT = Path(__file__).resolve().parents[1]


class FuzzOracleTests(unittest.TestCase):
    def test_committed_synthetic_corpus(self):
        for name, oracle in (
            ("recorder-metadata", recorder_metadata),
            ("runtime-contract", runtime_contract),
        ):
            for seed in sorted((ROOT / "fuzz/corpus" / name).iterdir()):
                with self.subTest(target=name, seed=seed.name):
                    oracle(seed.read_bytes())

    def test_recorder_field_type_and_value_boundaries(self):
        valid = json.loads(
            (ROOT / "fuzz/corpus/recorder-metadata/valid-xz.json").read_text()
        )
        values = [None, False, True, -1, 0, 1, 1.5, "", "../outside", [], {}, [{}]]
        for key in valid:
            for value in values:
                with self.subTest(key=key, value=value):
                    recorder_metadata(json.dumps({**valid, key: value}).encode())

    def test_reviewed_profiles_and_independent_invalidations(self):
        for profile in range(2):
            for operation in (
                "unchanged",
                "source",
                "remove",
                "dependency",
                "python",
                "prerelease",
            ):
                for key in ("synthetic", "components/backup/manager.py", "securetar"):
                    runtime_contract(
                        json.dumps(
                            {
                                "profile": profile,
                                "operation": operation,
                                "key": key,
                                "value": "changed",
                            }
                        ).encode()
                    )

    def test_non_documents_and_truncated_data(self):
        for data in (
            b"",
            b"{",
            b"[]",
            b"null",
            b"false",
            b"NaN",
            b"Infinity",
            b"-Infinity",
            b"1",
            b"\xff",
            b"[" * 2048,
        ):
            recorder_metadata(data)
            runtime_contract(data)
