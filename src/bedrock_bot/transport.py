"""NetherNet connection, identity verification, and application message limits."""

import hmac
from typing import TYPE_CHECKING

from aiortc import RTCIceServer
from nethernet import IdentitySigner, InvalidIdentity, connect_http
from nethernet.identity import build_envelope, insert_identity, sign_fingerprints

if TYPE_CHECKING:
    from nethernet import Connection

    from .auth import Identity
    from .raknet_transport import RakNetTransport
    from .types import BotOptions

MAX_WIRE_MESSAGE = 2 * 16 * 1024 * 1024 + 1024
COMMAND_BYTES = 4 * 1024 * 1024
COMMAND_PACKETS = 4096


async def _open_connection(options: "BotOptions", identity: "Identity") -> "Connection | RakNetTransport":
    selected_transport = getattr(options, "transport", "nethernet")
    if selected_transport == "raknet":
        try:
            from .raknet_transport import open_connection
        except ModuleNotFoundError as error:
            if error.name == "raknet":
                raise RuntimeError(
                    "RakNet transport requires the optional dependency; install bedrock-bot[raknet]"
                ) from error
            raise
        return await open_connection(options)
    if selected_transport != "nethernet":
        raise ValueError("transport must be 'nethernet' or 'raknet'")
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
    return await connect_http(
        options.server_url,
        ice_servers=[RTCIceServer(urls=[])],
        negotiation_timeout=timeout,
        timeout=timeout,
        identity_signer=BotIdentitySigner(identity.key, domain="offline.endstone.invalid"),
        on_server_identity=verify_server,
    )
