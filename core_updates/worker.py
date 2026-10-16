"""Execute an already-approved immutable Core update independently of Home Assistant."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
from urllib.request import urlopen

import yaml

from model import IMAGE, utc
from runtime import BackupCapacityError, command, terminal_job_state


def image_tag(image):
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise ValueError("Unexpected approved image reference")
    return image.split("home-assistant:", 1)[1]


def mapping_value(node, name):
    if not isinstance(node, yaml.MappingNode):
        raise RuntimeError("Unexpected backup manifest structure")
    matches = [value for key, value in node.value if key.value == name]
    if len(matches) != 1:
        raise RuntimeError("Missing or ambiguous backup manifest field")
    return matches[0]


def pin_backup_manifest(text, image):
    """Change only two named HA image scalars, retaining comments and anchors."""
    documents = list(yaml.compose_all(text))
    cronjobs = [node for node in documents if isinstance(node, yaml.MappingNode)
                and mapping_value(node, "kind").value == "CronJob"
                and mapping_value(mapping_value(node, "metadata"), "name").value == "ha-recorder-native-backup"]
    if len(cronjobs) != 1:
        raise RuntimeError("Expected one native backup CronJob in source")
    pod = cronjobs[0]
    for part in ("spec", "jobTemplate", "spec", "template", "spec"):
        pod = mapping_value(pod, part)
    changes = []
    for section, name in (("initContainers", "prepare"), ("containers", "native-backup")):
        containers = mapping_value(pod, section)
        if not isinstance(containers, yaml.SequenceNode):
            raise RuntimeError("Unexpected backup container list")
        found = [node for node in containers.value if mapping_value(node, "name").value == name]
        if len(found) != 1:
            raise RuntimeError("Expected exactly one " + name + " container")
        scalar = mapping_value(found[0], "image")
        if not isinstance(scalar, yaml.ScalarNode) or not scalar.value.startswith(("ghcr.io/home-assistant/home-assistant:", "ghcr.io/home-assistant/home-assistant@sha256:")):
            raise RuntimeError("Unexpected native backup image repository")
        changes.append((scalar.start_mark.index, scalar.end_mark.index))
    for start, end in sorted(changes, reverse=True):
        text = text[:start] + json.dumps(image) + text[end:]
    return text


class Source:
    def __init__(self, settings):
        self.settings = settings
        self.path = Path("/state/repository")
        self.approval_path = Path("/state/approvals")
        self.env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
            GIT_SSH_COMMAND="ssh -i /credentials/ssh-privatekey -o IdentitiesOnly=yes -o UserKnownHostsFile=/credentials/known_hosts -o StrictHostKeyChecking=yes",
            GIT_AUTHOR_NAME="Home Assistant updater", GIT_COMMITTER_NAME="Home Assistant updater",
            GIT_AUTHOR_EMAIL="ha-core-updater@users.noreply.github.com",
            GIT_COMMITTER_EMAIL="ha-core-updater@users.noreply.github.com")

    def git(self, *args):
        return command(["git", *args], cwd=self.path, env=self.env).strip()

    def prepare(self):
        remote = self.settings["repository"]
        # The administrator explicitly selects a deployment repository. Never
        # accept option-like paths, local transports, or an example destination.
        if (not isinstance(remote, str)
                or not re.fullmatch(r"git@github\.com:[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+\.git", remote)
                or remote.startswith("git@github.com:example/")):
            raise RuntimeError("Configure an explicit GitHub SSH deployment repository")
        if not self.path.exists():
            command(["git", "clone", "--no-tags", remote, str(self.path)], env=self.env)
        if self.git("remote", "get-url", "origin") != remote or self.git("status", "--porcelain"):
            raise RuntimeError("Deployment checkout is not clean or has an unexpected origin")
        self.git("fetch", "--no-tags", "origin", "main")
        self.git("checkout", "-B", "approved-runtime", "origin/main")
        return self.git("rev-parse", "HEAD")

    def pin(self, image, approval):
        """Create a local source commit; the caller must journal intent before push."""
        path = self.path / "helm/values-pajikos.yaml"
        text = path.read_text()
        parsed = yaml.safe_load(text)
        if parsed["image"]["repository"] != "ghcr.io/home-assistant/home-assistant":
            raise RuntimeError("Unexpected Helm image repository")
        tag = image_tag(image)
        block = re.search(r"(?ms)^image:\n(?:(?:[ \t]+[^\n]*|)\n)+", text)
        if not block:
            raise RuntimeError("Cannot locate Helm image values")
        replacement, changes = re.subn(r"(?m)^  tag:.*$", "  tag: " + json.dumps(tag), block.group())
        if changes != 1:
            raise RuntimeError("Ambiguous Helm image tag")
        values = text[:block.start()] + replacement + text[block.end():]
        backup_path = self.path / "native_backup/cronjob.yaml"
        backup = pin_backup_manifest(backup_path.read_text(), image)
        if not re.fullmatch(r"[a-f0-9]{32}", approval["candidate_id"]):
            raise ValueError("Invalid approval identifier")
        self._record_approval(approval)
        path.write_text(values)
        backup_path.write_text(backup)
        self.git("add", "helm/values-pajikos.yaml", "native_backup/cronjob.yaml")
        self.git("commit", "-m", "Approve Home Assistant " + approval["target_version"])
        return self.git("rev-parse", "HEAD")

    def _record_approval(self, approval):
        """Persist an immutable private receipt before any source-file mutation."""
        payload = (json.dumps({k: approval[k] for k in (
            "candidate_id", "target_version", "image", "base_image", "approved_at", "backup_id")}, indent=2) + "\n").encode()
        directory = self.approval_path
        if directory.resolve().is_relative_to(self.path.resolve()):
            raise RuntimeError("Approval receipts must stay outside the source checkout")
        directory.mkdir(mode=0o700, exist_ok=True)
        parent = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary = ".receipt-" + secrets.token_hex(16)
        created = False
        try:
            info = os.fstat(descriptor)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise RuntimeError("Approval receipt directory must be private and owned by the updater")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=descriptor)
            created = True
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Linking publishes the complete receipt without replacing an old one.
            os.link(temporary, approval["candidate_id"] + ".json",
                    src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
            os.fsync(descriptor)
        finally:
            if created:
                os.unlink(temporary, dir_fd=descriptor)
            os.close(descriptor)

    def push(self, commit):
        if self.git("rev-parse", "HEAD") != commit:
            raise RuntimeError("Local source commit changed before push")
        # Normal fast-forward only. A branch rule or concurrent writer blocks deployment.
        # The caller treats every exception as an ambiguous externally visible write.
        self.git("push", "origin", commit + ":main")


def chart_file(settings):
    path = Path("/state/chart.tgz")
    expected = settings["chart_sha256"]
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
        return path
    with urlopen(settings["chart_url"], timeout=60) as response:
        data = response.read(2 * 1024**2 + 1)
    if len(data) > 2 * 1024**2 or hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError("Pinned Helm chart checksum mismatch")
    path.write_bytes(data)
    return path


def helm_env(rt):
    # This kubeconfig references the rotating, namespace-scoped ServiceAccount token.
    path = Path("/state/kubeconfig")
    d = {"apiVersion": "v1", "kind": "Config", "current-context": "updater",
        "clusters": [{"name": "cluster", "cluster": {"server": rt.kube,
            "certificate-authority": str(rt.sa / "ca.crt")}}],
        "users": [{"name": "worker", "user": {"tokenFile": str(rt.sa / "token")}}],
        "contexts": [{"name": "updater", "context": {"cluster": "cluster", "user": "worker", "namespace": rt.namespace}}]}
    path.write_text(yaml.safe_dump(d))
    path.chmod(0o600)
    return dict(os.environ, KUBECONFIG=str(path), HELM_CACHE_HOME="/state/helm/cache",
                HELM_CONFIG_HOME="/state/helm/config", HELM_DATA_HOME="/state/helm/data")


def run(store, rt, settings):
    approval = store.read()
    identity = approval["candidate_id"][:12]
    source = Source(settings)
    backup_job = "ha-recorder-core-" + identity
    preflight_job = "ha-core-preflight-" + identity
    old_cron = None
    schedule_suspend_attempted = False
    backup_create_attempted = False
    backup_terminal = False
    backup_job_uid = None
    source_push_attempted = False
    helm_started = False
    backup_image_verified = False
    outcome = {"stage": "failed", "last_error": "Update did not complete"}
    try:
        image_tag(approval["image"])
        rt.wait_update_visible(approval["candidate_id"])
        if rt.healthy(approval["installed_version"]) != approval["installed_version"] or rt.image() != approval["base_image"]:
            raise RuntimeError("Running deployment changed after this update was offered")
        rt.maintenance_idle()
        source.prepare()
        chart = chart_file(settings)
        env = helm_env(rt)
        history = json.loads(command(["/tools/helm", "history", "home-assistant", "-n", rt.namespace, "-o", "json"], env=env))
        if not history or history[-1]["status"] != "deployed" or history[-1]["chart"] != "home-assistant-" + settings["chart_version"]:
            raise RuntimeError("Helm release is busy or the chart changed")
        old_revision = str(history[-1]["revision"])

        cron = rt.get("cronjobs", "ha-recorder-native-backup")
        old_cron = cron["spec"].get("suspend", False)
        if not isinstance(old_cron, bool):
            raise RuntimeError("Unexpected backup schedule suspension value")
        # Journal the original schedule and exact job name BEFORE changing the
        # CronJob. A crash must never lose the information required to recover.
        store.update(stage="backup", backup_schedule_was_suspended=old_cron,
                     backup_job=backup_job, backup_create_attempted=False,
                     backup_schedule_restored=False, previous_helm_revision=old_revision)
        schedule_suspend_attempted = True
        rt.request(rt.path("cronjobs", "ha-recorder-native-backup"), "PATCH",
                   {"spec": {"suspend": True}, "metadata": {"resourceVersion": cron["metadata"]["resourceVersion"]}})
        rt.maintenance_idle()
        backup_spec = rt.backup_spec(approval["candidate_id"])
        backup_start = datetime.now(timezone.utc)
        store.update(backup_create_attempted=True, backup_started_at=backup_start.isoformat())
        backup_create_attempted = True
        created = rt.job(backup_job, backup_spec)
        backup_job_uid = created["metadata"]["uid"]
        store.update(backup_job_uid=backup_job_uid)
        try:
            finished = rt.wait_job(backup_job, 7500)
        except Exception:
            diagnostic = rt.backup_failure(backup_job, backup_job_uid, approval["candidate_id"], backup_start)
            if diagnostic:
                raise BackupCapacityError(diagnostic) from None
            raise
        if finished.get("metadata", {}).get("uid") != backup_job_uid or terminal_job_state(finished) != "complete":
            raise RuntimeError("Backup completion is not confirmed")
        backup_terminal = True
        store.update(backup_job_terminal="complete")
        receipt = json.loads(Path("/ha-config/backups/recorder-native-receipt.json").read_text())
        if receipt.get("update_run_id") != approval["candidate_id"]:
            raise RuntimeError("Native backup receipt belongs to a different update run")
        date = datetime.fromisoformat(receipt["date"].replace("Z", "+00:00"))
        if not receipt.get("backup_id") or date.tzinfo is None or date < backup_start:
            raise RuntimeError("A fresh exact native backup receipt was not found")
        if not receipt.get("agents"):
            raise RuntimeError("No confirmed backup destination")
        approval = store.update(backup_id=receipt["backup_id"])

        # A native export can take tens of minutes. Validate the target against
        # a NEW configuration copy after that backup, not an earlier snapshot.
        rt.maintenance_idle()
        rt.healthy(approval["installed_version"])
        if rt.image() != approval["base_image"]:
            raise RuntimeError("Deployment changed while backup was running")
        store.update(stage="preflight", preflight_job=preflight_job)
        rt.preflight(preflight_job, approval["image"])
        rt.maintenance_idle()
        rt.healthy(approval["installed_version"])
        if rt.image() != approval["base_image"]:
            raise RuntimeError("Deployment changed during target preflight")

        # Refresh immediately before committing; never override another writer.
        source_base = source.prepare()
        store.update(source_mutation_intent={"base_commit": source_base,
                     "image": approval["image"], "candidate_id": approval["candidate_id"]})
        source_commit = source.pin(approval["image"], approval)
        # A push may succeed remotely and then time out locally. Persist its
        # exact intent before starting it and require recovery on ANY exception.
        store.update(source_commit=source_commit, source_push_attempted=True,
                     source_push_status="pending")
        source_push_attempted = True
        source.push(source_commit)
        store.update(source_push_status="confirmed")

        # Journal before invoking Helm. A timeout is not proof that HA never
        # started or migrated data; neither Helm rollback nor a source revert
        # is safe automatically after this point.
        store.update(stage="deploying", helm_upgrade_attempted=True)
        helm_started = True
        tag = image_tag(approval["image"])
        command(["/tools/helm", "upgrade", "home-assistant", str(chart), "-n", rt.namespace,
            "--reuse-values", "--set-string", "image.tag=" + tag, "--wait", "--timeout", "30m"], env=env, timeout=1830)
        store.update(stage="verifying")
        deadline = time.monotonic() + settings.get("health_timeout_seconds", 900)
        consecutive = 0
        while time.monotonic() < deadline:
            try:
                rt.healthy(approval["target_version"])
                if rt.image() != approval["image"]:
                    raise RuntimeError("Deployed image differs from approval")
                consecutive += 1
                if consecutive >= 3:
                    break
            except Exception:
                consecutive = 0
            time.sleep(15)
        else:
            raise RuntimeError("New HA or required integrations did not pass the health checks")
        rt.update_backup_image(approval["image"])
        backup_image_verified = True
        store.update(backup_image_verified=True)
        outcome = {"stage": "succeeded", "installed_version": approval["target_version"], "last_error": ""}
    except Exception as error:
        if isinstance(error, BackupCapacityError):
            store.update(backup_failure=error.diagnostic)
        message = str(error) if isinstance(error, (RuntimeError, ValueError)) else type(error).__name__
        stage = "failed"
        if helm_started:
            stage = "recovery_required"
            message += "; target may have migrated data; automatic downgrade was refused"
        elif source_push_attempted:
            stage = "recovery_required"
            message += "; inspect the recorded source commit and remote main; no automatic source revert"
        outcome = {"stage": stage, "last_error": message[:350]}
        # Keep the active stage while cleanup runs: a crash here must enter
        # recovery on startup, and HA must not announce premature completion.
        store.update(last_error=message[:350])
    finally:
        if schedule_suspend_attempted:
            if backup_create_attempted and not backup_terminal:
                # Failed waits and create timeouts leave a potentially live Job.
                # Even a missing Job after an ambiguous POST is not proof that
                # its pods never ran, so only a final condition permits resume.
                try:
                    job = rt.get("jobs", backup_job)
                    terminal = terminal_job_state(job)
                    if backup_job_uid is not None and job.get("metadata", {}).get("uid") != backup_job_uid:
                        terminal = None
                    backup_terminal = terminal in {"complete", "failed"}
                    if backup_terminal:
                        store.update(backup_job_terminal=terminal)
                except Exception:
                    backup_terminal = False
            if backup_create_attempted and not backup_terminal:
                outcome = {"stage": "recovery_required",
                           "last_error": "Backup Job status is unknown or active; weekly backup remains suspended. Inspect the recorded Job before recovery."}
            elif helm_started and not backup_image_verified:
                outcome = {"stage": "recovery_required",
                           "last_error": "Core upgrade outcome or backup image is unverified; weekly backup remains suspended. No automatic downgrade was attempted."}
            else:
                try:
                    current = rt.get("cronjobs", "ha-recorder-native-backup")
                    rt.request(rt.path("cronjobs", "ha-recorder-native-backup"), "PATCH",
                               {"spec": {"suspend": old_cron}, "metadata": {"resourceVersion": current["metadata"]["resourceVersion"]}})
                    actual = rt.get("cronjobs", "ha-recorder-native-backup")["spec"].get("suspend", False)
                    if actual != old_cron:
                        raise RuntimeError("Native backup schedule readback did not match")
                    store.update(backup_schedule_restored=True)
                except Exception:
                    outcome = {"stage": "recovery_required",
                               "last_error": "Could not confirm the restored native backup schedule; inspect the CronJob"}
        store.update(**outcome, completed_at=utc())
