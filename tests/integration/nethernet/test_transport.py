import asyncio
import base64
import json
import logging
import subprocess
import sys
import warnings
from types import SimpleNamespace

import nethernet
import pytest
from aiortc import RTCIceServer
from nethernet.identity import cpk_digest, verify_fingerprints

from bedrock_bot import transport
from bedrock_bot.auth import create_identity


def options(url, pin):
    return SimpleNamespace(server_url=url, server_key_sha256=pin, connect_timeout_ms=3000)


def tamper_identity(sdp, fault):
    lines = sdp.splitlines(keepends=True)
    identity_line = next(index for index, line in enumerate(lines) if line.startswith("a=identity:"))
    newline = "\r\n" if lines[identity_line].endswith("\r\n") else "\n"
    if fault == "malformed":
        lines[identity_line] = f"a=identity:!{newline}"
        return "".join(lines)

    envelope = json.loads(base64.b64decode(lines[identity_line][len("a=identity:") :].strip()))
    assertion = json.loads(envelope["assertion"])
    field = "token" if fault == "bad-identity-signature" else "fingerprints"
    parts = assertion[field].split(".")
    signature = parts[-1]
    parts[-1] = ("A" if signature[0] != "A" else "B") + signature[1:]
    assertion[field] = ".".join(parts)
    envelope["assertion"] = json.dumps(assertion, separators=(",", ":"))
    encoded = base64.b64encode(json.dumps(envelope, separators=(",", ":")).encode()).decode()
    lines[identity_line] = f"a=identity:{encoded}{newline}"
    return "".join(lines)


class TamperedIdentitySigner(nethernet.IdentitySigner):
    def __init__(self, private_key, fault):
        super().__init__(private_key, domain="test.example")
        self.fault = fault

    def sign(self, sdp):
        return tamper_identity(super().sign(sdp), self.fault)


async def test_adapter_signs_session_identity_and_verifies_server_with_optional_pin(
    monkeypatch, caplog, capsys
):
    server_key = nethernet.generate_operator_key()
    identity = create_identity("signed test", "00000000-0000-0000-0000-000000000001")
    signer = nethernet.IdentitySigner(server_key, domain="test.example")
    expected_pin = cpk_digest(server_key.public_key())
    offers = []
    callbacks = []

    connect_http = transport.connect_http

    async def record_callback(*args, **kwargs):
        callbacks.append(kwargs.get("on_server_identity"))
        return await connect_http(*args, **kwargs)

    monkeypatch.setattr(transport, "connect_http", record_callback)

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
        url = f"http://127.0.0.1:{server.bound_port}"
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")
            with caplog.at_level(logging.WARNING):
                caplog.clear()
                conn = await transport._open_connection(options(url, ""), identity)
                try:
                    payload = b"\x01wire-without-raknet-prefix" * 12000
                    await conn.send(payload)
                    assert await asyncio.wait_for(conn.recv(), 5) == payload
                finally:
                    await conn.close()
                    await conn.close()
        assert not caught_warnings
        assert not caplog.records
        captured = capsys.readouterr()
        assert captured.out == captured.err == ""

        conn = await transport._open_connection(options(url, expected_pin), identity)
        try:
            payload = b"\x01wire-without-raknet-prefix" * 12000
            await conn.send(payload)
            assert await asyncio.wait_for(conn.recv(), 5) == payload
        finally:
            await conn.close()
            await conn.close()
    assert len(offers) == 2
    assert len(callbacks) == 2
    assert all(callback is not None for callback in callbacks)


@pytest.mark.parametrize(
    "assertion", ["missing", "malformed", "bad-identity-signature", "bad-fingerprint-signature"]
)
async def test_adapter_rejects_unverified_server_identity_without_pin(assertion):
    key = nethernet.generate_operator_key()
    signer = None if assertion == "missing" else TamperedIdentitySigner(key, assertion)
    identity = create_identity("invalid server test", "00000000-0000-0000-0000-000000000001")

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
                options(f"http://127.0.0.1:{server.bound_port}", ""),
                identity,
            )


async def test_adapter_rejects_mismatched_server_pin():
    key = nethernet.generate_operator_key()
    other = nethernet.generate_operator_key()
    identity = create_identity("wrong pin test", "00000000-0000-0000-0000-000000000001")

    async def handler(conn):
        await conn.wait_closed()

    async with nethernet.serve_http(
        handler,
        host="127.0.0.1",
        ice_servers=[RTCIceServer(urls=[])],
        identity_signer=nethernet.IdentitySigner(key, domain="test.example"),
    ) as server:
        with pytest.raises(nethernet.InvalidIdentity, match="pin mismatch"):
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
