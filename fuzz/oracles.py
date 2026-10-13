"""Pure fuzz oracles, also replayable without the Linux mutation engine."""

import copy
import json
from pathlib import Path

from native_backup.contract import (
    REVIEWED_DEPENDENCIES,
    REVIEWED_PROFILES,
    REVIEWED_PYTHON,
    source_digest,
    validate_snapshot,
)
from native_backup.dump_format import known_dump, validate_metadata


def recorder_metadata(data: bytes) -> None:
    try:
        metadata = json.loads(data)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return
    original = copy.copy(metadata)
    try:
        validate_metadata(metadata)
    except ValueError:
        assert metadata is original or metadata == original  # nosec B101 - mutation oracle
        return
    assert metadata is original or metadata == original  # nosec B101 - mutation oracle
    assert known_dump(metadata["file"])  # nosec B101 - filename boundary
    assert Path(metadata["file"]).name == metadata["file"]  # nosec B101
    assert type(metadata["format"]) is int  # nosec B101 - bool must fail
    for field in ("compressed_bytes", "uncompressed_bytes"):
        assert type(metadata[field]) is int and metadata[field] > 0  # nosec B101
    # Accepted manifests must stop qualifying when identity, location, byte
    # counts or the table inventory is invalidated independently.
    for change in (
        {"sha256": "g" * 64},
        {"file": "../outside.sql.xz"},
        {"format": True},
        {"compressed_bytes": 0},
        {"uncompressed_bytes": True},
        {"tables": []},
    ):
        try:
            validate_metadata({**metadata, **change})
        except ValueError:
            continue
        raise AssertionError("Invalidated recorder manifest still qualifies")


def runtime_contract(data: bytes) -> None:
    try:
        mutation = json.loads(data)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return
    if not isinstance(mutation, dict):
        return
    index = mutation.get("profile", 0)
    if type(index) is not int:
        return
    version, profile = tuple(REVIEWED_PROFILES.items())[index % len(REVIEWED_PROFILES)]
    fingerprints = profile["files"].copy()
    dependencies = REVIEWED_DEPENDENCIES.copy()
    python_minor = REVIEWED_PYTHON
    operation = mutation.get("operation")
    key = mutation.get("key", "synthetic")
    value = mutation.get("value", "changed")
    if not isinstance(key, str) or not isinstance(value, str):
        return
    if operation == "source":
        fingerprints[key] = value
    elif operation == "remove":
        fingerprints.pop(key, None)
    elif operation == "dependency":
        dependencies[key] = value
    elif operation == "python":
        python_minor = (0, index)
    elif operation == "prerelease":
        version += value or "b1"
    before = (fingerprints.copy(), dependencies.copy())
    try:
        result = validate_snapshot(version, fingerprints, dependencies, python_minor)
    except RuntimeError:
        # Every unmodified reviewed profile is a required positive control.
        if operation == "unchanged":
            raise
        return
    assert (fingerprints, dependencies) == before  # nosec B101 - mutation oracle
    assert dependencies == REVIEWED_DEPENDENCIES  # nosec B101 - review boundary
    assert python_minor == REVIEWED_PYTHON  # nosec B101 - runtime boundary
    assert fingerprints in [item["files"] for item in REVIEWED_PROFILES.values()]  # nosec B101
    assert result["source_sha256"] == source_digest(fingerprints)  # nosec B101
    assert result["allowed"] is True  # nosec B101
    # A known version must never excuse a missing source file.
    fingerprints.pop(next(iter(fingerprints)))
    try:
        validate_snapshot(version, fingerprints, dependencies, python_minor)
    except RuntimeError:
        return
    raise AssertionError("Incomplete reviewed source inventory still qualifies")
