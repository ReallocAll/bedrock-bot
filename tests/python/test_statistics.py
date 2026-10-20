import asyncio
import dataclasses

import bedrock_protocol as bp
import nethernet
import pytest
from peer import Peers, until

import bedrock_bot as api


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
            peer.codec.encode_packet(packet, sender_subclient_id=1),
        )
        peer.incoming.put_nowait(raw)
        await until(lambda: bot.capture_statistics().packets_in == previous.packets_in + 4)
        current = bot.capture_statistics()
        assert current.batch_bytes_in == previous.batch_bytes_in + len(raw)
        assert current.packet_counts_in[42] == previous.packet_counts_in[42] + 2
        assert current.packet_counts_in[900] == previous.packet_counts_in[900] + 1
        assert current.packet_counts_in[9] == previous.packet_counts_in[9] + 1
        assert sum(current.packet_counts_in) == current.packets_in
        assert sum(current.packet_counts_out) == current.packets_out
        assert len(current.packet_counts_in) == len(current.packet_counts_out) == 1024
        assert type(current.packet_counts_in.to_list()[0]) is int
        assert current.copy().packet_counts_in.to_list() == current.packet_counts_in.to_list()
        assert bot.get_status().health == 17


@pytest.mark.parametrize("failure", ["framing", "later_header", "selected_body"])
async def test_malformed_native_batch_counts_bytes_but_no_partial_histogram(monkeypatch, failure):
    peers = Peers(monkeypatch, compression_threshold=65535)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
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


async def test_connected_snapshot_samples_close_and_new_connection(monkeypatch):
    peers = Peers(monkeypatch)
    closed = []

    def callback(bot, event):
        if isinstance(event, api.StateEvent) and event.state == api.BotState.closed:
            closed.append(bot.capture_statistics())

    async with api.Runtime(callback) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.dtls_byte_counts = nethernet.DtlsByteCounts(1234, 5678)
        captured = bot.capture_statistics()
        assert (captured.connected_bytes_sent, captured.connected_bytes_received) == (1234, 5678)
        peer.close_gate.clear()
        completion = bot.disconnect()
        await peer.closing.wait()
        assert not closed
        peer.dtls_byte_counts = nethernet.DtlsByteCounts(1277, 5701)
        peer.close_gate.set()
        await completion
        assert (closed[0].connected_bytes_sent, closed[0].connected_bytes_received) == (1277, 5701)
        assert captured.connected_bytes_sent == 1234
        with pytest.raises(dataclasses.FrozenInstanceError):
            captured.connected_bytes_sent = 1
        await bot.reconnect()
        await bot.wait_ready(2)
        assert bot.capture_statistics().connected_bytes_sent == 0
        assert closed[0].connected_bytes_received == 5701


@pytest.mark.parametrize("rejected", [False, True])
async def test_outgoing_counts_only_after_successful_send(monkeypatch, rejected):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.capacity.clear()
        before = bot.capture_statistics()

        def boundary():
            assert bot.capture_statistics().packets_out == before.packets_out

        peer.before_send = boundary
        if rejected:
            peer.send_error = RuntimeError("controlled send rejection")
        bot.request_chunk_radius(4)
        peer.capacity.set()
        if rejected:
            await until(lambda: peer.closed)
        else:
            await until(lambda: bot.capture_statistics().packets_out > before.packets_out)
        after = bot.capture_statistics()
        assert after.packet_counts_out[69] == before.packet_counts_out[69] + (not rejected)
        assert (after.batch_bytes_out > before.batch_bytes_out) is (not rejected)
        if rejected:
            assert after.packets_out == before.packets_out
            assert after.packet_counts_out.to_list() == before.packet_counts_out.to_list()


async def test_statistics_through_actual_installed_nethernets_dtls_graph():
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
                peer.accept(message)
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            finished.set()

    async with nethernet.serve_http(
        handler, host="127.0.0.1", ice_servers=[], limits=nethernet.ConnectionLimits(), identity_signer=signer
    ) as server:
        async with api.Runtime() as runtime:
            bot = runtime.add_bot(
                api.BotOptions(
                    server_url=f"http://127.0.0.1:{server.bound_port}",
                    server_key_sha256=cpk_digest(key.public_key()),
                )
            )
            await bot.wait_ready(10)
            counts = bot._session.connection.get_dtls_byte_counts()
            before = bot.capture_statistics()
            assert before.connected_bytes_sent == counts.bytes_sent > before.batch_bytes_out > 0
            assert before.connected_bytes_received == counts.bytes_received > before.batch_bytes_in > 0
            bot.probe_latency()
            await until(lambda: bot.capture_statistics().connected_bytes_sent > before.connected_bytes_sent)
            await bot.disconnect()
            final = bot.capture_statistics()
            counts = bot._session.connection.get_dtls_byte_counts()
            assert (final.connected_bytes_sent, final.connected_bytes_received) == (
                counts.bytes_sent,
                counts.bytes_received,
            )
            assert final.connected_bytes_sent > before.connected_bytes_sent
            assert before.to_dict()["connected_bytes_sent"] < final.connected_bytes_sent
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
