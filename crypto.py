"""End-to-end encryption for the webhook bot (#9).

The bot owns a device created through the appservice API (MSC4190), keeps its Olm/Megolm
state and the room state it needs in one SQLite file under ``CRYPTO_DB_DIR`` and runs a ``/sync`` loop that only carries what the
crypto layer needs (memberships, encryption state, to-device and device-list changes).
Messages sent with ``client.send_message_event`` are then encrypted in encrypted rooms.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path as FsPath

from mautrix.api import Method, Path
from mautrix.client import Client, InternalEventType
from mautrix.client.state_store.asyncpg import PgStateStore
from mautrix.crypto import OlmMachine, PgCryptoStateStore, PgCryptoStore
from mautrix.errors import MatrixRequestError, MUnknownToken
from mautrix.types import (
    EventFilter,
    EventType,
    Filter,
    Membership,
    PresenceState,
    RoomEventFilter,
    RoomFilter,
    StateEvent,
    StateFilter,
    TrustState,
)
from mautrix.util.async_db import Database

log = logging.getLogger("webhook.crypto")

MX = os.getenv("MATRIX_URL", "http://localhost:8008").rstrip("/")
AS_TOK = os.environ["AS_TOKEN"]
LOCAL = os.getenv("BOT_LOCALPART", "webhook_bot")
SERVER = os.getenv("SERVER_NAME", "localhost")
USER_ID = f"@{LOCAL}:{SERVER}"
DATA_DIR = FsPath(os.getenv("CRYPTO_DB_DIR", "data"))
PICKLE_KEY = os.getenv("PICKLE_KEY", "matrix-webhook")
RECOVERY_KEY = os.getenv("RECOVERY_KEY", "").strip()
DEVICE_NAME = os.getenv("DEVICE_NAME", "Webhook bot")

_KEEP = [EventType.ROOM_MEMBER, EventType.ROOM_ENCRYPTION, EventType.ROOM_POWER_LEVELS]
_ALL = EventType.find("*")
SYNC_FILTER = Filter(
    presence=EventFilter(not_types=[_ALL]),
    account_data=EventFilter(not_types=[_ALL]),
    room=RoomFilter(
        include_leave=False,
        state=StateFilter(types=_KEEP, lazy_load_members=False),
        timeline=RoomEventFilter(types=_KEEP, limit=50, lazy_load_members=False),
        ephemeral=RoomEventFilter(not_types=[_ALL]),
        account_data=RoomEventFilter(not_types=[_ALL]),
    ),
)


class Bot:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db = Database.create(
            f"sqlite:{DATA_DIR / 'bot.db'}",
            upgrade_table=PgCryptoStore.upgrade_table,
            log=logging.getLogger("webhook.db"),
        )
        self.crypto_store = PgCryptoStore(USER_ID, PICKLE_KEY, self.db)
        self.state_store = PgCryptoStateStore(self.db)
        self.client = Client(
            mxid=USER_ID,
            base_url=MX,
            token=AS_TOK,
            state_store=self.state_store,
            sync_store=self.crypto_store,
        )
        self.client.presence = PresenceState.OFFLINE
        self.machine = OlmMachine(self.client, self.crypto_store, self.state_store)
        self.client.crypto = self.machine
        self.ready = False
        self.cross_signed = False

    async def start(self) -> None:
        await self.db.start()
        await PgStateStore.upgrade_table.upgrade(self.db)
        await self.crypto_store.open()

        await self._ensure_registered()
        await self._load_device()
        await self._ensure_cross_signed()

        self.client.add_event_handler(InternalEventType.SYNC_STOPPED, self._on_sync_stopped)
        self.client.add_event_handler(EventType.ROOM_MEMBER, self._on_member)
        self.client.start(SYNC_FILTER)
        self.ready = True
        log.info("Crypto ready: device %s, cross-signed %s", self.client.device_id, self.cross_signed)

    async def stop(self) -> None:
        self.ready = False
        self.client.stop()
        await self.crypto_store.close()
        await self.db.stop()
        await self.client.api.session.close()

    async def _ensure_registered(self) -> None:
        try:
            await self.client.api.request(
                Method.POST,
                Path.v3.register,
                {"username": LOCAL, "type": "m.login.application_service", "inhibit_login": True},
            )
        except MatrixRequestError as e:
            if e.errcode not in ("M_USER_IN_USE", "M_EXCLUSIVE"):
                raise

    async def _load_device(self) -> None:
        device_id = await self.crypto_store.get_device_id()
        await self.client.create_device_msc4190(device_id=device_id, initial_display_name=DEVICE_NAME)
        await self.machine.load()

        if not device_id:
            await self.crypto_store.put_device_id(self.client.device_id)
            await self.machine.share_keys()
            log.info("Created device %s", self.client.device_id)
            await self._delete_other_devices()
            return

        if self.machine.account.shared and not await self._keys_on_server():
            log.warning("Keys of device %s are gone from the server; starting a new device", device_id)
            await self.crypto_store.delete()
            self.client.api.as_device_id = None
            self.client.device_id = ""
            await self._load_device()
            return
        if not self.machine.account.shared:
            await self.machine.share_keys()

    async def _delete_other_devices(self) -> None:
        resp = await self.client.api.request(Method.GET, Path.v3.devices)
        for device in resp.get("devices", []):
            other = device.get("device_id")
            if not other or other == self.client.device_id:
                continue
            try:
                await self.client.api.request(Method.DELETE, Path.v3.devices[other])
                log.info("Deleted old device %s", other)
            except MatrixRequestError as e:
                log.warning("Could not delete old device %s: %s", other, e)

    async def _keys_on_server(self) -> bool:
        resp = await self.client.query_keys([self.client.mxid])
        device = resp.device_keys.get(self.client.mxid, {}).get(self.client.device_id)
        return bool(device and device.keys)

    async def _ensure_cross_signed(self) -> None:
        trust = await self.machine.resolve_trust(self.machine.own_identity)
        if trust >= TrustState.CROSS_SIGNED_UNTRUSTED:
            self.cross_signed = True
            return

        if RECOVERY_KEY:
            await self.machine.verify_with_recovery_key(RECOVERY_KEY)
            self.cross_signed = True
            log.info("Signed device %s with the recovery key", self.client.device_id)
            return

        if await self._has_cross_signing_identity():
            log.warning(
                "The bot already has a cross-signing identity but RECOVERY_KEY is not set; "
                "device %s stays unsigned",
                self.client.device_id,
            )
            return

        recovery_key = await self.machine.generate_recovery_key()
        key_file = DATA_DIR / "recovery_key.txt"
        key_file.write_text(recovery_key + "\n")
        key_file.chmod(0o600)
        self.cross_signed = True
        log.warning(
            "Created the bot's cross-signing identity. Its recovery key was written to %s: "
            "store it with the other secrets, then delete the file",
            key_file,
        )

    async def _has_cross_signing_identity(self) -> bool:
        resp = await self.client.query_keys([self.client.mxid])
        return self.client.mxid in resp.master_keys

    async def _on_member(self, evt: StateEvent) -> None:
        if evt.state_key == self.client.mxid and evt.content.membership == Membership.INVITE:
            try:
                await self.client.join_room(evt.room_id)
                log.info("Joined %s after an invite", evt.room_id)
            except MatrixRequestError as e:
                log.warning("Could not join %s: %s", evt.room_id, e)

    async def _on_sync_stopped(self, evt) -> None:
        error = getattr(evt, "error", None)
        if isinstance(error, MUnknownToken):
            log.critical("The appservice token or device is no longer valid; exiting")
            sys.exit(1)


_bot: Bot | None = None


async def start_bot() -> Bot:
    global _bot
    _bot = Bot()
    await _bot.start()
    return _bot


async def stop_bot() -> None:
    global _bot
    if _bot:
        await _bot.stop()
    _bot = None


def get_bot() -> Bot:
    if _bot is None:
        raise RuntimeError("The bot is not running")
    return _bot
