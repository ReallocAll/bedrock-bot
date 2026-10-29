"""RakNet adapter for the message-oriented Bedrock transport interface."""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import raknet
import raknet.asyncio

from .transport import MAX_WIRE_MESSAGE

_GAME_PACKET_ID = 0xFE


class RakNetTransport:
    """Expose RakNet messages as Bedrock game batches."""

    __slots__ = ("_connection",)

    def __init__(self, connection: raknet.asyncio.Connection) -> None:
        self._connection = connection

    async def send(self, data: bytes) -> None:
        await self._connection.send(
            bytes((_GAME_PACKET_ID,)) + data,
            reliability=raknet.PacketReliability.RELIABLE_ORDERED,
            priority=raknet.PacketPriority.MEDIUM_PRIORITY,
            channel=0,
        )

    async def recv(self) -> bytes:
        while True:
            message = await self._connection.recv()
            if not message or message[0] != _GAME_PACKET_ID:
                continue
            if len(message) - 1 > MAX_WIRE_MESSAGE:
                raise ValueError("inbound wire message limit exceeded")
            return message[1:]

    async def close(self) -> None:
        await self._connection.close()


async def _wait_uninterruptibly(task: asyncio.Task):
    """Drain a task through repeated cancellation requests without abandoning it."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise


async def _close_late_connection(connection: raknet.asyncio.Connection) -> None:
    close_task = asyncio.create_task(connection.close())
    await _wait_uninterruptibly(close_task)


async def open_connection(options) -> RakNetTransport:
    parsed = urlsplit(options.server_url)
    host = parsed.hostname
    port = parsed.port
    if not host or port is None:
        raise ValueError("server_url must be an explicit raknet://host:port URL")

    attempts = max(3, (options.connect_timeout_ms + 499) // 500)
    acquire_task = asyncio.create_task(
        raknet.asyncio.create_connection((host, port), attempts=attempts, attempt_interval=0.5)
    )
    try:
        connection = await asyncio.shield(acquire_task)
    except asyncio.CancelledError as cancellation:
        try:
            connection = await _wait_uninterruptibly(acquire_task)
        except BaseException:
            pass
        else:
            try:
                await _close_late_connection(connection)
            except BaseException as error:
                cancellation.add_note(f"failed to close RakNet connection acquired during cancellation: {error!r}")
        raise

    return RakNetTransport(connection)
