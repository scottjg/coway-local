import asyncio
import json
import logging
import ssl
import time
from dataclasses import replace

import aiohttp
import pytest

from custom_components.coway_local.broker import topic
from custom_components.coway_local.const import SETTINGS
from custom_components.coway_local.service import LocalService, ServerConfig, local_serial

from .conftest import Fan, enroll, eventually


async def test_setup_token_is_bound_to_one_mac_on_first_enrollment(service):
    pairing = service.begin_pairing()
    bundle = service.setup_bundle(pairing)
    assert pairing.serial is None and pairing.mac is None
    tls = await asyncio.to_thread(ssl.create_default_context, cafile=service.root / "ca.pem")
    url = bundle["certSvrUrl"] + "/VGVzdCBmYW4"
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls)) as client:
        for serial, mac in (("PRINTEDSERIAL00001", "001122334455"), ("../bad", "bad")):
            async with client.post(
                url, json={"certToken": pairing.token, "serial": serial, "macAddr": mac}
            ) as response:
                assert response.status == 403
        assert pairing.serial is None and not service.devices

        async def claim(mac):
            async with client.post(
                url, json={"certToken": pairing.token, "serial": local_serial(mac), "macAddr": mac}
            ) as response:
                return response.status

        # Two devices racing to use one token must never both be enrolled.
        statuses = await asyncio.gather(claim("001122334455"), claim("001122334466"))
        assert sorted(statuses) == [200, 403]
        assert len(service.devices) == 1
        assert pairing.serial == local_serial(pairing.mac)
        assert service.devices[pairing.serial]["mac"] == pairing.mac


async def test_new_claim_for_same_fan_revokes_old_setup_capability(service):
    first, _ = await enroll(service)
    second = service.begin_pairing()
    await enroll(service, pairing=second)
    assert second.serial == first.serial == "LOCAL0001122334455"
    assert first.token not in service.pairings
    assert first.download not in service.downloads
    assert second.token in service.pairings
    assert len(service.devices) == 1


async def test_enrollment_is_temporary_and_https_only(service):
    pairing, payload = await enroll(service)
    tls = await asyncio.to_thread(ssl.create_default_context, cafile=service.root / "ca.pem")
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls)) as client:
        async with client.get(service.setup_bundle(pairing)["caUrl"]) as response:
            assert response.status == 200
        url = payload["certSvrUrl"]
        for body in (
            {},
            [],
            {"certToken": "wrong"},
            {"certToken": pairing.token, "serial": "OTHER00001", "macAddr": "001122334455"},
            {"certToken": pairing.token, "serial": pairing.serial, "macAddr": "001122334466"},
        ):
            async with client.post(url, json=body) as response:
                assert response.status == 403
        http_base = f"http://127.0.0.1:{service.config.http_port}"
        async with client.post(http_base + "/n/VGVzdCBmYW4", json={}) as response:
            assert response.status == 404
        async with client.get(
            payload["privateUrl"].replace(
                f"https://127.0.0.1:{service.config.https_port}", http_base
            )
        ) as response:
            assert response.status == 404
        pairing.expires = time.time() - 1
        async with client.get(payload["privateUrl"]) as response:
            assert response.status == 404
        async with client.post(
            url,
            json={
                "certToken": pairing.token,
                "serial": pairing.serial,
                "macAddr": pairing.mac,
            },
        ) as response:
            assert response.status == 403


async def test_certificate_server_replies_in_compact_json(service):
    # The hotspot-setup AP-1720G (02F9R) rejects a reply written with spaces.
    pairing = service.begin_pairing()
    tls = await asyncio.to_thread(ssl.create_default_context, cafile=service.root / "ca.pem")
    url = service.setup_bundle(pairing)["certSvrUrl"] + "/VGVzdCBmYW4"
    mac = "001122334455"
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls)) as client:
        for body, status in (
            ({}, 403),
            ({"certToken": pairing.token, "serial": local_serial(mac), "macAddr": mac}, 200),
        ):
            async with client.post(url, json=body) as response:
                assert response.status == status
                text = await response.text()
            assert text == json.dumps(json.loads(text), separators=(",", ":"))
            assert ('"success":true' if status == 200 else '"success":false') in text


async def test_fan_commands_reserved_events_and_acknowledgements(service, fan):
    assert service.states[fan.serial].online
    assert not service.pairings and not service.downloads
    for setting, (_, _, values) in SETTINGS.items():
        for value in values:
            await service.set(fan.serial, setting, value)
    assert service.states[fan.serial].prefilter_cycle == 224
    assert service.states[fan.serial].settings["000A"] == 3
    request = fan.messages.get_nowait()
    assert len(request["header"]["requestId"]) == 16
    for denied in ("#", "$aws/#", topic("MControl", "OTHER00001"), topic("BEvent", fan.serial)):
        await asyncio.to_thread(fan.subscribe, denied, False)
    fan.respond = False
    with pytest.raises(TimeoutError):
        await service.command(fan.serial, timeout=0.15)
    assert not service.pending
    await asyncio.to_thread(fan.close)
    await eventually(lambda: not service.states[fan.serial].online)
    with pytest.raises(ConnectionError):
        await service.command(fan.serial)


async def test_identity_cannot_impersonate_or_evict_another_fan(service, fan):
    await enroll(service, "LOCAL0001122334466", "001122334466")
    impostor = await asyncio.to_thread(
        Fan, service.root, service.config.mqtt_port, fan.serial, "LOCAL0001122334466"
    )
    try:
        await asyncio.to_thread(impostor.open, False)
        await asyncio.sleep(0.15)
        assert impostor.connections.empty()
        assert service.states[fan.serial].online
        await service.set(fan.serial, "speed", "2")
    finally:
        await asyncio.to_thread(impostor.close)
    other = await asyncio.to_thread(
        Fan, service.root, service.config.mqtt_port, "LOCAL0001122334466"
    )
    try:
        await asyncio.to_thread(other.open)
        await eventually(lambda: bool(service.states[other.serial].settings))
        await service.set(other.serial, "speed", "3")
        assert service.states[fan.serial].settings["0003"] == 2
        assert service.states[other.serial].settings["0003"] == 3
        await asyncio.gather(
            service.set(fan.serial, "prefilter_cycle", "2 weeks"),
            service.set(other.serial, "prefilter_cycle", "4 weeks"),
        )
        saved = json.loads((service.root / "devices.json").read_text())["devices"]
        assert saved[fan.serial]["prefilter_cycle"] == 112
        assert saved[other.serial]["prefilter_cycle"] == 224
    finally:
        await asyncio.to_thread(other.close)


async def test_missing_certificate_rejected(service):
    context = await asyncio.to_thread(ssl.create_default_context, cafile=service.root / "ca.pem")
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    with pytest.raises((ConnectionError, ssl.SSLError)):
        await asyncio.open_connection("127.0.0.1", service.config.mqtt_port, ssl=context)


async def test_wrong_direction_publications_are_dropped(service, fan):
    original = service.states[fan.serial].settings["0003"]
    forged = json.dumps(
        {
            "header": {"prodId": fan.serial, "API_NO": "A1010"},
            "attributes": {"A1010.0003": 3},
        }
    )
    for denied in (
        topic("MControl", fan.serial),
        topic("BEvent", "LOCAL0001122334466"),
        topic("MEvent", "LOCAL0001122334466"),
        "$SYS/test",
    ):
        await asyncio.to_thread(fan.publish, denied, forged)
    await asyncio.sleep(0.05)
    assert service.states[fan.serial].settings["0003"] == original
    await service.set(fan.serial, "speed", "2")


async def test_reports_on_mevent_are_accepted(service, fan):
    # The hotspot-setup AP-1720G (02F9R) reports on MEvent instead of BEvent.
    report = {"header": {"prodId": fan.serial, "API_NO": "A1010"}, "attributes": {}}
    for speed in (3, 2):
        report["attributes"] = {"A1010.0003": speed}
        await asyncio.to_thread(fan.publish, topic("MEvent", fan.serial), json.dumps(report))
        await eventually(lambda: service.states[fan.serial].settings["0003"] == speed)


async def test_dropped_publications_are_reported_once_without_identity(service, fan, caplog):
    caplog.set_level(logging.WARNING, logger="custom_components.coway_local.broker")
    for _ in range(3):
        await asyncio.to_thread(fan.publish, topic("MUnknown", fan.serial), "{}")
    # A command round trip shows the broker has handled the publications.
    await service.set(fan.serial, "speed", "2")
    messages = [r.getMessage() for r in caplog.records if r.name.endswith(".broker")]
    assert messages == [
        "Dropped a fan publication on IoCare/MUnknown/002AIR007/<serial>, "
        "which the broker does not accept"
    ]


async def test_persistence_reload_and_removal(service, fan):
    ca = (service.root / "ca.pem").read_bytes()
    cert = (service.root / "devices" / f"{fan.serial}.pem").read_bytes()
    await asyncio.to_thread(fan.close)
    await service.stop()
    await service.start()
    assert (service.root / "ca.pem").read_bytes() == ca
    assert (service.root / "devices" / f"{fan.serial}.pem").read_bytes() == cert
    await asyncio.to_thread(fan.open)
    await eventually(lambda: service.states[fan.serial].online)
    await service.set(fan.serial, "speed", "3")
    diagnostics = json.dumps(service.diagnostics())
    assert fan.serial not in diagnostics and "127.0.0.1" not in diagnostics
    await service.remove(fan.serial)
    assert not service.devices
    assert not (service.root / "devices" / f"{fan.serial}.key").exists()
    await asyncio.to_thread(fan.close)
    while not fan.connections.empty():
        fan.connections.get_nowait()
    await asyncio.to_thread(fan.open, False)
    await asyncio.sleep(0.05)
    assert fan.connections.empty()
    with pytest.raises(ConnectionError):
        await service.command(fan.serial)


async def test_partial_start_failure_releases_listeners(service, tmp_path):
    failed = LocalService(
        tmp_path / "failure",
        replace(
            service.config, http_port=service.config.https_port, https_port=service.config.http_port
        ),
    )
    with pytest.raises(OSError):
        await failed.start()
    assert not failed.runners and not failed.started
    await service.stop()
    await failed.start()
    await failed.stop()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"host": "0.0.0.0"},
        {"host": "::1"},
        {"mqtt_port": 80},
        {"mqtt_port": 8080},
        {"provisioner_url": "http://example.com"},
        {"provisioner_url": "https://example.com/#secret"},
    ],
)
def test_invalid_server_configuration(kwargs):
    with pytest.raises(ValueError):
        ServerConfig(**({"host": "192.0.2.1"} | kwargs)).validate()
