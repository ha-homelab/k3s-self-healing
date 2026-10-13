"""Durable, single-use approvals for Home Assistant Container updates."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

ACTIVE = {"preparing", "preflight", "backup", "deploying", "verifying"}
VERSION = re.compile(r"20\d{2}\.(?:[1-9]|1[0-2])\.(?:0|[1-9]\d*)\Z")
IMAGE = re.compile(r"ghcr\.io/home-assistant/home-assistant:([\d.]+)@sha256:[a-f0-9]{64}\Z")
RUN_FIELDS = (
    "source_mutation_intent", "source_commit", "source_push_attempted", "source_push_status",
    "helm_upgrade_attempted", "backup_job_uid", "backup_create_attempted", "backup_started_at",
    "backup_job_terminal", "backup_schedule_was_suspended", "backup_schedule_restored",
    "backup_image_verified", "previous_helm_revision", "backup_job", "preflight_job", "backup_failure")
ATTEMPT_SUMMARY_FIELDS = (
    "candidate_id", "target_version", "stage", "approved_at", "completed_at", "last_error",
    "backup_job", "preflight_job", "backup_id", "backup_failure")


def version_tuple(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ValueError("A stable Home Assistant version is required")
    return tuple(map(int, value.split(".")))


def utc():
    return datetime.now(timezone.utc).isoformat()


def validate_failed_reoffer(d):
    """Only proven pre-deployment failures can receive a new approval offer."""
    if d.get("stage") != "failed" or not d.get("approved_at"):
        raise RuntimeError("Only a previously approved, failed update can be re-offered")
    if (any(d.get(key) is not None for key in ("source_mutation_intent", "source_commit", "source_push_status"))
            or any(d.get(key) is not None and d.get(key) is not False for key in ("source_push_attempted", "helm_upgrade_attempted"))):
        raise RuntimeError("Source or deployment mutation requires manual recovery")
    suspended = d.get("backup_schedule_was_suspended")
    if suspended is not None and (not isinstance(suspended, bool) or d.get("backup_schedule_restored") is not True):
        raise RuntimeError("Backup schedule restoration is not confirmed")
    created = d.get("backup_create_attempted")
    if created is not None and not isinstance(created, bool):
        raise RuntimeError("Backup creation state is invalid")
    if created is True:
        if (suspended is None or not d.get("backup_job") or not d.get("backup_job_uid")
                or d.get("backup_job_terminal") not in {"complete", "failed"}):
            raise RuntimeError("Backup completion is not confirmed")
    elif d.get("backup_job_uid") or d.get("backup_job_terminal"):
        raise RuntimeError("Backup creation state is inconsistent")
    version_tuple(d.get("installed_version"))
    version_tuple(d.get("target_version"))
    if not isinstance(d.get("base_image"), str) or not d["base_image"]:
        raise RuntimeError("Previous running image is unknown")
    match = IMAGE.fullmatch(d.get("image") or "")
    if not match or match.group(1) != d["target_version"] or version_tuple(d["target_version"]) <= version_tuple(d["installed_version"]):
        raise RuntimeError("Failed update image or version is invalid")


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {
            "stage": "idle", "last_error": "", "installed_version": None,
            "latest_version": None, "candidate_id": "", "target_version": "",
        }

    def read(self):
        with self.lock:
            return deepcopy(self.data)

    def update(self, **changes):
        with self.lock:
            result = deepcopy(self.data)
            result.update(changes, updated_at=utc())
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.path.with_suffix(".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(result, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.data = result
            return deepcopy(result)

    def offer(self, installed, target, image, base_image, release_url, lifetime=7 * 86400):
        version_tuple(installed)
        version_tuple(target)
        if not IMAGE.fullmatch(image) or IMAGE.fullmatch(image).group(1) != target:
            raise ValueError("Unexpected image reference")
        with self.lock:
            old = self.data
            if old["stage"] in ACTIVE | {"recovery_required", "failed"}:
                return self.read()
            if version_tuple(target) <= version_tuple(installed):
                if old["stage"] == "succeeded" and old.get("target_version") == installed:
                    # Keep a durable result long enough for HA to reconnect and notify.
                    return self.update(installed_version=installed, latest_version=target)
                return self.update(installed_version=installed, latest_version=target,
                                   stage="idle", candidate_id="", target_version="", last_error="")
            if (old.get("target_version") == target and old.get("image") == image
                    and old.get("installed_version") == installed
                    and old.get("base_image") == base_image
                    and old.get("expires_at", 0) > time.time()):
                # A failed attempt is never silently rearmed by discovery.
                return self.read()
            return self.update(stage="available", installed_version=installed,
                latest_version=target, target_version=target, image=image,
                base_image=base_image, release_url=release_url,
                candidate_id=secrets.token_hex(16), expires_at=time.time() + lifetime,
                last_error="", approved_at=None, completed_at=None, backup_id=None,
                **{key: None for key in RUN_FIELDS})

    def reoffer_snapshot(self, body):
        if not isinstance(body, dict) or set(body) != {"action", "target_version", "candidate_id"}:
            raise ValueError("Expected action, target_version and candidate_id only")
        if body["action"] != "reoffer":
            raise ValueError("Unsupported action")
        version_tuple(body["target_version"])
        if not isinstance(body["candidate_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", body["candidate_id"]):
            raise ValueError("Invalid candidate identifier")
        with self.lock:
            validate_failed_reoffer(self.data)
            if any(body[key] != self.data.get(key) for key in ("candidate_id", "target_version")):
                raise RuntimeError("Failed update identity changed")
            return self.read()

    def reoffer(self, body, verified, lifetime=7 * 86400):
        """CAS after live checks; never consume or replay an install approval."""
        with self.lock:
            current = self.reoffer_snapshot(body)
            if current != verified:
                raise RuntimeError("Failed update changed during recovery checks")
            previous = {key: current.get(key) for key in ATTEMPT_SUMMARY_FIELDS}
            return self.update(stage="available", candidate_id=secrets.token_hex(16),
                expires_at=time.time() + lifetime, approved_at=None, completed_at=None,
                last_error="", backup_id=None, previous_attempt=previous,
                **{key: None for key in RUN_FIELDS})

    def approve(self, body, now=None):
        now = time.time() if now is None else now
        if not isinstance(body, dict) or set(body) != {"action", "target_version", "candidate_id"}:
            raise ValueError("Expected action, target_version and candidate_id only")
        if body["action"] != "install":
            raise ValueError("Unsupported action")
        version_tuple(body["target_version"])
        if not isinstance(body["candidate_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", body["candidate_id"]):
            raise ValueError("Invalid candidate identifier")
        with self.lock:
            d = self.data
            if (d["stage"] != "available" or d.get("expires_at", 0) <= now
                    or body["candidate_id"] != d.get("candidate_id")
                    or body["target_version"] != d.get("target_version")):
                raise RuntimeError("Approval is stale, expired, already used, or unavailable")
            # fsync before acknowledging HTTP 202. Repeated requests cannot start another worker.
            return self.update(stage="preparing", approved_at=utc(), last_error="")


def public_state(d):
    return {key: d.get(key) for key in (
        "stage", "installed_version", "latest_version", "candidate_id", "target_version",
        "expires_at", "release_url", "last_error", "updated_at", "approved_at",
        "completed_at", "backup_id", "source_commit", "backup_job", "preflight_job", "check_error",
        "backup_failure", "previous_attempt")}
