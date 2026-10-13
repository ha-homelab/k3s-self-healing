"""Private approval API and MQTT status for the independently running Core updater."""
from __future__ import annotations

import asyncio
import fcntl
import hmac
import json
import os
from pathlib import Path
import time

from aiohttp import web
import paho.mqtt.client as mqtt

from model import ACTIVE, Store, public_state
from runtime import Runtime, image_for, release
from worker import run


class Publisher:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ha-core-updater", clean_session=True)
        self.client.username_pw_set(Path("/credentials/mqtt_username").read_text().strip(),
                                    Path("/credentials/mqtt_password").read_text().strip())
        self.client.will_set("homelab/ha-updater/availability", "offline", qos=1, retain=True)
        self.client.on_connect = self.connected
        self.client.on_message = self.message

    def connected(self, client, userdata, flags, reason, properties):
        if not reason.is_failure:
            client.subscribe("homeassistant/status", qos=1)
            self.publish()

    def message(self, client, userdata, message):
        if message.topic == "homeassistant/status" and message.payload == b"online":
            self.publish()

    def publish(self):
        d = self.store.read()
        discovery = {
            "name": "Home Assistant Core", "unique_id": "ha_core_k3s_approved_update",
            "default_entity_id": "update.home_assistant_core",
            "state_topic": "homelab/ha-updater/state", "json_attributes_topic": "homelab/ha-updater/attributes",
            "availability_topic": "homelab/ha-updater/availability",
            "entity_category": "config", "icon": "mdi:home-assistant",
            "device": {"identifiers": ["ha_core_updater"], "name": "Home Assistant updates",
                "manufacturer": "ha-homelab", "model": "Approved k3s Core updates"},
        }
        # Deliberately no MQTT command_topic: broker publishers cannot approve deployments.
        self.client.publish("homeassistant/update/ha_core_updater/config", json.dumps(discovery), qos=1, retain=True)
        if d.get("installed_version"):
            state = {"installed_version": d["installed_version"],
                "latest_version": d.get("latest_version") or d["installed_version"],
                "title": "Home Assistant Core", "in_progress": d["stage"] in ACTIVE,
                "release_url": d.get("release_url") or "https://www.home-assistant.io/blog/",
                "release_summary": "Stage: " + d["stage"] + ". Approval and progress: the Core updates dashboard."}
            self.client.publish("homelab/ha-updater/state", json.dumps(state), qos=1, retain=True)
        self.client.publish("homelab/ha-updater/attributes", json.dumps(public_state(d)), qos=1, retain=True)
        self.client.publish("homelab/ha-updater/availability", "online", qos=1, retain=True)

    def start(self):
        self.client.connect_async(self.settings.get("mqtt_host", "mosquitto.homeassistant.svc.cluster.local"), 1883, 30)
        self.client.loop_start()


def create_app(store, runtime, settings, token, worker=run):
    if len(token) < 32:
        raise ValueError("A strong approval token is required")
    app = web.Application(client_max_size=2048)
    app["tasks"] = set()

    def authorized(request):
        return hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token)

    async def health(request):
        return web.json_response({"status": "ok"})

    async def status(request):
        if not authorized(request):
            raise web.HTTPForbidden()
        return web.json_response(public_state(store.read()))

    async def approve(request):
        if not authorized(request):
            raise web.HTTPForbidden()
        try:
            body = await request.json()
            accepted = store.approve(body)
        except (ValueError, TypeError, json.JSONDecodeError):
            return web.json_response({"error": "Invalid approval request"}, status=422)
        except RuntimeError:
            return web.json_response({"error": "Approval expired, changed or already used"}, status=409)
        task = asyncio.create_task(asyncio.to_thread(worker, store, runtime, settings))
        app["tasks"].add(task)
        task.add_done_callback(app["tasks"].discard)
        return web.json_response({"accepted": True, "candidate_id": accepted["candidate_id"],
                                  "target_version": accepted["target_version"]}, status=202)

    async def reoffer(request):
        if not authorized(request):
            raise web.HTTPForbidden()
        try:
            body = await request.json()
            failed = store.reoffer_snapshot(body)
        except (ValueError, TypeError, json.JSONDecodeError):
            return web.json_response({"error": "Invalid re-offer request"}, status=422)
        except RuntimeError:
            return web.json_response({"error": "Failed update is not eligible for re-offer"}, status=409)
        try:
            await asyncio.to_thread(runtime.validate_reoffer, failed)
            offered = store.reoffer(body, failed)
        except Exception:
            return web.json_response({"error": "Re-offer safety checks failed or state changed; inspect status and recorded jobs"}, status=409)
        # Creating an offer is not approval. Only a later /approve request with
        # this new candidate ID can start the worker.
        return web.json_response({"offered": True, "candidate_id": offered["candidate_id"],
                                  "target_version": offered["target_version"]}, status=200)

    app.router.add_get("/healthz", health)
    app.router.add_get("/status", status)
    app.router.add_post("/approve", approve)
    app.router.add_post("/reoffer", reoffer)
    return app


async def supervise(app):
    store, rt, settings = app["store"], app["runtime"], app["settings"]
    publisher = Publisher(store, settings)
    publisher.start()
    last_check = float("-inf")
    while True:
        if time.monotonic() - last_check > settings.get("check_interval_seconds", 6 * 3600):
            last_check = time.monotonic()
            if store.read()["stage"] not in ACTIVE | {"recovery_required"}:
                try:
                    installed = await asyncio.to_thread(rt.healthy)
                    target, url = await asyncio.to_thread(release)
                    image = await asyncio.to_thread(image_for, target)
                    base_image = await asyncio.to_thread(rt.image)
                    store.offer(installed, target, image, base_image, url)
                    store.update(check_error="")
                except Exception as error:
                    # Never put authenticated URLs or raw server output on MQTT.
                    store.update(check_error=type(error).__name__)
                    print(json.dumps({"event": "version_check_failed", "error_type": type(error).__name__}), flush=True)
        publisher.publish()
        await asyncio.sleep(5)


async def start_background(app):
    app["supervisor"] = asyncio.create_task(supervise(app))


async def stop_background(app):
    app["supervisor"].cancel()
    try:
        await app["supervisor"]
    except asyncio.CancelledError:
        pass


def recover_interrupted(store):
    previous = store.read()
    unfinished_schedule = (previous.get("backup_schedule_was_suspended") is not None
                           and previous.get("backup_schedule_restored") is False)
    if previous["stage"] in ACTIVE or unfinished_schedule:
        # Jobs and Helm can outlive the controller. Never replay an approval.
        store.update(stage="recovery_required", last_error="Updater restarted with an unfinished operation or backup schedule restoration; inspect the recorded jobs and Helm release before recovery")


def main():
    os.umask(0o077)
    Path("/state").mkdir(exist_ok=True)
    lock = open("/state/worker.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    settings = json.loads(Path(os.environ.get("CONFIG_PATH", "/app/settings.json")).read_text())
    store = Store("/state/status.json")
    recover_interrupted(store)
    runtime = Runtime(settings)
    app = create_app(store, runtime, settings, Path("/credentials/approval_token").read_text().strip())
    app["store"], app["runtime"], app["settings"] = store, runtime, settings
    app.on_startup.append(start_background)
    app.on_cleanup.append(stop_background)
    web.run_app(app, host="0.0.0.0", port=8099, access_log=None)


if __name__ == "__main__":
    main()
