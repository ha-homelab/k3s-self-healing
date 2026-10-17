"""Bounded Kubernetes, Home Assistant and official release clients."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import ssl
import subprocess
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import (HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener)

from model import ACTIVE, version_tuple, validate_failed_reoffer
from preflight import validate_placeholder_directories


class NoRedirect(HTTPRedirectHandler):
    """Never forward administrative credentials to a redirect target."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


async def reject_websocket_redirect(_session, _context, params):
    """Stop the upgrade before contacting a redirected administrative endpoint."""
    params.response.close()
    raise RuntimeError("Administrative WebSocket redirects are not allowed")


def urlopen(request, *, context=None, timeout=30):
    """Open the selected endpoint directly, retaining normal TLS verification."""
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
    try:
        return opener.open(request, timeout=timeout)
    except HTTPError as error:
        error.close()  # Only status/headers are used; do not retain private error bodies.
        raise


class KubernetesError(RuntimeError):
    """Keep the status code without exposing Kubernetes response bodies."""
    def __init__(self, method, status):
        self.status = status
        super().__init__(f"Kubernetes {method} failed with HTTP {status}")


class BackupCapacityError(RuntimeError):
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        gib = 1024**3
        super().__init__("Native backup needs %.2f GiB free; %.2f GiB available; short %.2f GiB. "
                         "The verified SQL export is retained; no Core update was started." %
                         (diagnostic["required_bytes"] / gib, diagnostic["free_bytes"] / gib,
                          diagnostic["shortfall_bytes"] / gib))


def capacity_diagnostic(message, run_id, started_at, job_uid=None):
    """Accept only our bounded numeric report, never arbitrary container text."""
    numbers = {"free_bytes", "reserve_bytes", "measured_config_bytes", "required_bytes",
               "reviewed_ceiling_bytes", "total_bytes", "sql_bytes", "archive_allowance_bytes", "shortfall_bytes"}
    required = numbers | {"schema_version", "code", "observed_at", "sql_export_retained", "update_run_id"}
    try:
        if not isinstance(message, str) or len(message.encode()) > 4096:
            return None
        data = json.loads(message)
        if not isinstance(data, dict) or not required <= set(data) or set(data) - required - {"update_job_uid"}:
            return None
        if (data["schema_version"] != 1 or isinstance(data["schema_version"], bool)
                or data["code"] != "native_backup_capacity" or data["update_run_id"] != run_id
                or data["sql_export_retained"] is not True):
            return None
        if any(not isinstance(data[key], int) or isinstance(data[key], bool) or not 0 <= data[key] < 2**63 for key in numbers):
            return None
        if "update_job_uid" in data:
            import re
            if (not isinstance(data["update_job_uid"], str)
                    or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", data["update_job_uid"])
                    or data["update_job_uid"] != job_uid):
                return None
        observed = datetime.fromisoformat(data["observed_at"].replace("Z", "+00:00"))
        if observed.tzinfo is None or started_at.tzinfo is None or not started_at <= observed <= datetime.now(timezone.utc):
            return None
        if (data["free_bytes"] > data["total_bytes"] or data["reserve_bytes"] < 3 * 1024**3
                or data["required_bytes"] != data["reserve_bytes"] + data["sql_bytes"] + data["archive_allowance_bytes"]
                or data["archive_allowance_bytes"] != (3 * data["measured_config_bytes"] + 1) // 2 + 128 * 1024**2
                or data["shortfall_bytes"] != data["required_bytes"] - data["free_bytes"]
                or data["shortfall_bytes"] <= 0):
            return None
        return {key: data[key] for key in required}
    except (ValueError, TypeError, AttributeError):
        return None


def terminal_job_state(job):
    """Only final Job conditions with no active pods establish completion."""
    status = job.get("status", {})
    if status.get("active", 0):
        return None
    conditions = {item.get("type") for item in status.get("conditions", [])
                  if item.get("status") == "True"}
    if "Complete" in conditions and "Failed" not in conditions:
        return "complete"
    if "Failed" in conditions and "Complete" not in conditions:
        return "failed"
    return None


def backup_containers(pod):
    """The recorder export client must never be replaced by the HA image."""
    prepared = [item for item in pod.get("initContainers", []) if item.get("name") == "prepare"]
    finished = [item for item in pod.get("containers", []) if item.get("name") == "native-backup"]
    if len(prepared) != 1 or len(finished) != 1:
        raise RuntimeError("Native backup must have exactly one prepare and native-backup container")
    return prepared[0], finished[0]


def http_json(url, headers=None, timeout=30):
    with urlopen(Request(url, headers=headers or {}), timeout=timeout) as response:
        return json.load(response)


def release():
    d = http_json("https://api.github.com/repos/home-assistant/core/releases/latest",
                  {"Accept": "application/vnd.github+json", "User-Agent": "ha-approved-core-updater"})
    version_tuple(d["tag_name"])
    if d.get("draft") or d.get("prerelease"):
        raise RuntimeError("Release is not stable")
    expected = "https://github.com/home-assistant/core/releases/tag/" + d["tag_name"]
    if d.get("html_url") != expected:
        raise RuntimeError("Unexpected release URL")
    return d["tag_name"], expected


def image_for(version):
    version_tuple(version)
    token = http_json("https://ghcr.io/token?service=ghcr.io&scope=repository:home-assistant/home-assistant:pull")["token"]
    request = Request("https://ghcr.io/v2/home-assistant/home-assistant/manifests/" + version,
        headers={"Authorization": "Bearer " + token, "Accept": ", ".join([
            "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"])},
        method="HEAD")
    with urlopen(request, timeout=30) as response:
        digest = response.headers["Docker-Content-Digest"]
    import re
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest or ""):
        raise RuntimeError("Registry did not provide an immutable image digest")
    return f"ghcr.io/home-assistant/home-assistant:{version}@{digest}"


class Runtime:
    def __init__(self, settings):
        self.settings = settings
        self.namespace = settings.get("namespace", "homeassistant")
        self.sa = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.ssl = ssl.create_default_context(cafile=str(self.sa / "ca.crt"))
        self.kube = "https://" + os.environ["KUBERNETES_SERVICE_HOST"] + ":" + os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        self.ha_url = settings.get("ha_url", "http://home-assistant.homeassistant.svc.cluster.local:8123").rstrip("/")
        self.ha_token = Path("/ha-api/token").read_text().strip()

    def request(self, path, method="GET", data=None):
        headers = {"Authorization": "Bearer " + (self.sa / "token").read_text().strip()}
        if data is not None:
            headers["Content-Type"] = "application/merge-patch+json" if method == "PATCH" else "application/json"
        request = Request(self.kube + path, data=None if data is None else json.dumps(data).encode(),
                          headers=headers, method=method)
        try:
            with urlopen(request, context=self.ssl, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except HTTPError as error:
            # Kubernetes error bodies can contain rendered objects and secrets.
            raise KubernetesError(method, error.code) from None

    def path(self, kind, name=""):
        roots = {"jobs": "apis/batch/v1", "cronjobs": "apis/batch/v1", "statefulsets": "apis/apps/v1",
                 "pods": "api/v1", "configmaps": "api/v1"}
        return f"/{roots[kind]}/namespaces/{self.namespace}/{kind}" + ("/" + quote(name, safe="") if name else "")

    def get(self, kind, name):
        return self.request(self.path(kind, name))

    def ha(self, path, timeout=30):
        return http_json(self.ha_url + path, {"Authorization": "Bearer " + self.ha_token}, timeout=timeout)

    async def _ha_entries(self):
        import aiohttp
        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(reject_websocket_redirect)
        async with asyncio.timeout(30):
            async with aiohttp.ClientSession(trace_configs=[trace], trust_env=False) as session:
                async with session.ws_connect(self.ha_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket") as ws:
                    await ws.receive_json()
                    await ws.send_json({"type": "auth", "access_token": self.ha_token})
                    if (await ws.receive_json()).get("type") != "auth_ok":
                        raise RuntimeError("HA authentication failed")
                    await ws.send_json({"id": 1, "type": "config_entries/get"})
                    result = await ws.receive_json()
                    if not result.get("success"):
                        raise RuntimeError("HA integration-state query failed")
                    return result["result"]

    def entries(self):
        return asyncio.run(self._ha_entries())

    def healthy(self, expected=None):
        d = self.ha("/api/config")
        version_tuple(d["version"])
        if expected and d["version"] != expected:
            raise RuntimeError("HA has not started the approved version")
        if d.get("state") != "RUNNING":
            raise RuntimeError("HA is still starting")
        entries = self.entries()
        for domain in self.settings.get("required_domains", ["zha", "mqtt"]):
            relevant = [e for e in entries if e.get("domain") == domain and not e.get("disabled_by")]
            if not relevant or any(e.get("state") != "loaded" for e in relevant):
                raise RuntimeError(f"Required integration {domain} is not loaded")
        return d["version"]

    def image(self):
        d = self.get("statefulsets", "home-assistant")
        containers = d["spec"]["template"]["spec"]["containers"]
        return next(c["image"] for c in containers if c["name"] == "home-assistant")

    def wait_update_visible(self, candidate_id, seconds=30):
        """Wait until HA can enforce its side of the mutual maintenance guard."""
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                state = self.ha("/api/states/update.home_assistant_core", timeout=min(5, remaining))
                attrs = state.get("attributes", {})
                if (state.get("state") not in (None, "unknown", "unavailable")
                        and attrs.get("candidate_id") == candidate_id
                        and attrs.get("stage") in ACTIVE
                        and attrs.get("in_progress") is True):
                    return
            except Exception:
                # Missing entity / delayed MQTT discovery / HA startup is not
                # proof of a working guard. Retry within the bounded window.
                pass
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise RuntimeError("HA has not confirmed this Core update's active maintenance guard")

    def maintenance_idle(self):
        states = self.ha("/api/states")
        by_id = {state["entity_id"]: state for state in states}
        if by_id.get("sensor.backup_backup_manager_state", {}).get("state") != "idle":
            raise RuntimeError("HA backup manager is busy or unavailable")
        for name in ("script.hacs_maintenance_backup", "script.hacs_maintenance_install"):
            state = by_id.get(name, {})
            current = state.get("attributes", {}).get("current")
            if state.get("state") != "off" or type(current) is not int or current != 0:
                raise RuntimeError("A required HACS worker is busy or unavailable")
        coordinators = [state for state in states
                        if state["entity_id"].startswith("automation.")
                        and state.get("attributes", {}).get("id") == "ha_homelab_hacs_maintenance_update"]
        if len(coordinators) != 1:
            raise RuntimeError("The required HACS maintenance coordinator is unavailable")
        coordinator = coordinators[0]
        current = coordinator.get("attributes", {}).get("current")
        if coordinator.get("state") not in ("on", "off") or type(current) is not int or current != 0:
            raise RuntimeError("HACS maintenance is running or unavailable")
        for e in states:
            entity = e["entity_id"]
            attrs = e.get("attributes", {})
            progress = attrs.get("in_progress")
            if entity.startswith("update.") and progress is not None and progress is not False:
                # The updater's own progress entity is expected to be active.
                # Numeric zero means an active update at 0%, not False.
                if entity != "update.home_assistant_core":
                    raise RuntimeError("Another update is running")
            if "hacs_maintenance" in entity and entity.startswith(("script.", "automation.")) and attrs.get("current", 0):
                raise RuntimeError("HACS maintenance is running")
        jobs = self.request(self.path("jobs"))["items"]
        if any(j["metadata"]["name"].startswith("ha-recorder-") and terminal_job_state(j) is None for j in jobs):
            raise RuntimeError("Another recorder backup is pending or running")

    def job(self, name, spec):
        body = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name,
            "namespace": self.namespace, "labels": {"app.kubernetes.io/part-of": "ha-core-updater"}}, "spec": spec}
        return self.request(self.path("jobs"), "POST", body)

    def wait_job(self, name, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            d = self.get("jobs", name)
            state = terminal_job_state(d)
            if state == "complete":
                return d
            if state == "failed":
                raise RuntimeError(f"Job {name} failed; inspect its private logs")
            time.sleep(10)
        raise RuntimeError(f"Job {name} exceeded its deadline; do not start a concurrent retry")

    def backup_failure(self, name, uid, run_id, started_at):
        """Read only the exact failed Job's typed termination diagnostic."""
        try:
            job = self.get("jobs", name)
            if job.get("metadata", {}).get("uid") != uid or terminal_job_state(job) != "failed":
                return None
            pods = self.request(self.path("pods") + "?labelSelector=" + quote("job-name=" + name, safe=""))["items"]
            reports = []
            for pod in pods:
                if not any(owner.get("kind") == "Job" and owner.get("name") == name
                           and owner.get("uid") == uid and owner.get("controller") is True
                           for owner in pod.get("metadata", {}).get("ownerReferences", [])):
                    continue
                for container in pod.get("status", {}).get("containerStatuses", []):
                    terminated = container.get("state", {}).get("terminated", {})
                    if container.get("name") != "native-backup" or terminated.get("exitCode", 0) == 0:
                        continue
                    report = capacity_diagnostic(terminated.get("message"), run_id, started_at, uid)
                    if report:
                        reports.append(report)
            return reports[0] if len(reports) == 1 else None
        except Exception:
            # Missing diagnostics must preserve the original failure, not turn
            # unknown Job state into a successful backup or a safe retry.
            return None

    def validate_reoffer(self, failed):
        validate_failed_reoffer(failed)
        self.maintenance_idle()
        if self.healthy(failed["installed_version"]) != failed["installed_version"] or self.image() != failed["base_image"]:
            raise RuntimeError("Running deployment changed after the failed update")
        if failed.get("backup_schedule_was_suspended") is not None:
            cron = self.get("cronjobs", "ha-recorder-native-backup")
            if cron["spec"].get("suspend", False) is not failed["backup_schedule_was_suspended"]:
                raise RuntimeError("Backup schedule no longer matches its restored state")
        if failed.get("backup_create_attempted") is True:
            job = self.get("jobs", failed["backup_job"])
            if (job.get("metadata", {}).get("uid") != failed["backup_job_uid"]
                    or terminal_job_state(job) != failed["backup_job_terminal"]):
                raise RuntimeError("The previous backup Job is not confirmed terminal")
        if failed.get("preflight_job"):
            if terminal_job_state(self.get("jobs", failed["preflight_job"])) is None:
                raise RuntimeError("The previous preflight is not confirmed terminal")

    def backup_spec(self, update_run_id):
        """Prepare a clone without creating it, allowing durable intent first."""
        import re
        if not re.fullmatch(r"[a-f0-9]{32}", update_run_id):
            raise ValueError("Invalid backup update run identifier")
        cj = self.get("cronjobs", "ha-recorder-native-backup")
        spec = deepcopy(cj["spec"]["jobTemplate"]["spec"])
        spec["ttlSecondsAfterFinished"] = 7 * 86400
        pod = spec["template"]["spec"]
        _, final = backup_containers(pod)
        final["env"] = [entry for entry in final.get("env", [])
                        if entry["name"] not in {"HA_UPDATE_RUN_ID", "HA_UPDATE_JOB_UID"}]
        final["env"].append({"name": "HA_UPDATE_RUN_ID", "value": update_run_id})
        pod["automountServiceAccountToken"] = False
        return spec

    def preflight(self, name, image):
        sts = self.get("statefulsets", "home-assistant")
        live = sts["spec"]["template"]["spec"]
        pod = {"restartPolicy": "Never", "automountServiceAccountToken": False,
            "nodeSelector": deepcopy(live.get("nodeSelector", {})),
            "securityContext": {"runAsUser": 0, "runAsGroup": 0,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "volumes": [
                {"name": "source", "persistentVolumeClaim": {"claimName": self.settings.get("config_pvc", "home-assistant-home-assistant-0"), "readOnly": True}},
                {"name": "work", "emptyDir": {"sizeLimit": "4Gi"}},
                {"name": "code", "configMap": {"name": "ha-core-updater-code"}}],
            "containers": [{"name": "preflight", "image": image, "command": ["python", "/app/preflight.py"],
                "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"], "add": ["DAC_READ_SEARCH"]}},
                "env": [{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
                "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "1", "memory": "1536Mi"}},
                "volumeMounts": [{"name": "source", "mountPath": "/ha-source", "readOnly": True},
                    {"name": "work", "mountPath": "/work"}, {"name": "code", "mountPath": "/app", "readOnly": True}]}]}
        subpath = self.settings.get("config_subpath", "")
        if subpath:
            if Path(subpath).is_absolute() or ".." in Path(subpath).parts:
                raise RuntimeError("Preflight config subPath escapes the PVC")
            pod["containers"][0]["volumeMounts"][0]["subPath"] = subpath
        # Exercise the exact deployed compatibility hooks in a disposable target image.
        mounts = next(c for c in live["containers"] if c["name"] == "home-assistant").get("volumeMounts", [])
        placeholders = validate_placeholder_directories(self.settings.get("preflight_empty_dirs", []), [mount.get("mountPath") for mount in mounts])
        pod["containers"][0]["env"].append({"name": "HA_PREFLIGHT_PLACEHOLDERS", "value": json.dumps(placeholders)})
        hooks = [m for m in mounts if m.get("mountPath", "").startswith("/etc/cont-init.d/")]
        for mount in hooks:
            volume = next(v for v in live.get("volumes", []) if v["name"] == mount["name"])
            if "configMap" not in volume:
                raise RuntimeError("Unsupported startup-hook source")
            if not any(v["name"] == volume["name"] for v in pod["volumes"]):
                pod["volumes"].append(deepcopy(volume))
            pod["containers"][0]["volumeMounts"].append(deepcopy(mount))
        self.job(name, {"backoffLimit": 0, "activeDeadlineSeconds": 1800, "ttlSecondsAfterFinished": 7 * 86400,
            "template": {"metadata": {"labels": {"app.kubernetes.io/name": "ha-core-preflight"}}, "spec": pod}})
        self.wait_job(name, 1830)

    def update_backup_image(self, image):
        cj = self.get("cronjobs", "ha-recorder-native-backup")
        spec = deepcopy(cj["spec"])
        pod = spec["jobTemplate"]["spec"]["template"]["spec"]
        for c in backup_containers(pod):
            c["image"] = image
        self.request(self.path("cronjobs", "ha-recorder-native-backup"), "PATCH", {"spec": spec,
            "metadata": {"resourceVersion": cj["metadata"]["resourceVersion"]}})
        actual = self.get("cronjobs", "ha-recorder-native-backup")["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        if any(c["image"] != image for c in backup_containers(actual)):
            raise RuntimeError("Native backup image update was not confirmed")


def command(args, cwd=None, timeout=120, env=None):
    result = subprocess.run(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        # Never expose git/Helm rendered values or private URLs in status/notifications.
        raise RuntimeError(f"{Path(args[0]).name} command failed (exit {result.returncode})")
    return result.stdout
