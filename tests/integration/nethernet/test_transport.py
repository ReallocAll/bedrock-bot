import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nethernet
import pytest
from nethernet.identity import cpk_digest, verify_fingerprints

from bedrock_bot import transport


def options(url, pin):
    return SimpleNamespace(server_url=url, server_key_sha256=pin, connect_timeout_ms=3000)


async def test_missing_pin_rejects_before_transport_io(monkeypatch):
    dial = AsyncMock()
    monkeypatch.setattr(nethernet, "connect_http", dial)
    with pytest.raises(ValueError, match="pin"):
        await transport._open_connection(options("http://127.0.0.1:1", ""), None)
    assert not dial.called


async def test_adapter_signs_session_identity_and_verifies_pinned_server():
    key = nethernet.generate_operator_key()
    server_key = nethernet.generate_operator_key()
    identity = SimpleNamespace(key=key, token="local-test-token")
    signer = nethernet.IdentitySigner(server_key, domain="test.example")
    offers = []

    def validate(offer):
        assert offer.identity.domain == "offline.endstone.invalid"
        assert offer.identity.token == identity.token
        verify_fingerprints(offer.sdp, offer.identity.fingerprints, key.public_key())
        offers.append(offer)

    async def echo(conn):
        async for data in conn:
            await conn.send(data)

    async with nethernet.serve_http(
        echo,
        host="127.0.0.1",
        ice_servers=[],
        limits=nethernet.ConnectionLimits(),
        identity_signer=signer,
        validate_offer=validate,
    ) as server:
        conn = await transport._open_connection(
            options(f"http://127.0.0.1:{server.bound_port}", cpk_digest(server_key.public_key())),
            identity,
        )
        try:
            held = await conn.reserve_send(transport.MAX_WIRE_MESSAGE)
            held.release()
            held.release()
            payload = b"\x01wire-without-raknet-prefix" * 12000
            permit = await conn.reserve_send(len(payload))
            permit.send_nowait(payload)
            permit.release()
            assert await asyncio.wait_for(conn.recv(), 5) == payload
        finally:
            await conn.close()
        assert await conn.wait_closed() == nethernet.SessionError.NONE
    assert len(offers) == 1


@pytest.mark.parametrize("assertion", ["missing", "wrong-key"])
async def test_adapter_rejects_missing_or_unpinned_server_identity(assertion):
    key = nethernet.generate_operator_key()
    other = nethernet.generate_operator_key()
    signer = nethernet.IdentitySigner(key, domain="test.example") if assertion != "missing" else None
    identity = SimpleNamespace(key=other, token="local-test-token")

    async def handler(conn):
        await conn.wait_closed()

    async with nethernet.serve_http(
        handler,
        host="127.0.0.1",
        ice_servers=[],
        limits=nethernet.ConnectionLimits(),
        identity_signer=signer,
    ) as server:
        with pytest.raises(nethernet.InvalidIdentity):
            await transport._open_connection(
                options(f"http://127.0.0.1:{server.bound_port}", cpk_digest(other.public_key())),
                identity,
            )


async def test_actual_adapter_close_joins_while_udp_is_paused_without_resume():
    key = nethernet.generate_operator_key()
    identity = SimpleNamespace(key=key, token="local-test-token")
    signer = nethernet.IdentitySigner(key, domain="test.example")

    async def handler(conn):
        await conn.wait_closed()

    async with nethernet.serve_http(
        handler,
        host="127.0.0.1",
        ice_servers=[],
        limits=nethernet.ConnectionLimits(),
        identity_signer=signer,
    ) as server:
        conn = await transport._open_connection(
            options(f"http://127.0.0.1:{server.bound_port}", cpk_digest(key.public_key())),
            identity,
        )
        sctp = conn._session._pc._pc.sctp
        ice = sctp.transport.transport._connection
        protocol = next(iter(ice._nominated.values())).protocol
        protocol.pause_writing()
        try:
            await conn.send(b"waiting for UDP capacity")
            await asyncio.sleep(0)
            assert conn.state == nethernet.SessionState.CONNECTED
            await asyncio.wait_for(conn.close(), 2)
            assert await conn.wait_closed() == nethernet.SessionError.NONE
            assert not ice._protocols
            assert not sctp._service_tasks
            assert sctp._send_bytes == 0
        finally:
            protocol.resume_writing()
            await conn.close()
