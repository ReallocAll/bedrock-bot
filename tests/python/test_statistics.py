import asyncio
import dataclasses

import bedrock_protocol as bp
import nethernet
import pytest
from aiortc import RTCIceServer
from peer import Peers, until

import endpilot as api


def uvarint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def framed(*packets):
    return b"\xff" + b"".join(uvarint(len(packet)) + packet for packet in packets)


@pytest.mark.parametrize("version", [1001, 2193])
async def test_histograms_count_unknown_filtered_and_subclient_headers(monkeypatch, version):
    peers = Peers(monkeypatch, compression_threshold=65535)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        previous = bot.capture_statistics()
        packet = peer.p.SetHealthPacket(health=17)
        raw = framed(
            peer.codec.encode_packet(packet),
            peer.codec.encode_packet(bp.UnknownPacket(packet_id=900, body=b"unknown")),
            uvarint(9) + b"unselected",
            peer.codec.encode_packet(peer.p.SetHealthPacket(health=0), sender_subclient_id=1),
        )
        peer.incoming.put_nowait(raw)
        await until(lambda: bot.capture_statistics().packets_in == previous.packets_in + 4)
        current = bot.capture_statistics()
        assert current.batch_bytes_in == previous.batch_bytes_in + len(raw)
        assert current.packet_counts_in[42] == previous.packet_counts_in[42] + 2
        assert current.play_packet_counts_in[42] == previous.play_packet_counts_in[42] + 2
        assert current.packet_counts_in[900] == previous.packet_counts_in[900] + 1
        assert current.packet_counts_in[9] == previous.packet_counts_in[9] + 1
        assert sum(current.packet_counts_in) == current.packets_in
        assert sum(current.packet_counts_out) == current.packets_out
        assert len(current.packet_counts_in) == len(current.packet_counts_out) == 1024
        assert type(current.packet_counts_in.to_list()[0]) is int
        assert current.copy().packet_counts_in.to_list() == current.packet_counts_in.to_list()
        assert bot.get_status().health == 17 and current.deaths == previous.deaths
        assert current.decode_errors == previous.decode_errors
        peer.send(peer.p.SetHealthPacket(health=16))
        await until(lambda: bot.get_status().health == 16)
        assert bot.get_status().state == api.BotState.ready


@pytest.mark.parametrize("failure,recover", [("framing", False), ("later_header", False),
                                            ("selected_body", False), ("framing", True),
                                            ("later_header", True)])
async def test_malformed_native_batch_counts_bytes_but_no_partial_histogram(monkeypatch, failure, recover):
    peers = Peers(monkeypatch, compression_threshold=65535)
    errors = []
    async with api.Runtime(lambda _bot, event: errors.append(event),
                           events=api.event_mask(api.EventKind.decode_error)) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", recover_packet_errors=recover))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        before = bot.capture_statistics()
        good = peer.codec.encode_packet(peer.p.SetHealthPacket(health=17))
        raw = {
            "framing": framed(good) + b"\xff",
            "later_header": framed(good, uvarint(1 << 14)),
            "selected_body": framed(good, uvarint(42)),
        }[failure]
        peer.incoming.put_nowait(raw)
        await until(lambda: peer.closed)
        current = bot.capture_statistics()
        assert current.batch_bytes_in == before.batch_bytes_in + len(raw)
        assert current.decode_errors == before.decode_errors + 1
        assert current.packets_in == before.packets_in
        assert current.packet_counts_in.to_list() == before.packet_counts_in.to_list()
        assert bot.get_status().state == api.BotState.failed

        assert len(errors) == 1 and errors[0].recoverable is False
        if failure == "framing":
            assert errors[0].packet_id == -1


async def test_unavailable_transport_counters_remain_none_across_lifecycle(monkeypatch):
    peers = Peers(monkeypatch)
    closed = []

    def callback(bot, event):
        if isinstance(event, api.StateEvent) and event.state == api.BotState.closed:
            closed.append(bot.capture_statistics())

    async with api.Runtime(callback) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        assert bot.capture_statistics().connected_bytes_sent is None
        assert bot.capture_statistics().connected_bytes_received is None
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        captured = bot.capture_statistics()
        assert (captured.connected_bytes_sent, captured.connected_bytes_received) == (None, None)
        peer.close_gate.clear()
        completion = bot.disconnect()
        await peer.closing.wait()
        assert not closed
        assert bot.capture_statistics().connected_bytes_sent is None
        peer.close_gate.set()
        await completion
        assert (closed[0].connected_bytes_sent, closed[0].connected_bytes_received) == (None, None)
        assert captured.connected_bytes_sent is None
        with pytest.raises(dataclasses.FrozenInstanceError):
            captured.connected_bytes_sent = 1
        await bot.reconnect()
        await bot.wait_ready(2)
        assert bot.capture_statistics().connected_bytes_sent is None
        assert closed[0].connected_bytes_received is None


async def test_outgoing_counts_only_after_successful_send(monkeypatch):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.pause_sender(bot)
        before = bot.capture_statistics()

        def boundary():
            assert bot.capture_statistics().packets_out == before.packets_out
            assert bot.capture_statistics().batch_bytes_out == before.batch_bytes_out

        peer.before_send = boundary
        bot.request_chunk_radius(4)
        peer.resume_sender()
        await until(lambda: bot.capture_statistics().packets_out > before.packets_out)
        after = bot.capture_statistics()
        assert after.packet_counts_out[69] == before.packet_counts_out[69] + 1
        assert after.play_packet_counts_out[69] == before.play_packet_counts_out[69] + 1
        assert after.batch_bytes_out > before.batch_bytes_out


async def test_application_statistics_through_actual_official_connection():
    from nethernet.identity import cpk_digest
    from peer import Peer

    key = nethernet.generate_operator_key()
    signer = nethernet.IdentitySigner(key, domain="statistics.example")
    finished = asyncio.Event()

    async def handler(connection):
        peer = Peer()

        async def outgoing():
            while True:
                await connection.send(await peer.incoming.get())

        sender = asyncio.create_task(outgoing())
        try:
            async for message in connection:
                peer.received_wire.append(message)
                previous = len(peer.received)
                peer.accept(message)
                for packet in peer.received[previous:]:
                    if packet.packet_id == 115:
                        peer.send(packet)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            finished.set()

    async with nethernet.serve_http(
        handler, host="127.0.0.1", ice_servers=[RTCIceServer(urls=[])], identity_signer=signer
    ) as server:
        async with api.Runtime() as runtime:
            bot = runtime.add_bot(
                api.BotOptions(
                    server_url=f"http://127.0.0.1:{server.bound_port}",
                    server_key_sha256=cpk_digest(key.public_key()),
                )
            )
            await bot.wait_ready(10)
            before = bot.capture_statistics()
            assert before.connected_bytes_sent is before.connected_bytes_received is None
            assert before.batch_bytes_out > 0 and before.batch_bytes_in > 0
            bot.probe_latency()
            await until(lambda: bot.capture_statistics().packet_counts_in[115] > before.packet_counts_in[115])
            await bot.disconnect()
            final = bot.capture_statistics()
            assert final.connected_bytes_sent is final.connected_bytes_received is None
            assert final.batch_bytes_out > before.batch_bytes_out
            assert final.batch_bytes_in > before.batch_bytes_in
            assert before.to_dict()["connected_bytes_sent"] is None
        await asyncio.wait_for(finished.wait(), 5)


async def test_python_business_validation_retains_counted_headers(monkeypatch):
    from peer import actor_packet

    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        packet = actor_packet(peer.p)
        values = {field: getattr(packet, field) for field in packet.to_dict()}
        values["pos"] = peer.p.Vec3(x=float("nan"), y=0, z=0)
        packet = peer.p.AddActorPacket(**values)
        before = bot.capture_statistics()
        peer.send(packet)
        await until(lambda: peer.closed)
        after = bot.capture_statistics()
        assert after.decode_errors == before.decode_errors + 1
        assert after.packets_in == before.packets_in + 1
        assert after.packet_counts_in[13] == before.packet_counts_in[13] + 1
        assert after.batch_bytes_in > before.batch_bytes_in
