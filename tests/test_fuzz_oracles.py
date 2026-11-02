"""Replay synthetic seeds and boundary mutations through the real pure functions."""

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fuzz.oracles import recorder_metadata, runtime_contract
from native_backup.contract import REVIEWED_DEPENDENCIES, REVIEWED_PROFILES, source_digest

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
        for index, (version, profile) in enumerate(REVIEWED_PROFILES.items()):
            for operation in (
                "unchanged",
                "source",
                "remove",
                "dependency",
                "python",
                "prerelease",
            ):
                keys = {"synthetic", *profile["files"],
                        *profile.get("dependencies", REVIEWED_DEPENDENCIES)}
                for key in sorted(keys):
                    with self.subTest(version=version, operation=operation, key=key):
                        runtime_contract(
                            json.dumps(
                                {
                                    "profile": index,
                                    "operation": operation,
                                    "key": key,
                                    "value": "changed",
                                }
                            ).encode()
                        )

    def test_positive_controls_wrap_indexes_without_mutating_profiles(self):
        before = copy.deepcopy(REVIEWED_PROFILES)
        count = len(REVIEWED_PROFILES)
        for index in range(-count, 2 * count):
            with self.subTest(index=index):
                runtime_contract(json.dumps({"profile": index, "operation": "unchanged"}).encode())
        # Exact input reported by the release fuzzer, not just the canonical index.
        runtime_contract(b'{"profile":5,"operation":"unchanged"}\n')
        self.assertEqual(REVIEWED_PROFILES, before)

    def test_oracle_rejects_erroneously_accepted_mutations(self):
        def accept_invalid(version, fingerprints, dependencies, python_minor):
            return {"allowed": True, "source_sha256": source_digest(fingerprints)}

        for index, (version, profile) in enumerate(REVIEWED_PROFILES.items()):
            mutations = (
                ("source", next(iter(profile["files"]))),
                ("dependency", next(iter(profile.get("dependencies", REVIEWED_DEPENDENCIES)))),
                ("python", "synthetic"),
            )
            for operation, key in mutations:
                with self.subTest(version=version, operation=operation), patch(
                    "fuzz.oracles.validate_snapshot", side_effect=accept_invalid
                ), self.assertRaises(AssertionError):
                    runtime_contract(json.dumps({
                        "profile": index, "operation": operation, "key": key, "value": "changed",
                    }).encode())

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
