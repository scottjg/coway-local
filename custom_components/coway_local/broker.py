"""aMQTT 0.12.1 adapter with exact Coway topic permissions.

Session initialization and delivery follow the upstream MIT-licensed implementation.
See LICENSE.amqtt. Private hooks are deliberately version-pinned and wire-tested.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from importlib.metadata import version

from amqtt.broker import Broker
from amqtt.contexts import Action, BrokerConfig
from amqtt.errors import MQTTError
from amqtt.events import BrokerEvents
from amqtt.mqtt.protocol.broker_handler import BrokerProtocolHandler

from .const import SERVICE_CODE

AMQTT_VERSION = version("amqtt")
_LOGGER = logging.getLogger(__name__)


def topic(kind: str, serial: str) -> str:
    prefix = "$aws/rules/AIR/IoCare" if kind == "BEvent" else "IoCare"
    return f"{prefix}/{kind}/{SERVICE_CODE}/{serial}"


def publications(serial):
    # The AP-1022K reports on BEvent. The hotspot-setup AP-1720G (02F9R) reports
    # the same messages on MEvent.
    return {topic(kind, serial) for kind in ("MITG", "MITGLWT", "BEvent", "MEvent")}


def subscriptions(serial):
    return {
        *(topic(kind, serial) for kind in ("MControl", "MFota", "MCert")),
        f"$aws/things/{serial}/shadow/update/delta",
    }


class CowayBroker(Broker):
    @staticmethod
    def configuration(root, host, port):
        # aMQTT checks certificate paths synchronously. Validate in the executor.
        return BrokerConfig.from_dict(
            {
                "listeners": {
                    "default": {
                        "type": "tcp",
                        "bind": f"{host}:{port}",
                        "ssl": True,
                        "certfile": str(root / "server.pem"),
                        "keyfile": str(root / "server.key"),
                        "max_connections": 32,
                    }
                },
                "plugins": {},
            }
        )

    def __init__(self, config, tls, devices, on_message, on_ready, on_disconnect):
        if AMQTT_VERSION != "0.12.1":
            raise RuntimeError("Coway Local requires the validated aMQTT 0.12.1")
        self.tls = tls
        self.devices = devices
        self.on_message = on_message
        self.on_ready = on_ready
        self.on_disconnect = on_disconnect
        self.closing = False
        self.dropped = set()
        super().__init__(config)

    def _create_ssl_context(self, listener):
        return self.tls

    def _identity_valid(self, session) -> bool:
        device = self.devices.get(session.client_id)
        tls = session.ssl_object
        der = tls.getpeercert(binary_form=True) if tls else None
        return bool(device and der and hashlib.sha256(der).hexdigest() == device["fingerprint"])

    def _rejection_reason(self, session) -> str:
        """Say why, without logging the client's identity."""
        if session.client_id not in self.devices:
            return "Client is not enrolled"
        # A resumed TLS session carries the certificate from its original
        # handshake, which can predate a re-enrollment.
        resumed = getattr(session.ssl_object, "session_reused", None)
        return f"Certificate does not match the enrolled client (resumed TLS session: {resumed})"

    @staticmethod
    async def _close(writer):
        # A rejected client may vanish without completing the TLS shutdown.
        with contextlib.suppress(TimeoutError, OSError):
            await writer.close()

    async def _initialize_client_session(self, reader, writer, remote_address, remote_port):
        # Authenticate BEFORE touching a cached session: otherwise an enrolled
        # device could evict another device by claiming its MQTT client ID.
        handler, session = await BrokerProtocolHandler.init_from_connect(
            reader, writer, self.plugins_manager
        )
        session.ssl_object = writer.get_ssl_info()
        if not self._identity_valid(session):
            reason = self._rejection_reason(session)
            await self._close(writer)
            raise MQTTError(reason)
        if session.will_flag and session.will_topic != topic("MITGLWT", session.client_id):
            await self._close(writer)
            raise MQTTError("Invalid device will topic")
        if session.clean_session:
            await self._delete_session(session.client_id)
            session.parent = 0
        elif session.client_id in self._sessions:
            existing, _ = self._sessions[session.client_id]
            for name in (
                "will_flag",
                "will_message",
                "will_topic",
                "will_qos",
                "will_retain",
                "keep_alive",
                "username",
                "password",
                "ssl_object",
            ):
                setattr(existing, name, getattr(session, name))
            session = existing
            session.parent = 1
        else:
            session.parent = 0
        session.is_anonymous = False
        return handler, session

    async def _authenticate(self, session, listener):
        return self._identity_valid(session)

    async def _topic_filtering(self, session, topic, action):
        if session is None or not self._identity_valid(session):
            return False
        if action == Action.PUBLISH:
            return topic in publications(session.client_id)
        if action in (Action.SUBSCRIBE, Action.RECEIVE):
            return topic in subscriptions(session.client_id)
        return False

    async def _handle_client_session(self, reader, writer, session, handler, server, name):
        try:
            await super()._handle_client_session(reader, writer, session, handler, server, name)
        finally:
            current = self._sessions.get(session.client_id)
            if current and current[1] is handler:
                self.on_disconnect(session.client_id)

    async def _client_message_loop(self, session, handler):
        control = topic("MControl", session.client_id)
        if any(s is session for s, _ in self._subscriptions.get(control, [])):
            self.on_ready(session.client_id)
        return await super()._client_message_loop(session, handler)

    async def _handle_subscription(self, session, handler, waiter):
        await super()._handle_subscription(session, handler, waiter)
        control = topic("MControl", session.client_id)
        if any(s is session for s, _ in self._subscriptions.get(control, [])):
            self.on_ready(session.client_id)

    async def _handle_message_delivery(self, session, handler, waiter):
        message = waiter.result()
        if message is None:
            return True
        if self.closing:
            return True
        if len(message.data) > 32768 or not await self._topic_filtering(
            session, message.topic, Action.PUBLISH
        ):
            # Drop unauthorized data without abandoning a running protocol
            # handler (the upstream loop's False path does not close it).
            self._report_dropped(session, message.topic)
            return True
        # Own exact BEvent is allowed; all other reserved topics fail the ACL.
        await self.plugins_manager.fire_event(
            BrokerEvents.MESSAGE_RECEIVED, client_id=session.client_id, message=message
        )
        await self.on_message(session.client_id, message.topic, bytes(message.data))
        # Device events are consumed in-process. No external controller or other
        # fan is allowed to subscribe to them. MQTT acknowledgements remain in
        # aMQTT's protocol handler.
        return True

    def _report_dropped(self, session, name):
        """Say once per fan and topic what was dropped, without the fan's identity.

        A fan that reports on an unexpected topic otherwise just looks offline.
        """
        key = (session.client_id, name)
        if key in self.dropped or len(self.dropped) >= 256:
            return
        self.dropped.add(key)
        shown = name.replace(session.client_id, "<serial>") if session.client_id else name
        _LOGGER.warning("Dropped a fan publication on %s, which the broker does not accept", shown)

    async def publish_command(self, serial, payload):
        if self.closing:
            raise ConnectionError("Local broker is stopping")
        await self.internal_message_broadcast(topic("MControl", serial), payload, qos=1)

    async def remove_device(self, serial):
        await self._cleanup_session(serial)

    async def _handle_disconnect(self, session, handler, waiter):
        # Availability is delivered directly through on_disconnect; there are
        # no external LWT subscribers. Avoid queued wills during shutdown.
        session.will_flag = False
        await super()._handle_disconnect(session, handler, waiter)

    async def shutdown(self):
        self.closing = True
        await super().shutdown()
