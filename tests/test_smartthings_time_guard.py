"""The null guard preserves valid readings and does not guess future source layouts."""

from datetime import time
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "smartthings_time_guard", ROOT / "helm/smartthings-time-guard/patch.py"
)
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)
SOURCE = "class SmartThingsDnDTime:\n" + GUARD.BEFORE + "\n"


def entity_class(source):
    namespace = {"time": time, "override": lambda method: method,
                 "Capability": SimpleNamespace(CUSTOM_DO_NOT_DISTURB_MODE="dnd")}
    exec(source, namespace)  # pylint: disable=exec-used
    return namespace["SmartThingsDnDTime"]


class SmartThingsTimeGuardTests(unittest.TestCase):
    def test_missing_start_and_end_time_return_unknown(self):
        source, status = GUARD.patched_source(SOURCE)
        self.assertEqual(status, "patched")
        for attribute in ["startTime", "endTime"]:
            entity = entity_class(source)()
            entity.entity_description = SimpleNamespace(attribute=attribute)
            entity.get_attribute_value = lambda *_: None
            self.assertIsNone(entity.native_value)

    def test_present_values_keep_existing_conversion_and_recover_after_none(self):
        original = entity_class(SOURCE)()
        patched = entity_class(GUARD.patched_source(SOURCE)[0])()
        for entity in [original, patched]:
            entity.entity_description = SimpleNamespace(attribute="startTime")
        for value in ["0000", "0800", "2305", "23:45", "00:00", "09:15"]:
            with self.subTest(value=value):
                original.get_attribute_value = lambda *_: value
                patched.get_attribute_value = lambda *_: None
                self.assertIsNone(patched.native_value)
                patched.get_attribute_value = lambda *_: value
                self.assertEqual(patched.native_value, original.native_value)

    def test_second_start_is_idempotent(self):
        patched, _ = GUARD.patched_source(SOURCE)
        self.assertEqual(GUARD.patched_source(patched), (patched, "already-patched"))

    def test_different_upstream_getter_is_not_replaced(self):
        changed = SOURCE.replace("state[:2]", "state[0:2]")
        self.assertEqual(GUARD.patched_source(changed), (changed, "unsupported"))
        renamed = SOURCE.replace("SmartThingsDnDTime", "AnotherTime")
        self.assertEqual(GUARD.patched_source(renamed), (renamed, "unsupported"))

    def test_other_methods_and_classes_are_preserved(self):
        unrelated = "\nclass AnotherTime:\n" + GUARD.BEFORE + "\n"
        patched, status = GUARD.patched_source(SOURCE + unrelated)
        self.assertEqual(status, "patched")
        self.assertTrue(patched.endswith(unrelated))

    def test_file_replacement_preserves_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "time.py"
            target.write_text(SOURCE)
            target.chmod(0o640)
            self.assertEqual(GUARD.patch_file(target), "patched")
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            self.assertEqual(GUARD.patch_file(target), "already-patched")
            self.assertEqual(list(Path(folder).iterdir()), [target])
