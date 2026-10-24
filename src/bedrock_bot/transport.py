"""NetherNet connection, identity verification, and application message limits."""

import hmac
from typing import TYPE_CHECKING

from aiortc import RTCIceServer
from nethernet import IdentitySigner, InvalidIdentity, connect_http
from nethernet.identity import build_envelope, insert_identity, sign_fingerprints

from .auth import derive_session_key

if TYPE_CHECKING:
    from nethernet import Connection

    from .auth import Identity
    from .types import BotOptions

MAX_WIRE_MESSAGE = 2 * 16 * 1024 * 1024 + 1024
COMMAND_BYTES = 4 * 1024 * 1024
COMMAND_PACKETS = 4096


def validate_outbound_message(data: bytes) -> None:
    if len(data) > MAX_WIRE_MESSAGE:
        raise ValueError("outbound wire message limit exceeded")


class _Connection:
    def __init__(self, connection: "Connection"):
        self._connection = connection
        self.batch_bytes_in = 0
        self.batch_bytes_out = 0

    async def send(self, data: bytes) -> None:
        validate_outbound_message(data)
        await self._connection.send(data)
        self.batch_bytes_out += len(data)

    async def recv(self) -> bytes:
        data = await self._connection.recv()
        self.batch_bytes_in += len(data)
        if len(data) > MAX_WIRE_MESSAGE:
            raise ValueError("inbound wire message limit exceeded")
        return data

    async def close(self) -> None:
        await self._connection.close()


def validate_server_handshake(identity: "Identity", token: str) -> None:
    # NetherNet protects the stream with DTLS. Verify the game handshake but
    # deliberately leave BatchCodec game-layer encryption disabled.
    derive_session_key(identity, token)


async def _open_connection(options: "BotOptions", identity: "Identity") -> _Connection:
    if not options.server_key_sha256:
        raise ValueError("a server identity key pin is required")

    class BotIdentitySigner(IdentitySigner):
        def sign(self, sdp: str) -> str:
            return insert_identity(
                sdp, build_envelope("offline.endstone.invalid", identity.token, sign_fingerprints(sdp, identity.key))
            )

    def verify_server(server_identity) -> None:
        if not hmac.compare_digest(server_identity.key_digest, options.server_key_sha256):
            raise InvalidIdentity("server identity key pin mismatch")

    timeout = options.connect_timeout_ms / 1000
    connection = await connect_http(
        options.server_url,
        ice_servers=[RTCIceServer(urls=[])],
        negotiation_timeout=timeout,
        timeout=timeout,
        identity_signer=BotIdentitySigner(identity.key, domain="offline.endstone.invalid"),
        on_server_identity=verify_server,
    )
    return _Connection(connection)
