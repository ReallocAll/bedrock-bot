"""The private whole-message boundary used by the session's single sender."""

import hmac
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nethernet import Connection

    from .auth import Identity
    from .types import BotOptions

MAX_WIRE_MESSAGE = 2 * 16 * 1024 * 1024 + 1024
COMMAND_BYTES = 4 * 1024 * 1024
COMMAND_PACKETS = 4096


async def _open_connection(options: "BotOptions", identity: "Identity") -> "Connection":
    if not options.server_key_sha256:
        raise ValueError("a server identity key pin is required")

    from nethernet import ConnectionLimits, InvalidIdentity, connect_http
    from nethernet.identity import build_envelope, sign_fingerprints

    def sign_offer(offer: str) -> str:
        return build_envelope("offline.endstone.invalid", identity.token, sign_fingerprints(offer, identity.key))

    def verify_server(server_identity) -> None:
        if not hmac.compare_digest(server_identity.key_digest, options.server_key_sha256):
            raise InvalidIdentity("server identity key pin mismatch")

    timeout = options.connect_timeout_ms / 1000
    return await connect_http(
        options.server_url,
        ice_servers=[],
        limits=ConnectionLimits(max_message_size=MAX_WIRE_MESSAGE),
        negotiation_timeout=timeout,
        timeout=timeout,
        sign_offer=sign_offer,
        on_server_identity=verify_server,
    )
