"""Guard the privileged callers' immutable metadata-only boundary."""
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PIN = "c3d39182d5a6a33dfff6c890e8f05f27957c3bf7"


class MetadataAutomationTests(unittest.TestCase):
    def load(self, name):
        return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)

    def test_callers_cannot_execute_source_or_inherit_credentials(self):
        for filename, job, shared in [
            ("auto-approve.yml", "auto-approve", "auto-approve-reusable.yml"),
            ("auto-merge.yml", "auto-merge", "auto-merge.yml"),
        ]:
            with self.subTest(filename=filename):
                workflow = self.load(filename)
                self.assertEqual(set(workflow["jobs"]), {job})
                value = workflow["jobs"][job]
                self.assertNotIn("steps", value)
                self.assertNotIn("runs-on", value)
                self.assertEqual(value["uses"], f"victron-venus/venus-os-ci-toolkit/.github/workflows/{shared}@{PIN}")
                self.assertEqual(value["secrets"], {"BOT_PAT": "${{ secrets.BOT_PAT }}"})
                self.assertEqual(workflow["permissions"]["contents"], "read")
                self.assertNotIn("pull_request", workflow["on"])

    def test_approval_uses_default_branch_definition_and_scoped_guard(self):
        workflow = self.load("auto-approve.yml")
        self.assertEqual(set(workflow["on"]), {"pull_request_target"})
        job = workflow["jobs"]["auto-approve"]
        self.assertEqual([part.strip() for part in job["if"].split("&&")], [
            "github.event.pull_request.head.repo.full_name == github.repository",
            "github.event.pull_request.base.ref == github.event.repository.default_branch",
            "contains(fromJSON('[\"4alvit\",\"californiantiramisu\",\"dependabot[bot]\",\"renovate[bot]\"]'), github.event.pull_request.user.login)",
            "(contains(github.event.pull_request.labels.*.name, 'auto-approve') || contains(github.event.pull_request.labels.*.name, 'automerge'))",
            "!github.event.pull_request.draft",
        ])

    def test_merge_retains_independent_review_and_named_ci(self):
        workflow = self.load("auto-merge.yml")
        inputs = workflow["jobs"]["auto-merge"]["with"]
        self.assertEqual(inputs["reviewed-merge"], "true")
        self.assertEqual(inputs["reviewed-single-pass"], "true")
        self.assertNotIn("native-auto-merge", inputs)
        self.assertEqual(inputs["required-status-checks"], "CI gate")
        self.assertEqual(inputs["allow-dependabot-actions-review"], "true")
        self.assertEqual(inputs["allow-owned-bot-actions-review"], "true")
        self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "false")
        self.assertEqual(workflow["on"]["workflow_run"]["types"], ["completed"])
        self.assertIn("Quality gate", workflow["on"]["workflow_run"]["workflows"])
        self.assertIn("schedule", workflow["on"])


if __name__ == "__main__":
    unittest.main()
