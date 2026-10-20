import asyncio
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nethernet
import pytest
from aiortc import RTCIceServer
from nethernet.identity import cpk_digest, verify_fingerprints

from bedrock_bot import transport
from bedrock_bot.auth import create_identity


def options(url, pin):
    return SimpleNamespace(server_url=url, server_key_sha256=pin, connect_timeout_ms=3000)


async def test_missing_pin_rejects_before_transport_io(monkeypatch):
    dial = AsyncMock()
    monkeypatch.setattr(nethernet, "connect_http", dial)
    with pytest.raises(ValueError, match="pin"):
        await transport._open_connection(options("http://127.0.0.1:1", ""), None)
    assert not dial.called


async def test_adapter_signs_session_identity_and_verifies_pinned_server():
    server_key = nethernet.generate_operator_key()
    identity = create_identity("signed test", "00000000-0000-0000-0000-000000000001")
    signer = nethernet.IdentitySigner(server_key, domain="test.example")
    offers = []

    def validate(offer):
        assert offer.identity.domain == "offline.endstone.invalid"
        assert offer.identity.token == identity.token
        verify_fingerprints(offer.sdp, offer.identity.fingerprints, identity.key.public_key())
        offers.append(offer)

    async def echo(conn):
        async for data in conn:
            await conn.send(data)

    async with nethernet.serve_http(
        echo,
        host="127.0.0.1",
        ice_servers=[RTCIceServer(urls=[])],
        identity_signer=signer,
        validate_offer=validate,
    ) as server:
        conn = await transport._open_connection(
            options(f"http://127.0.0.1:{server.bound_port}", cpk_digest(server_key.public_key())),
            identity,
        )
        try:
            payload = b"\x01wire-without-raknet-prefix" * 12000
            await conn.send(payload)
            assert await asyncio.wait_for(conn.recv(), 5) == payload
        finally:
            await conn.close()
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
        ice_servers=[RTCIceServer(urls=[])],
        identity_signer=signer,
    ) as server:
        with pytest.raises(nethernet.InvalidIdentity):
            await transport._open_connection(
                options(f"http://127.0.0.1:{server.bound_port}", cpk_digest(other.public_key())),
                identity,
            )


async def test_adapter_cancellation_during_pending_http_signaling():
    from aiohttp import web

    entered, release = asyncio.Event(), asyncio.Event()

    async def pending(request):
        entered.set()
        await release.wait()
        return web.Response(status=503)

    async def capability(request):
        return web.Response(status=200, text="1")

    app = web.Application()
    app.router.add_get("/v1/join", capability)
    app.router.add_post("/v1/join/{network_id}", pending)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = int(runner.addresses[0][1])
        identity = create_identity("cancel test", "00000000-0000-0000-0000-000000000001")
        task = asyncio.create_task(transport._open_connection(options(f"http://127.0.0.1:{port}", "0" * 64), identity))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await runner.cleanup()


def test_official_connection_process_exits_after_repeated_close():
    code = """
import asyncio
import sys
from types import SimpleNamespace
import nethernet
from aiortc import RTCIceServer
from nethernet.identity import cpk_digest
from bedrock_bot import transport
from bedrock_bot.auth import create_identity
async def main():
    key = nethernet.generate_operator_key()
    async def echo(conn):
        async for data in conn:
            await conn.send(data)
    signer = nethernet.IdentitySigner(key, domain="exit.example")
    async with nethernet.serve_http(
        echo, host="127.0.0.1", ice_servers=[RTCIceServer(urls=[])], identity_signer=signer
    ) as server:
        options = SimpleNamespace(server_url=f"http://127.0.0.1:{server.bound_port}",
                                  server_key_sha256=cpk_digest(key.public_key()), connect_timeout_ms=3000)
        identity = create_identity("exit test", "00000000-0000-0000-0000-000000000001")
        conn = await transport._open_connection(options, identity)
        payload = b"fragmented-exit" * 2000
        await conn.send(payload)
        assert await asyncio.wait_for(conn.recv(), 5) == payload
        await conn.close()
        await conn.close()
asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)
"""
    result = subprocess.run([sys.executable, "-I", "-c", code], check=False, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert not result.stderr
