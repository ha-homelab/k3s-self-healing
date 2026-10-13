"""Deployment storage gates use subprocess fixtures, never a Kubernetes cluster."""

from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml


spec = importlib.util.spec_from_file_location(
    "deploy_recovery", Path(__file__).resolve().parents[1] / "scripts/deploy-disaster-recovery.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "disaster_recovery").mkdir()
        self.profile = {"nas_server": "nas.example.invalid", "nas_export": "/volume1/fixture"}
        (self.root / "disaster_recovery/config.json").write_text(json.dumps(self.profile))
        from site_profile import DEFAULTS
        site = dict(DEFAULTS, node='test-node', lan_node='test-lan',
                    node_ipv4='198.18.0.10', nas_ipv4='198.18.0.20',
                    database_ipv4='198.18.0.30', recovery_storage_class='fixture-nfs')
        (self.root / 'site.json').write_text(json.dumps(site))
        self.claim = {
            "metadata": {"name": deploy.CLAIM, "namespace": deploy.NAMESPACE, "uid": "claim-uid"},
            "status": {"phase": "Bound"},
            "spec": {"volumeName": "backup-volume", "storageClassName": "fixture-nfs"},
        }
        self.volume = {
            "metadata": {"name": "backup-volume", "uid": "volume-uid"}, "status": {"phase": "Bound"},
            "spec": {"claimRef": {"name": deploy.CLAIM, "namespace": deploy.NAMESPACE, "uid": "claim-uid"},
                     "storageClassName": "fixture-nfs", "persistentVolumeReclaimPolicy": "Retain",
                     "nfs": {"server": self.profile["nas_server"], "path": "/volume1/fixture/private-backup"}},
        }
        self.objects = [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": deploy.NAMESPACE}},
            {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
             "metadata": {"name": deploy.CLAIM, "namespace": deploy.NAMESPACE},
             "spec": {"storageClassName": "fixture-nfs"}},
            {"apiVersion": "batch/v1", "kind": "CronJob",
             "metadata": {"name": deploy.CRONJOB, "namespace": deploy.NAMESPACE},
             "spec": {"schedule": "15 4 * * *", "jobTemplate": {"spec": {"template": {"spec": {
                 "containers": [{"name": "backup", "env": [{"name": "SOURCE_REVISION", "value": "unset"}]}]
             }}}}}},
        ]
        self.events = []
        self.fail_dry_run = False

    def validate(self, claim=None, volume=None):
        return deploy.validate_output_volume(claim or self.claim, volume or self.volume, self.profile, "fixture-nfs")

    def check_output(self, command, **kwargs):
        self.events.append(("read", command, None))
        if command[:3] == ["git", "status", "--porcelain"]:
            return b""
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return "a" * 40 + "\n"
        if command[:2] == ["kubectl", "kustomize"]:
            return yaml.safe_dump_all(self.objects)
        if "get" in command and "pvc" in command:
            return json.dumps(self.claim).encode()
        if "get" in command and "pv" in command:
            return json.dumps(self.volume).encode()
        raise AssertionError("Unexpected command: " + repr(command))

    def execute(self, command, **kwargs):
        self.events.append(("write", command, kwargs.get("input")))
        if self.fail_dry_run and "--dry-run=server" in command:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)

    def invoke(self):
        with patch.object(deploy, "ROOT", self.root), \
                patch.object(deploy.subprocess, "check_output", side_effect=self.check_output), \
                patch.object(deploy.subprocess, "run", side_effect=self.execute), \
                patch("sys.argv", ["deploy", "--apply", "--run-now", "--context", "test",
                                   "--profile", str(self.root / 'site.json')]), redirect_stdout(io.StringIO()):
            deploy.main()

    def assert_only_suspended_cronjobs_applied(self):
        applied = []
        for kind, command, data in self.events:
            if kind == "write" and "apply" in command and "--dry-run=server" not in command:
                applied.extend(obj for obj in yaml.safe_load_all(data) if obj["kind"] == "CronJob")
        self.assertTrue(applied)
        self.assertTrue(all(obj["spec"]["suspend"] is True for obj in applied))

    def test_verified_dedicated_nfs_directory_is_accepted(self):
        self.assertEqual(self.validate(), "/volume1/fixture/private-backup")

    def test_binding_and_retention_mismatches_are_rejected(self):
        changes = [
            ("claim", ("metadata", "uid"), "recreated-claim"),
            ("claim", ("metadata", "namespace"), "unrelated"),
            ("claim", ("status", "phase"), "Pending"),
            ("claim", ("spec", "storageClassName"), "other-storage"),
            ("volume", ("metadata", "name"), "other-volume"),
            ("volume", ("metadata", "uid"), ""),
            ("volume", ("metadata", "deletionTimestamp"), "2026-09-15T00:00:00Z"),
            ("volume", ("spec", "claimRef", "name"), "other-claim"),
            ("volume", ("spec", "claimRef", "namespace"), "unrelated"),
            ("volume", ("spec", "storageClassName"), "other-storage"),
            ("volume", ("spec", "persistentVolumeReclaimPolicy"), "Delete"),
        ]
        for target, fields, value in changes:
            with self.subTest(target=target, fields=fields):
                copies = {"claim": copy.deepcopy(self.claim), "volume": copy.deepcopy(self.volume)}
                node = copies[target]
                for field in fields[:-1]:
                    node = node[field]
                node[fields[-1]] = value
                with self.assertRaises(ValueError):
                    self.validate(**copies)

    def test_nfs_destination_cannot_escape_or_use_another_server(self):
        for path in ("relative", "/volume1/fixture", "/volume1/fixture-other/backup",
                     "/volume1/fixture/../outside", "/volume1/fixture//backup", "/volume1/fixture/backup/"):
            with self.subTest(path=path):
                volume = copy.deepcopy(self.volume)
                volume["spec"]["nfs"]["path"] = path
                with self.assertRaises(ValueError):
                    self.validate(volume=volume)
        for key, value in (("server", "other-nas.example.invalid"), ("readOnly", True)):
            with self.subTest(key=key):
                volume = copy.deepcopy(self.volume)
                volume["spec"]["nfs"][key] = value
                with self.assertRaises(ValueError):
                    self.validate(volume=volume)

    def test_invalid_bound_volume_leaves_schedule_suspended_and_creates_no_job(self):
        self.volume["spec"]["persistentVolumeReclaimPolicy"] = "Delete"
        with self.assertRaisesRegex(SystemExit, "remains suspended"):
            self.invoke()
        self.assert_only_suspended_cronjobs_applied()
        self.assertFalse(any("patch" in command or "create" in command for _, command, _ in self.events))

    def test_server_validation_failure_already_has_a_suspended_schedule(self):
        self.fail_dry_run = True
        with self.assertRaises(subprocess.CalledProcessError):
            self.invoke()
        self.assert_only_suspended_cronjobs_applied()
        self.assertFalse(any("patch" in command or "create" in command for _, command, _ in self.events))

    def test_manual_job_and_schedule_start_only_after_all_volume_gates(self):
        self.invoke()
        self.assert_only_suspended_cronjobs_applied()
        pv_read = next(i for i, (_, command, _) in enumerate(self.events) if "get" in command and "pv" in command)
        unsuspend = next(i for i, (_, command, _) in enumerate(self.events) if "patch" in command)
        manual_job = next(i for i, (_, command, _) in enumerate(self.events) if "create" in command)
        self.assertLess(pv_read, unsuspend)
        self.assertLess(unsuspend, manual_job)
        patch_command = self.events[unsuspend][1]
        self.assertEqual(json.loads(patch_command[patch_command.index("-p") + 1]), {"spec": {"suspend": False}})


if __name__ == "__main__":
    unittest.main()
