"""Keep main-push and release qualification complete when PR CI skips docs."""

import importlib.util
import re
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


class RuntimeScopeContract(unittest.TestCase):
    def setUp(self):
        self.quality = yaml.safe_load(
            (ROOT / ".github/workflows/quality-gate.yml").read_text()
        )
        self.validation = yaml.safe_load(
            (ROOT / ".github/workflows/validate.yml").read_text()
        )

    def test_runtime_defaults_to_full_validation(self):
        setting = self.validation["on"]["workflow_call"]["inputs"][
            "run-home-assistant-runtime"
        ]
        self.assertEqual(setting["type"], "boolean")
        self.assertIs(setting["default"], True)
        self.assertEqual(
            self.validation["jobs"]["home-assistant"]["if"],
            "${{ inputs.run-home-assistant-runtime }}",
        )

    def test_source_security_and_required_caller_remain_unconditional(self):
        self.assertNotIn("if", self.quality["jobs"]["check-0"])
        self.assertNotIn("if", self.quality["jobs"]["check-1"])
        source = self.validation["jobs"]["source"]
        self.assertNotIn("if", source)
        self.assertTrue(source["steps"])
        self.assertTrue(all("if" not in step for step in source["steps"]))
        self.assertTrue(
            any("gitleaks" in step.get("run", "") for step in source["steps"])
        )

    def route(self, run, reason):
        # Evaluate the actual limited OR/inequality expression, without eval.
        expression = self.quality["jobs"]["check-0"]["with"][
            "run-home-assistant-runtime"
        ]
        self.assertTrue(expression.startswith("${{ ") and expression.endswith(" }}"))
        values = {"run": run, "reason": reason}
        terms = expression[4:-3].split(" || ")
        self.assertEqual(len(terms), 2)
        comparisons = []
        for term in terms:
            match = re.fullmatch(
                r"needs\.scope\.outputs\.(run|reason) != '([^']*)'", term
            )
            self.assertIsNotNone(match, "Runtime caller must remain fail-closed")
            comparisons.append(values[match[1]] != match[2])
        return any(comparisons)

    def test_only_proven_docs_scope_can_skip_runtime(self):
        for run, reason, wanted in (
            ("false", "documentation-only", False),
            ("true", "non-documentation-change", True),
            ("true", "forced", True),
            ("true", "unsupported-event", True),
            ("false", "git-error", True),
            ("", "", True),
            ("unknown", "documentation-only", True),
        ):
            with self.subTest(run=run, reason=reason):
                self.assertIs(self.route(run, reason), wanted)

    def test_actual_classifier_results_select_runtime(self):
        spec = importlib.util.spec_from_file_location(
            "runtime_change_scope", ROOT / "scripts/change_scope.py"
        )
        classifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(classifier)
        for path, wanted in (("docs/guide.md", False), ("source.py", True)):
            raw = (
                ":100644 100644 " + "a" * 40 + " " + "b" * 40 + " M\0" + path + "\0"
            ).encode()
            result = classifier.classify_diff(raw, frozenset(), frozenset())
            self.assertIs(
                self.route(str(result["run"]).lower(), result["reason"]), wanted
            )
        for event, force in (
            ("workflow_dispatch", False),
            ("schedule", False),
            ("unknown", False),
            ("push", True),
        ):
            result = classifier.classify(ROOT, event, None, force=force)
            self.assertTrue(self.route(str(result["run"]).lower(), result["reason"]))


if __name__ == "__main__":
    unittest.main()
