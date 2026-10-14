"""Check the actual project notes before a candidate reaches publication."""

import base64
import hashlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import release_control as release
import version_plan


class ProjectReleaseNotesTests(unittest.TestCase):
    def test_current_base_has_publishable_source_bound_notes(self):
        policy = json.loads((ROOT / ".release-policy.json").read_text())
        version = version_plan.read_base_version(ROOT, policy)
        source = "a" * 40
        raw = (ROOT / "CHANGELOG.md").read_bytes()
        response = {
            "type": "file",
            "path": "CHANGELOG.md",
            "encoding": "base64",
            "content": base64.b64encode(raw).decode(),
            "size": len(raw),
            "sha": hashlib.sha1(
                b"blob " + str(len(raw)).encode() + b"\0" + raw,
                usedforsecurity=False,
            ).hexdigest(),
        }
        github = Mock()
        github.api.return_value = response
        with patch.object(release, "source_policy_snapshot", return_value={"data": policy}):
            for channel in ("beta", "rc"):
                body = release.release_notes(
                    github, f"v{version}-{channel}.1", source, "Synthetic provenance"
                )
                self.assertIn("### Upgrade", body)
                self.assertIn("### Security", body)
                github.api.assert_called_with(f"contents/CHANGELOG.md?ref={source}")
        self.assertEqual(github.api.call_count, 2)


if __name__ == "__main__":
    unittest.main()
