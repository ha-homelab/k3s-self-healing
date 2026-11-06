"""Keep main-push and release qualification complete when PR CI skips docs."""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class ReleaseScopeContract(unittest.TestCase):
    def workflow(self):
        return yaml.safe_load(
            (ROOT / ".github/workflows/release-pipeline.yml").read_text()
        )

    def test_release_entry_always_requires_full_validation(self):
        classifiers = [
            step
            for step in self.workflow()["jobs"]["scope"]["steps"]
            if step.get("id") == "scope"
        ]
        self.assertEqual(len(classifiers), 1, "Expected one release scope classifier")
        self.assertEqual(classifiers[0]["env"]["FORCE_FULL"], "true")

    def test_nested_quality_always_requires_full_validation(self):
        self.assertIs(self.workflow()["jobs"]["checks"]["with"]["force-full"], True)


if __name__ == "__main__":
    unittest.main()
