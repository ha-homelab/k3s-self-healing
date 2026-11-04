# Vendored release toolkit; change the toolkit source, then render again.
# ruff: noqa
# mypy: ignore-errors
# pylint: skip-file
# fmt: off
"""Exercise the publication parser against this checkout's real release notes."""

import base64
import hashlib
import json
import sys
import unittest
from importlib import import_module
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
client = import_module("scripts.release")
release_control = import_module("scripts.release_control")

# No repository API is contacted: the adapter binds the current checkout bytes.
SOURCE = "a" * 40


class CheckoutGitHub:
    """Provide only the two source reads; any mutation or other read fails."""

    def __init__(self):
        self.repo = json.loads((ROOT / ".release-policy.json").read_text())["repository"]
        self.calls = []

    def api(self, path, method="GET", body=None):
        """Apply the real parser's content and Git blob checks to local bytes."""
        allowed = {
            f"contents/{name}?ref={SOURCE}": name
            for name in (".release-policy.json", "CHANGELOG.md")
        }
        if method != "GET" or body is not None or path not in allowed:
            raise AssertionError(f"Unexpected release API operation: {method} {path}")
        self.calls.append(path)
        name = allowed[path]
        raw = (ROOT / name).read_bytes()
        return {
            "type": "file", "path": name, "encoding": "base64",
            "content": base64.b64encode(raw).decode(), "size": len(raw),
            "sha": hashlib.sha1(
                b"blob " + str(len(raw)).encode() + b"\0" + raw,
                usedforsecurity=False,
            ).hexdigest(),
        }


class CheckoutReleaseNotesTests(unittest.TestCase):
    """Fail review CI when the committed version has unpublishable notes."""

    def test_current_version_has_publishable_source_bound_notes(self):
        """Every tag form must select the same reviewed base-version section."""
        policy = json.loads((ROOT / ".release-policy.json").read_text())
        if policy.get("release_notes") is None:
            self.skipTest("This repository does not configure changelog release notes")
        if not (policy.get("version") or policy.get("version_file")):
            self.skipTest(
                "No committed version selection; prepare validates notes after "
                "resolving the manual or tag-derived base"
            )
        base = client.resolve_version(policy)
        for suffix in ("-nightly.20261027000000.1.1", "-beta.1", "-rc.1", ""):
            with self.subTest(tag=f"v{base}{suffix}"):
                github = CheckoutGitHub()
                notes = release_control.release_notes(
                    github, f"v{base}{suffix}", SOURCE, "Offline checkout validation."
                )
                self.assertEqual(github.calls, [
                    f"contents/.release-policy.json?ref={SOURCE}",
                    f"contents/CHANGELOG.md?ref={SOURCE}",
                ])
                self.assertIn(f"## Changes in {base}", notes)
                self.assertIn("Offline checkout validation.", notes)
