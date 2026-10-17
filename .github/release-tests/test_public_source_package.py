"""Retain public-source boundaries while producing actual versioned candidates."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tools"))
import package_release
import stage_release_assets
import verify_source
import version_plan
import version_receipt


class PublicSourcePackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="public-source-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in json.loads((ROOT / "publication.json").read_text())["files"]:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        self.command("git", "init", "-q")
        self.command("git", "add", ".")
        self.command(
            "git",
            "-c",
            "user.name=Source fixture",
            "-c",
            "user.email=source@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Reviewed source fixture",
        )
        self.policy = json.loads((self.root / ".release-policy.json").read_text())
        self.base_version = version_plan.read_base_version(ROOT, self.policy)

    def command(self, *args):
        return subprocess.check_output(
            args, cwd=self.root, text=True, stderr=subprocess.STDOUT
        )

    def freeze(self, channel):
        (self.root / "SOURCE_VERSION").write_text(self.base_version + "\n")
        plan = version_plan.create_plan(
            self.base_version,
            channel,
            7,
            self.command("git", "rev-parse", "HEAD").strip(),
            self.policy,
            build_number=7,
        )
        (self.root / ".release-plan.json").write_text(json.dumps(plan))
        self.command(
            sys.executable,
            "scripts/version_plan.py",
            "sync",
            "--plan",
            ".release-plan.json",
        )
        return plan

    def test_legacy_clean_archive_is_deterministic_and_rejects_dirty_source(self):
        first = package_release.build(self.root, self.root / "build/first.tar.gz")
        second = package_release.build(self.root, self.root / "build/second.tar.gz")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        (self.root / "README.md").write_text("unreviewed change\n")
        with self.assertRaisesRegex(ValueError, "Commit reviewed source"):
            package_release.require_source(self.root)

    def test_local_ha_source_edits_package_and_verify_without_network(self):
        maintained_files = (
            "homeassistant/packages/core_updates.yaml",
            "homeassistant/packages/hacs_maintenance.yaml",
            "homeassistant/blueprints/motion_light_off.yaml",
            "runtime_tests/test_core_updates.py",
            "runtime_tests/test_hacs_maintenance.py",
        )
        edited = {}
        for name in maintained_files:
            path = self.root / name
            edited[name] = path.read_bytes() + b"\n# Reviewed local source edit.\n"
            path.write_bytes(edited[name])
        self.command("git", "add", *maintained_files)
        self.command(
            "git", "-c", "user.name=Source fixture",
            "-c", "user.email=source@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-qm",
            "Maintain the reusable HA source locally",
        )

        # Child Python validators inherit this guard, including archive replay.
        with tempfile.TemporaryDirectory(prefix="source-network-guard-") as guard:
            Path(guard, "sitecustomize.py").write_text(
                "import socket\n"
                "def reject_network(*args, **kwargs):\n"
                "    raise AssertionError('Source acceptance attempted network access')\n"
                "socket.socket.connect = reject_network\n"
                "socket.socket.connect_ex = reject_network\n"
                "socket.create_connection = reject_network\n"
                "socket.getaddrinfo = reject_network\n"
            )
            with patch.dict(os.environ, {"PYTHONPATH": guard}):
                self.command(
                    sys.executable, "-c",
                    "import socket; assert socket.socket.connect.__name__ == 'reject_network'",
                )
                archive = package_release.build(
                    self.root, self.root / "build/locally-maintained.tar.gz"
                )
                verify_source.verify(archive, self.root)

        with tarfile.open(archive) as package:
            for name, expected in edited.items():
                with self.subTest(path=name):
                    stream = package.extractfile("k3s-self-healing/" + name)
                    self.assertIsNotNone(stream)
                    self.assertEqual(stream.read(), expected)

    def test_real_beta_and_rc_archives_have_verifiable_receipts(self):
        for channel in ("beta", "rc"):
            with self.subTest(channel=channel):
                plan = self.freeze(channel)
                version = version_plan.projections(plan)["package"]
                output = self.root / "release-dist" / channel
                archive = package_release.build(
                    self.root,
                    output / f"k3s-self-healing-v{version}.tar.gz",
                    self.base_version,
                    channel,
                )
                staged = stage_release_assets.stage(
                    self.root, channel, [f"release-dist/{channel}/*"]
                )
                inventory = [
                    {
                        "name": p.name,
                        "size": p.stat().st_size,
                        "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                    }
                    for p in sorted(staged.iterdir())
                ]
                version_receipt.verify_receipts(staged, plan, inventory, self.policy)
                version_receipt.verify_declared_artifacts(staged, self.policy, plan)
                if channel == "rc":
                    verify_source.verify(archive, self.root)

    def test_frozen_overlay_rejects_unrelated_changes_and_untracked_source(self):
        self.freeze("beta")
        readme = self.root / "README.md"
        previous = readme.read_bytes()
        readme.write_text("unreviewed change\n")
        with self.assertRaisesRegex(ValueError, "Only the frozen"):
            package_release.require_source(self.root, self.base_version, "beta")
        readme.write_bytes(previous)
        (self.root / "unreviewed.txt").write_text("synthetic untracked source")
        with self.assertRaisesRegex(ValueError, "Only the frozen"):
            package_release.require_source(self.root, self.base_version, "beta")

    def test_candidate_requires_frozen_plan(self):
        with self.assertRaisesRegex(ValueError, "frozen release plan"):
            package_release.require_source(self.root, self.base_version, "rc")


if __name__ == "__main__":
    unittest.main()
