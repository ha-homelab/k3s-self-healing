"""Offline recovery orchestration contracts; Kubernetes is always a fixture."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from disaster_recovery import runner, archive


class StubKube:
    """Only registered reads are possible; unexpected requests fail the test."""

    def __init__(self, objects=None, collections=None):
        self.objects = objects or {}
        self.collections = collections or {}
        self.calls = []

    def get(self, endpoint):
        self.calls.append(("get", endpoint))
        return copy.deepcopy(self.objects[endpoint])

    def list(self, endpoint):
        self.calls.append(("list", endpoint))
        return copy.deepcopy(self.collections[endpoint])


def collection(kind, items, api_version="v1"):
    return {"apiVersion": api_version, "kind": kind + "List", "items": items}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.host = self.base / "host"
        self.nfs = self.base / "nfs"
        self.host.mkdir()
        self.nfs.mkdir()
        self.config = {"node": "fixture-node", "nas_server": "nas.example.invalid",
                       "nas_export": "/volume1/fixture-export"}
        self.item = {"name": "application", "namespace": "homeassistant", "claim": "app-data"}
        self.pvc = {"metadata": {"uid": "claim-uid"}, "status": {"phase": "Bound"},
                    "spec": {"volumeName": "fixture-volume"}}
        self.pv = {
            "metadata": {"name": "fixture-volume", "uid": "volume-uid"},
            "spec": {"claimRef": {"uid": "claim-uid"}, "local": {"path": "/srv/application"},
                     "nodeAffinity": {"required": {"nodeSelectorTerms": [self.node_term(["fixture-node"])]}}},
        }

    @staticmethod
    def node_term(nodes):
        return {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": nodes}]}

    def claim_api(self):
        return StubKube(objects={
            "/api/v1/namespaces/homeassistant/persistentvolumeclaims/app-data": self.pvc,
            "/api/v1/persistentvolumes/fixture-volume": self.pv,
        })

    def resolve(self):
        return runner.resolve_claim(self.claim_api(), self.item, self.config, self.host, self.nfs)

    def test_component_uses_only_three_attempts_and_one_shared_deadline(self):
        work = self.base / 'work';work.mkdir()
        changed = archive.SourceChangedError('Source changed: configuration.yaml')
        with patch.object(archive.time, 'monotonic', return_value=100.0), \
             patch.object(archive, '_create_archive_attempt', side_effect=changed) as attempts, \
             patch.object(runner, 'create_archive', wraps=archive.create_archive) as operation:
            with self.assertRaises(archive.SourceChangedError):
                runner.archive_component(self.host, work, 'home-assistant', {'type': 'fixture'})
        self.assertEqual(operation.call_count, 1)
        self.assertEqual(operation.call_args.kwargs['timeout_seconds'], 900)
        self.assertEqual(attempts.call_count, 3)
        self.assertEqual([call.args[3] for call in attempts.call_args_list], [1000.0] * 3)
        diagnostic = work / 'home-assistant-source-change.private.json'
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)
        self.assertIn('configuration.yaml', json.loads(diagnostic.read_text())['cause'])
        self.assertFalse((work / 'home-assistant.tar.gz').exists())

    def test_claim_identity_is_preserved_and_stale_binding_is_rejected(self):
        path, receipt = self.resolve()
        self.assertEqual(path, self.host / "srv/application")
        self.assertEqual(receipt["claim_uid"], "claim-uid")
        self.assertEqual(receipt["pv_uid"], "volume-uid")
        self.assertEqual(receipt["location"], {"type": "local", "node": "fixture-node", "path": "/srv/application"})
        self.pv["spec"]["claimRef"]["uid"] = "previous-claim-uid"
        with self.assertRaises(RuntimeError):
            self.resolve()
        self.pv["spec"]["claimRef"]["uid"] = "claim-uid"
        self.pvc["status"]["phase"] = "Pending"
        with self.assertRaises(RuntimeError):
            self.resolve()

    def test_local_volume_must_be_exclusive_to_the_expected_node(self):
        unsafe_terms = [
            [],
            [self.node_term(["another-node"])],
            [self.node_term(["fixture-node", "another-node"])],
            [self.node_term(["fixture-node"]), self.node_term(["another-node"])],
            [self.node_term(["fixture-node"]), {"matchExpressions": [
                {"key": "disk", "operator": "In", "values": ["ssd"]}]}],
        ]
        for terms in unsafe_terms:
            with self.subTest(terms=terms):
                self.pv["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"] = terms
                with self.assertRaises(RuntimeError):
                    self.resolve()

    def test_nfs_claim_stays_on_the_reviewed_server_and_export(self):
        del self.pv["spec"]["local"]
        self.pv["spec"]["nfs"] = {"server": self.config["nas_server"],
                                   "path": self.config["nas_export"] + "/application"}
        self.assertEqual(self.resolve()[0], self.nfs / "application")
        unsafe_sources = [
            {"server": "other-nas.example.invalid", "path": "/volume1/fixture-export/application"},
            {"server": self.config["nas_server"], "path": "/volume1/fixture-export-other/application"},
            {"server": self.config["nas_server"], "path": "/volume1/fixture-export/../outside"},
        ]
        for source in unsafe_sources:
            with self.subTest(source=source):
                self.pv["spec"]["nfs"] = source
                with self.assertRaises((RuntimeError, ValueError)):
                    self.resolve()

    def test_source_mapping_rejects_parent_and_symlink_escape(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.host / "escape").symlink_to(outside, target_is_directory=True)
        (self.host / "inside").mkdir()
        (self.host / "safe-alias").symlink_to("inside", target_is_directory=True)
        self.assertEqual(runner.mapped_source(self.host, "/safe-alias/settings"), self.host / "safe-alias/settings")
        for path in ("relative/path", "/../outside", "/inside/../../outside", "/escape/settings"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    runner.mapped_source(self.host, path)

    def test_retention_preserves_latest_successes_and_unowned_content(self):
        root = self.base / "snapshots"
        root.mkdir()

        def snapshot(name, manifest):
            folder = root / name
            folder.mkdir()
            (folder / "manifest.json").write_text(json.dumps(manifest))
            (folder / "sentinel").write_text(name)
            return folder

        completed = {"profile": runner.PROFILE, "completed": True}
        obsolete = snapshot("20260910T040000Z-aaaaaaaa", completed)
        retained = [snapshot("20260914T040000Z-bbbbbbbb", completed),
                    snapshot("20260915T040000Z-cccccccc", completed)]
        untouched = [
            snapshot("native-home-assistant-backup", completed),
            snapshot("20260901T040000Z-dddddddd", {"profile": "someone-else", "completed": True}),
            snapshot("20260902T040000Z-eeeeeeee", {"profile": runner.PROFILE, "completed": False}),
            snapshot(".incomplete-20260903T040000Z-ffffffff", completed),
            snapshot("20260904T040000Z-11111111", {"profile": runner.PROFILE, "completed": "true"}),
            snapshot("20260907T040000Z-44444444", []),
            snapshot("20260908T040000Z-55555555", None),
        ]
        malformed = snapshot("20260905T040000Z-22222222", {})
        (malformed / "manifest.json").write_text("interrupted JSON {")
        untouched.append(malformed)
        outside = self.base / "native-backup"
        outside.mkdir()
        (outside / "manifest.json").write_text(json.dumps(completed))
        linked = root / "20260906T040000Z-33333333"
        linked.symlink_to(outside, target_is_directory=True)

        runner.cleanup_completed(root, 2)

        self.assertFalse(obsolete.exists())
        for folder in retained + untouched:
            self.assertEqual((folder / "sentinel").read_text(), folder.name)
        self.assertTrue(linked.is_symlink())
        self.assertTrue((outside / "manifest.json").is_file())

    def test_invalid_retention_never_deletes_a_snapshot(self):
        root = self.base / "snapshots"
        root.mkdir()
        sentinel = root / "must-survive"
        sentinel.write_text("native backup")
        for keep in (True, False, 0, 1, -1, "2", 2.0, None):
            with self.subTest(keep=keep):
                with self.assertRaises(ValueError):
                    runner.cleanup_completed(root, keep)
                self.assertEqual(sentinel.read_text(), "native backup")

    def test_portable_metadata_is_clean_without_changing_the_raw_object(self):
        original = {
            "kind": "Secret", "metadata": {
                "name": "fixture-secret", "namespace": "homeassistant", "uid": "runtime-uid",
                "resourceVersion": "19", "generation": 3, "creationTimestamp": "old",
                "deletionTimestamp": "old", "managedFields": [{"manager": "controller"}],
                "selfLink": "old", "ownerReferences": [{"uid": "old-controller"}],
                "labels": {"application": "fixture"},
                "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "stale",
                                "example.invalid/retain": "desired"},
            }, "status": {"observedGeneration": 3}, "data": {"fixture": "c3ludGhldGlj"},
        }
        before = copy.deepcopy(original)
        portable = runner.portable_object(original)
        self.assertEqual(original, before)
        self.assertEqual(portable, {
            "kind": "Secret", "metadata": {"name": "fixture-secret", "namespace": "homeassistant",
                "labels": {"application": "fixture"}, "annotations": {"example.invalid/retain": "desired"}},
            "data": {"fixture": "c3ludGhldGlj"},
        })

    def test_portable_export_cannot_start_application_controllers(self):
        def obj(name, spec, namespace="homeassistant"):
            return {"metadata": {"name": name, "namespace": namespace, "uid": "runtime-" + name},
                    "spec": spec, "status": {"fixture": "runtime"}}

        collections = {
            "/apis/apiextensions.k8s.io/v1/customresourcedefinitions": collection("CustomResourceDefinition", []),
            "/apis/apps/v1/deployments": collection("Deployment", [obj("web", {"replicas": 3}),
                obj("default-replicas", {}), obj("other-namespace", {"replicas": 2}, "unrelated")], "apps/v1"),
            "/apis/apps/v1/statefulsets": collection("StatefulSet", [obj("ha", {"replicas": 1})], "apps/v1"),
            "/apis/apps/v1/daemonsets": collection("DaemonSet", [obj("agent", {"template": {"spec": {"containers": []}}})], "apps/v1"),
            "/apis/batch/v1/cronjobs": collection("CronJob", [obj("periodic", {"suspend": False})], "batch/v1"),
            "/apis/batch/v1/jobs": collection("Job", [obj("previous-job", {})], "batch/v1"),
            "/api/v1/pods": collection("Pod", [obj("previous-pod", {})]),
        }
        api = StubKube(objects={"/version": {"gitVersion": "fixture"}}, collections=collections)
        folder = self.base / "export"
        config = {"api_collections": list(collections), "namespaces": ["homeassistant"]}

        inventory = runner.export_kubernetes(api, folder, config)

        portable = json.loads((folder / "application-objects-stopped.json").read_text())["items"]
        by_name = {item["metadata"]["name"]: item for item in portable}
        self.assertEqual(set(by_name), {"web", "default-replicas", "ha", "periodic"})
        for name, replicas in (("web", 3), ("default-replicas", 1), ("ha", 1)):
            self.assertEqual(by_name[name]["spec"]["replicas"], 0)
            self.assertEqual(by_name[name]["metadata"]["annotations"]["ha-homelab.io/restore-replicas"], str(replicas))
        self.assertTrue(by_name["periodic"]["spec"]["suspend"])
        for item in portable:
            self.assertNotIn("status", item)
            self.assertNotIn("uid", item["metadata"])
        raw = json.loads((folder / inventory["/apis/apps/v1/deployments"]["file"]).read_text())
        self.assertEqual(raw["items"][0]["spec"]["replicas"], 3)
        self.assertEqual(raw["items"][0]["metadata"]["uid"], "runtime-web")
        raw_daemonsets = json.loads((folder / inventory["/apis/apps/v1/daemonsets"]["file"]).read_text())
        self.assertEqual(len(raw_daemonsets["items"]), 1)
        for file in folder.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)

    def test_json_temporary_file_cannot_modify_an_outside_symlink_target(self):
        folder = self.base / "snapshots"
        folder.mkdir()
        outside = self.base / "outside-sentinel"
        outside.write_text("untouched")
        outside.chmod(0o640)
        (folder / "latest-success.json.new").symlink_to(outside)
        try:
            runner.write_json(folder / "latest-success.json", {"state": "complete"})
        except (OSError, ValueError):
            pass  # Refusing a stale temporary path is also a safe outcome.
        self.assertEqual(outside.read_text(), "untouched")
        self.assertEqual(outside.stat().st_mode & 0o777, 0o640)
        if (folder / "latest-success.json").exists():
            self.assertFalse((folder / "latest-success.json").is_symlink())
            self.assertEqual(json.loads((folder / "latest-success.json").read_text()), {"state": "complete"})

    def test_symlink_backup_root_is_rejected_before_any_kubernetes_action(self):
        outside = self.base / "outside"
        outside.mkdir(mode=0o750)
        outside.chmod(0o750)  # Other runner tests may intentionally leave umask 077.
        root = self.base / "snapshots"
        root.symlink_to(outside, target_is_directory=True)
        api = StubKube()
        previous_umask = os.umask(0o077)
        try:
            with self.assertRaises(ValueError):
                runner.run({}, root, host=self.host, nfs=self.nfs, api=api)
        finally:
            os.umask(previous_umask)
        self.assertEqual(api.calls, [])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(outside.stat().st_mode & 0o777, 0o750)


if __name__ == "__main__":
    unittest.main()
