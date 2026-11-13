"""Local enrollment, MQTT and command lifecycle, independent of HA's frontend."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from aiohttp import web

from . import pki
from .broker import AMQTT_VERSION, CowayBroker
from .const import (
    DEFAULT_PREFILTER_CYCLE,
    MODEL,
    PAIRING_SECONDS,
    PROVISIONER_URL,
    REPLIES,
    SERVICE_CODE,
    SETTINGS,
    SETUP_FORMAT,
)
from .naming import decode_bluetooth_name
from .telemetry import decode_report

_LOGGER = logging.getLogger(__name__)
MAC = re.compile(r"[0-9A-F]{12}\Z")


def local_serial(mac: str) -> str:
    """Stable 18-character protocol identity, independent of the product label."""
    if not isinstance(mac, str) or not MAC.fullmatch(mac):
        raise ValueError("Invalid Wi-Fi MAC address")
    return "LOCAL0" + mac


def valid_prefilter_cycle(value):
    return type(value) is int and value in SETTINGS["prefilter_cycle"][2].values()


@dataclass(frozen=True)
class ServerConfig:
    host: str
    http_port: int = 8080
    https_port: int = 9443
    mqtt_port: int = 9883
    bind_host: str = "0.0.0.0"
    provisioner_url: str = PROVISIONER_URL

    def validate(self):
        address = ipaddress.IPv4Address(self.host)
        if address.is_unspecified or address.is_multicast:
            raise ValueError("Use the LAN IPv4 address reachable by the fan")
        ipaddress.IPv4Address(self.bind_host)
        ports = (self.http_port, self.https_port, self.mqtt_port)
        if len(set(ports)) != 3 or any(type(p) is not int or not 1024 <= p <= 65535 for p in ports):
            raise ValueError("Choose three different TCP ports between 1024 and 65535")
        url = urlsplit(self.provisioner_url)
        if not url.hostname or url.username or url.password or url.fragment or url.query:
            raise ValueError("Use a plain HTTPS provisioning page URL")
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1")
        ):
            raise ValueError("The provisioning page must use HTTPS (or localhost)")
        return self


@dataclass
class Pairing:
    serial: str | None = None
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    expires: float = field(default_factory=lambda: time.time() + PAIRING_SECONDS)
    complete: bool = False
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    connected: bool = False
    mac: str | None = None
    download: str | None = None


@dataclass
class DeviceState:
    online: bool = False
    last_seen: float | None = None
    settings: dict = field(default_factory=dict)
    telemetry: dict[str, int | None] = field(default_factory=dict)
    prefilter_cycle: int | None = None
    firmware: str | None = None


class LocalService:
    def __init__(self, root: Path, config: ServerConfig):
        self.root = root
        self.config = config.validate()
        self.devices: dict[str, dict] = {}
        self.states: dict[str, DeviceState] = {}
        self.pairings: dict[str, Pairing] = {}
        self.downloads: dict[str, tuple[Pairing, bytes, bytes]] = {}
        self.listeners: set[Callable[[str], None]] = set()
        self.pending: dict[tuple[str, str], tuple[str, asyncio.Future]] = {}
        self.command_locks: dict[str, asyncio.Lock] = {}
        self.initialization_locks: dict[str, asyncio.Lock] = {}
        self.enrollment_lock = asyncio.Lock()
        self.broker: CowayBroker | None = None
        self.runners: list[web.AppRunner] = []
        self.tasks: set[asyncio.Task] = set()
        self.started = False
        self.stopping = False
        self.last_request = 0

    def subscribe(self, listener):
        self.listeners.add(listener)
        return lambda: self.listeners.discard(listener)

    def changed(self, serial):
        for listener in tuple(self.listeners):
            listener(serial)

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def start(self):
        if self.started:
            return
        self.stopping = False
        data = await asyncio.to_thread(pki.load_state, self.root)
        if data.get("version") != 1:
            raise ValueError("Unsupported credential store version")
        self.devices.update(data["devices"])
        self.states.update(
            {
                serial: DeviceState(
                    prefilter_cycle=(
                        value
                        if valid_prefilter_cycle(value := device.get("prefilter_cycle"))
                        else None
                    )
                )
                for serial, device in self.devices.items()
            }
        )
        ca, https_tls, mqtt_tls = await asyncio.to_thread(
            pki.initialize, self.root, self.config.host
        )
        self.ca = ca
        try:
            # Independent web apps prevent accidental exposure of private routes over HTTP.
            for secure, port in ((False, self.config.http_port), (True, self.config.https_port)):
                app = web.Application(client_max_size=8192)
                app.router.add_get("/ca.pem", self.ca_download)
                if secure:
                    app.router.add_post("/n/{bluetooth_name}", self.enroll)
                    app.router.add_get("/d/{cap}/{kind}", self.credential_download)
                runner = web.AppRunner(app, access_log=None, shutdown_timeout=5)
                self.runners.append(runner)
                await runner.setup()
                await web.TCPSite(
                    runner,
                    self.config.bind_host,
                    port,
                    ssl_context=https_tls if secure else None,
                ).start()
            broker_config = await asyncio.to_thread(
                CowayBroker.configuration, self.root, self.config.bind_host, self.config.mqtt_port
            )
            self.broker = CowayBroker(
                broker_config,
                mqtt_tls,
                self.devices,
                self.message,
                self.connected,
                self.disconnected,
            )
            await self.broker.start()
            self.started = True
            self.task(self.maintenance())
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        self.stopping = True
        self.started = False
        for _, future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Local service stopped"))
        self.pending.clear()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        self.tasks.clear()
        if self.broker:
            try:
                await self.broker.shutdown()
            except Exception:
                _LOGGER.exception("Error stopping the Coway broker")
            self.broker = None
        for runner in reversed(self.runners):
            await runner.cleanup()
        self.runners.clear()
        for pairing in tuple(self.pairings.values()):
            self.cancel_pairing(pairing)
        self.downloads.clear()
        for serial, state in self.states.items():
            state.online = False
            self.changed(serial)

    async def save(self):
        # Callers hold enrollment_lock for identity and confirmed-setting changes.
        await asyncio.to_thread(
            pki.save_state,
            self.root,
            {"version": 1, "devices": dict(self.devices)},
        )

    def begin_pairing(self) -> Pairing:
        if not self.started:
            raise ConnectionError("Local service is not running")
        self.expire_pairings()
        if len(self.pairings) >= 8:
            raise ValueError("Finish or cancel an existing setup first")
        pairing = Pairing()
        self.pairings[pairing.token] = pairing
        return pairing

    def cancel_pairing(self, pairing):
        self.pairings.pop(pairing.token, None)
        if pairing.download:
            self.downloads.pop(pairing.download, None)
        pairing.finished.set()

    def expire_pairings(self):
        for pairing in tuple(self.pairings.values()):
            if time.time() > pairing.expires:
                self.cancel_pairing(pairing)

    def setup_bundle(self, pairing):
        cfg = self.config
        return {
            "v": SETUP_FORMAT,
            "model": MODEL,
            "caUrl": f"http://{cfg.host}:{cfg.http_port}/ca.pem",
            "certSvrUrl": f"https://{cfg.host}:{cfg.https_port}/n",
            "certToken": pairing.token,
            "expiresAt": int(pairing.expires * 1000),
        }

    def setup_url(self, pairing, return_url):
        bundle = self.setup_bundle(pairing) | {"returnUrl": return_url}
        payload = json.dumps(bundle, separators=(",", ":")).encode()
        fragment = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        url = urlsplit(self.config.provisioner_url)
        path = f"{url.path.rstrip('/')}/v{SETUP_FORMAT}/"
        return urlunsplit((url.scheme, url.netloc, path, "", f"setup={fragment}"))

    @staticmethod
    def response(body, status=200):
        # Compact, as the fans write it: the hotspot-setup AP-1720G (02F9R)
        # rejects an enrollment reply with spaces and enrolls again every ten
        # seconds without downloading its certificate.
        return web.json_response(
            body,
            status=status,
            headers={"Cache-Control": "no-store"},
            dumps=lambda value: json.dumps(value, separators=(",", ":")),
        )

    def rejected(self, status, message):
        return self.response(
            {"header": {"success": False, "result": "F5002", "message": message}}, status
        )

    async def ca_download(self, request):
        return web.Response(
            body=self.ca,
            content_type="application/x-pem-file",
            headers={"Cache-Control": "no-store"},
        )

    async def enroll(self, request):
        try:
            body = await request.json()
            token = body["certToken"]
            serial = body["serial"]
            mac = body["macAddr"].replace(":", "").upper()
            pairing = self.pairings[token]
            if serial != local_serial(mac):
                raise ValueError
        except ValueError, TypeError, KeyError, AttributeError, UnicodeError:
            return self.rejected(403, "INVALID_LOCAL_TOKEN")
        async with self.enrollment_lock:
            # The token is claimed under this lock, so concurrent requests
            # cannot use it to enroll different devices.
            if (
                self.pairings.get(token) is not pairing
                or pairing.connected
                or time.time() > pairing.expires
            ):
                return self.rejected(403, "INVALID_LOCAL_TOKEN")
            old = self.devices.get(serial)
            if (old and old["mac"] != mac) or (pairing.mac and pairing.mac != mac):
                return self.rejected(403, "DEVICE_IDENTITY_MISMATCH")
            if any(d["mac"] == mac and key != serial for key, d in self.devices.items()):
                return self.rejected(409, "DEVICE_ALREADY_ENROLLED")
            try:
                name = decode_bluetooth_name(request.match_info["bluetooth_name"])
            except ValueError:
                return self.rejected(400, "INVALID_BLUETOOTH_NAME")
            pin, cert, key = await asyncio.to_thread(pki.issue_device, self.root, serial, mac)
            self.devices[serial] = {
                "serial": serial,
                "mac": mac,
                "name": name,
                "model": MODEL,
                "fingerprint": pin,
                # Durable intent: a restart before the first acknowledgement
                # must retry initialization, not assume the default was applied.
                "prefilter_default_pending": True,
            }
            self.states.setdefault(serial, DeviceState()).prefilter_cycle = None
            await self.save()
            pairing.serial = serial
            pairing.mac = mac
            for other in tuple(self.pairings.values()):
                if other is not pairing and other.serial == serial:
                    self.cancel_pairing(other)
            pairing.download = pairing.download or secrets.token_urlsafe(18)
            self.downloads[pairing.download] = (pairing, cert, key)
            self.changed(serial)
        cfg = self.config
        base = f"https://{cfg.host}:{cfg.https_port}/d/{pairing.download}"
        return self.response(
            {
                "header": {"success": True, "result": "S1000", "message": "SUCCESS"},
                "payload": {
                    "macAddr": mac,
                    "serial": serial,
                    "certToken": token,
                    "host": cfg.host,
                    "port": cfg.mqtt_port,
                    "certSvrUrl": f"https://{cfg.host}:{cfg.https_port}{request.path}",
                    "caUrl": f"http://{cfg.host}:{cfg.http_port}/ca.pem",
                    "certUrl": base + "/c",
                    "privateUrl": base + "/k",
                    "svcCode": SERVICE_CODE,
                    "countryCd": "US",
                },
            }
        )

    async def credential_download(self, request):
        download = self.downloads.get(request.match_info["cap"])
        if download is None:
            raise web.HTTPNotFound()
        pairing, cert, key = download
        if pairing.complete or time.time() > pairing.expires:
            raise web.HTTPNotFound()
        kind = request.match_info["kind"]
        if kind not in ("c", "k"):
            raise web.HTTPNotFound()
        return web.Response(
            body=cert if kind == "c" else key,
            content_type="application/x-pem-file",
            headers={"Cache-Control": "no-store"},
        )

    def connected(self, serial):
        if self.stopping or serial not in self.states:
            return
        state = self.states[serial]
        was_online = state.online
        state.online = True
        if not was_online:
            # A new connection must report fresh measurements before we show them.
            state.telemetry.clear()
            for pairing in tuple(self.pairings.values()):
                # Require a fresh connection after this attempt's enrollment.
                if pairing.serial == serial and pairing.mac is not None:
                    pairing.connected = True
                    if pairing.download:
                        self.downloads.pop(pairing.download, None)
        self.changed(serial)
        if not was_online:
            self.task(self.refresh(serial))

    def disconnected(self, serial):
        state = self.states.get(serial)
        if state is not None:
            state.online = False
            for (device, _), (_, future) in tuple(self.pending.items()):
                if device == serial and not future.done():
                    future.set_exception(ConnectionError("Fan disconnected"))
            self.changed(serial)

    async def message(self, serial, topic, payload):
        try:
            body = json.loads(payload)
            header = body["header"]
            attributes = body.get("attributes", {})
            if (
                header.get("prodId") != serial
                or not isinstance(attributes, dict)
                or len(attributes) > 128
            ):
                return
            api = header["API_NO"]
            request_id = header.get("requestId")
        except ValueError, TypeError, KeyError, AttributeError:
            return
        state = self.states.get(serial)
        if state is None:
            return
        state.last_seen = time.time()
        if api in ("A1010", "A1012"):
            for key, value in attributes.items():
                if isinstance(key, str) and key.startswith(api + ".") and type(value) is int:
                    state.settings[key.split(".", 1)[1]] = value
        elif api == "A1020":
            state.telemetry.update(decode_report(attributes))
        elif api == "A1050":
            value = attributes.get("A1050.0001")
            if valid_prefilter_cycle(value):
                async with self.enrollment_lock:
                    device = self.devices.get(serial)
                    if device is not None and device.get("prefilter_cycle") != value:
                        previous = device.get("prefilter_cycle")
                        device["prefilter_cycle"] = value
                        try:
                            await self.save()
                        except OSError:
                            device["prefilter_cycle"] = previous
                            _LOGGER.exception("Could not save the confirmed pre-filter interval")
                            return
                    state.prefilter_cycle = value
        elif api == "A1000":
            version = attributes.get("mcuFrmwrVer")
            if isinstance(version, str):
                state.firmware = version[:80]
        pending = self.pending.get((serial, request_id)) if isinstance(request_id, str) else None
        if pending and pending[0] == api and not pending[1].done():
            pending[1].set_result(attributes)
        self.changed(serial)

    async def command(self, serial, api="A1013", attributes=None, timeout=15):
        lock = self.command_locks.setdefault(serial, asyncio.Lock())
        async with lock:
            state = self.states.get(serial)
            if not self.started or not self.broker or not state or not state.online:
                raise ConnectionError("Fan is offline")
            # Firmware accepts the captured app's fixed-width format, not UUIDs.
            self.last_request = max(self.last_request + 1, int(time.time() * 1000))
            request_id = f"{self.last_request:013d}-22"
            future = asyncio.get_running_loop().create_future()
            key = (serial, request_id)
            self.pending[key] = (REPLIES[api], future)
            try:
                payload = {
                    "header": {"requestId": request_id, "prodId": serial, "API_NO": api},
                    "attributes": attributes or {},
                }
                await self.broker.publish_command(
                    serial, json.dumps(payload, separators=(",", ":")).encode()
                )
                return await asyncio.wait_for(future, timeout)
            finally:
                self.pending.pop(key, None)
                if not future.done():
                    future.cancel()

    async def set(self, serial, setting, selection):
        api, suffix, values = SETTINGS[setting]
        wire = values[selection]
        reply = await self.command(serial, api, {f"{api}.{suffix}": wire})
        if reply.get(f"{REPLIES[api]}.{suffix}") != wire:
            raise ValueError("Fan replied without confirming the requested setting")
        return reply

    async def initialize_prefilter(self, serial):
        """Apply the enrollment default once, retaining only device-confirmed state."""
        async with self.initialization_locks.setdefault(serial, asyncio.Lock()):
            device = self.devices.get(serial)
            if not device or not device.get("prefilter_default_pending"):
                return
            if any(
                p.serial == serial and p.mac is not None and not p.connected
                for p in self.pairings.values()
            ):
                return
            await self.set(serial, "prefilter_cycle", DEFAULT_PREFILTER_CYCLE)
            async with self.enrollment_lock:
                # Removal or a newer enrollment may have replaced this record
                # while the command was in flight. Do not complete that attempt.
                if self.devices.get(serial) is not device:
                    return
                device.pop("prefilter_default_pending", None)
                try:
                    await self.save()
                except OSError:
                    device["prefilter_default_pending"] = True
                    raise
                for pairing in tuple(self.pairings.values()):
                    if pairing.serial == serial and pairing.connected:
                        pairing.complete = True
                        self.cancel_pairing(pairing)

    async def refresh(self, serial):
        try:
            await self.initialize_prefilter(serial)
            await self.command(serial)
        except OSError, ValueError:
            if not self.stopping:
                _LOGGER.debug("Coway initialization or status refresh was not confirmed")

    async def maintenance(self):
        while True:
            await asyncio.sleep(60)
            self.expire_pairings()
            await asyncio.gather(
                *(self.refresh(serial) for serial, state in self.states.items() if state.online)
            )

    async def remove(self, serial):
        async with self.enrollment_lock:
            self.devices.pop(serial, None)
            self.disconnected(serial)
            if self.broker:
                await self.broker.remove_device(serial)
            self.states.pop(serial, None)
            for pairing in tuple(self.pairings.values()):
                if pairing.serial == serial:
                    self.cancel_pairing(pairing)
            await self.save()
            for extension in ("pem", "key"):
                await asyncio.to_thread(
                    (self.root / "devices" / f"{serial}.{extension}").unlink, missing_ok=True
                )
            self.changed(serial)

    def diagnostics(self):
        # Deliberately omit keys, certificates, tokens, identities, IPs and raw payloads.
        return {
            "running": self.started,
            "amqtt_version": AMQTT_VERSION,
            "ports": {
                key: value for key, value in asdict(self.config).items() if key.endswith("_port")
            },
            "devices": [
                {
                    "model": d["model"],
                    "online": self.states[s].online,
                    "has_reported_state": bool(self.states[s].settings),
                }
                for s, d in self.devices.items()
            ],
        }
