import asyncio

import pytest


@pytest.mark.parametrize("protocol", [1001, 2193])
async def test_native_raknet_loopback_frames_application_batch_and_uses_expected_qos(monkeypatch, protocol):
    pytest.importorskip("raknet")
    import raknet.asyncio

    from bedrock_bot import raknet_transport

    send_options = []
    original_send = raknet.asyncio.Connection.send

    async def observe_send(
        connection,
        data,
        *,
        reliability=raknet.asyncio.raw.PacketReliability.RELIABLE_ORDERED,
        priority=raknet.asyncio.raw.PacketPriority.HIGH_PRIORITY,
        channel=0,
    ):
        send_options.append((reliability, priority, channel))
        await original_send(connection, data, reliability=reliability, priority=priority, channel=channel)

    monkeypatch.setattr(raknet.asyncio.Connection, "send", observe_send)
    server = await raknet.asyncio.create_server(("127.0.0.1", 0))
    client = server_connection = None
    try:
        host, port = server.local_address
        options = type(
            "Options",
            (),
            {"server_url": f"raknet://{host}:{port}", "connect_timeout_ms": 20, "protocol": protocol},
        )()
        client = await raknet_transport.open_connection(options)
        server_connection = await asyncio.wait_for(server.accept(), 5)

        payload = b"\x00\xffbedrock batch payload"
        await client.send(payload)
        received = await asyncio.wait_for(server_connection.recv(), 5)
        assert received == b"\xfe" + payload
        assert send_options == [
            (
                raknet_transport.raknet.PacketReliability.RELIABLE_ORDERED,
                raknet_transport.raknet.PacketPriority.MEDIUM_PRIORITY,
                0,
            )
        ]

        await server_connection.send(b"\xfe" + b"server response")
        assert await asyncio.wait_for(client.recv(), 5) == b"server response"
    finally:
        if server_connection is not None:
            await server_connection.close()
        if client is not None:
            await client.close()
        await server.close()
