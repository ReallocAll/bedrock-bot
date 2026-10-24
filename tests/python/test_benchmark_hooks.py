import dataclasses

import bedrock_protocol as bp
import pytest
from peer import Peer, Peers, until
from test_statistics import framed, uvarint

import bedrock_bot as api


@pytest.mark.parametrize("protocol", [1001, 2193])
@pytest.mark.parametrize("load_screen", [False, True])
async def test_loading_option_covers_spawn_and_dimension(monkeypatch, protocol, load_screen):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=protocol, load_screen=load_screen))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        p = peer.p
        assert sum(packet.packet_id == 312 for packet in peer.received) == 2 * load_screen
        peer.send(p.ChangeDimensionPacket(dimension_id=1, pos=p.Vec3(x=1, y=2, z=3), respawn=False,
                                         loading_screen_id=73))
        await until(lambda: bot.get_status().dimension == 1)
        bot._session.dimension_deadline = runtime._loop.time()
        await until(lambda: any(packet.packet_id == 36 for packet in peer.received))
        loading = [packet for packet in peer.received if packet.packet_id == 312]
        assert len(loading) == 4 * load_screen
        if load_screen:
            assert [packet.loading_screen_id for packet in loading] == [None, None, 73, 73]
        assert bot.get_status().state == api.BotState.ready


@pytest.mark.parametrize("radius", [4, 16])
async def test_play_counts_start_per_packet_inside_spawn_batch(monkeypatch, radius):
    original_spawn = Peer.spawn

    def spawn(self):
        original_spawn(self)
        last = self.incoming.get_nowait()
        # Move the existing pre-spawn batches out of the way and append a health
        # packet to the batch that completes spawn.
        pending = [last]
        while not self.incoming.empty():
            pending.append(self.incoming.get_nowait())
        final = [decoded.payload for decoded in self.batch.decode(pending.pop())]
        for data in pending:
            self.incoming.put_nowait(data)
        self.send(*final, self.p.SetHealthPacket(health=17))

    monkeypatch.setattr(Peer, "spawn", spawn)
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", chunk_radius=radius))
        await bot.wait_ready(2)
        await until(lambda: bot.get_status().health == 17)
        peer = peers.connections[-1]
        stats = bot.capture_statistics()
        assert stats.play_packet_counts_in[2] == stats.play_packet_counts_in[70] == 0
        assert stats.play_packet_counts_in[42] == 1
        assert stats.play_packet_counts_out[113] == 0
        assert stats.packet_counts_out[113] == 1
        assert stats.play_packet_counts_out[69] == (radius != 16)
        assert stats.packet_counts_out[69] == 1 + (radius != 16)
        assert stats.play_packet_counts_out[312] == 2
        assert len(stats.play_packet_counts_in) == len(stats.play_packet_counts_out) == 1024
        assert [packet.chunk_radius for packet in peer.received if packet.packet_id == 69] == (
            [16] if radius == 16 else [16, radius]
        )
        with pytest.raises((TypeError, AttributeError)):
            stats.play_packet_counts_in[42] = 2
        with pytest.raises(dataclasses.FrozenInstanceError):
            stats.play_packet_counts_out = ()
        peer.send(peer.p.SetHealthPacket(health=16))
        await until(lambda: bot.get_status().health == 16)
        assert stats.play_packet_counts_in[42] == 1
        assert stats.copy().play_packet_counts_in.to_list() == stats.play_packet_counts_in.to_list()
        assert bot.capture_statistics().play_packet_counts_in[42] == 2


@pytest.mark.parametrize("validate_all", [False, True])
async def test_validate_all_decodes_packets_outside_business_selection(monkeypatch, validate_all):
    peers = Peers(monkeypatch, compression_threshold=65535)
    events = []
    async with api.Runtime(lambda _bot, event: events.append(event),
                           events=api.event_mask(api.EventKind.decode_error)) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", validate_all_packets=validate_all))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        before = bot.capture_statistics()
        peer.incoming.put_nowait(framed(uvarint(9)))
        if validate_all:
            await until(lambda: peer.closed)
            after = bot.capture_statistics()
            assert after.decode_errors == before.decode_errors + 1
            assert after.play_packet_counts_in.to_list() == before.play_packet_counts_in.to_list()
            assert len(events) == 1
            assert isinstance(events[0], api.DecodeErrorEvent)
            assert events[0].reason and events[0].recoverable is False
        else:
            await until(lambda: bot.capture_statistics().packets_in == before.packets_in + 1)
            assert bot.get_status().state == api.BotState.ready
            assert bot.capture_statistics().play_packet_counts_in[9] == 0


@pytest.mark.parametrize("option", ["load_screen", "validate_all_packets", "recover_packet_errors"])
async def test_benchmark_options_require_boolean_values(option):
    async with api.Runtime() as runtime:
        with pytest.raises(ValueError, match=option):
            runtime.add_bot(api.BotOptions(server_url="http://peer", **{option: 1}))


async def test_session_login_retains_declared_offline_xuid(monkeypatch):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", xuid="2535400000000042"))
        await bot.wait_ready(2)
        assert peers.connections[-1].login[1]["xid"] == "2535400000000042"
        assert bot.capture_options().xuid == "2535400000000042"


async def test_native_body_recovery_preserves_error_order_and_play_counts(monkeypatch):
    peers = Peers(monkeypatch, compression_threshold=65535)
    events = []
    interests = api.event_mask(api.EventKind.decode_error) | api.event_mask(api.EventKind.health)
    async with api.Runtime(lambda _bot, event: events.append(event), events=interests) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", validate_all_packets=True,
                                            recover_packet_errors=True))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        before = bot.capture_statistics()
        peer.incoming.put_nowait(framed(
            peer.codec.encode_packet(peer.p.SetHealthPacket(health=17)),
            uvarint(42),
            peer.codec.encode_packet(peer.p.SetHealthPacket(health=16)),
        ))
        await until(lambda: bot.get_status().health == 16)
        after = bot.capture_statistics()
        assert [type(event) for event in events] == [api.HealthEvent, api.DecodeErrorEvent, api.HealthEvent]
        assert events[1].packet_id == 42 and events[1].reason and events[1].recoverable is True
        assert after.decode_errors == before.decode_errors + 1
        assert after.packets_in == before.packets_in + 3
        assert after.packet_counts_in[42] == before.packet_counts_in[42] + 3
        assert after.play_packet_counts_in[42] == before.play_packet_counts_in[42] + 2
        peer.send(peer.p.SetHealthPacket(health=15))
        await until(lambda: bot.get_status().health == 15)
        assert bot.get_status().state == api.BotState.ready
        assert bot.capture_statistics().play_packet_counts_in[42] == before.play_packet_counts_in[42] + 3


@pytest.mark.parametrize("failure", ["framing", "header"])
async def test_native_recovery_keeps_structural_errors_fatal(monkeypatch, failure):
    peers = Peers(monkeypatch, compression_threshold=65535)
    errors = []
    async with api.Runtime(lambda _bot, event: errors.append(event),
                           events=api.event_mask(api.EventKind.decode_error)) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", validate_all_packets=True,
                                            recover_packet_errors=True))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        before = bot.capture_statistics()
        good = peer.codec.encode_packet(peer.p.SetHealthPacket(health=17))
        raw = {
            "framing": framed(good) + b"\xff",
            "header": framed(good, uvarint(1 << 14)),
        }[failure]
        peer.incoming.put_nowait(raw)
        await until(lambda: peer.closed)
        after = bot.capture_statistics()
        assert bot.get_status().state == api.BotState.failed
        assert len(errors) == 1 and errors[0].recoverable is False
        if failure == "framing":
            assert errors[0].packet_id == -1
        assert after.decode_errors == before.decode_errors + 1
        assert after.batch_bytes_in == before.batch_bytes_in + len(raw)
        assert after.packets_in == before.packets_in
        assert after.play_packet_counts_in.to_list() == before.play_packet_counts_in.to_list()


@pytest.mark.parametrize("validate_all", [False, True])
@pytest.mark.parametrize("recover", [False, True])
async def test_unknown_packet_is_success_and_next_batch_remains_usable(monkeypatch, validate_all, recover):
    peers = Peers(monkeypatch, compression_threshold=65535)
    errors = []
    async with api.Runtime(lambda _bot, event: errors.append(event),
                           events=api.event_mask(api.EventKind.decode_error)) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", validate_all_packets=validate_all,
                                            recover_packet_errors=recover))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        before = bot.capture_statistics()
        peer.send(
            peer.p.SetHealthPacket(health=17),
            bp.UnknownPacket(packet_id=900, body=b"owned opaque packet body"),
            peer.p.SetHealthPacket(health=16),
        )
        await until(lambda: bot.get_status().health == 16)
        after = bot.capture_statistics()
        assert after.packets_in == before.packets_in + 3
        assert after.packet_counts_in[900] == before.packet_counts_in[900] + 1
        assert after.play_packet_counts_in[42] == before.play_packet_counts_in[42] + 2
        assert after.play_packet_counts_in[900] == before.play_packet_counts_in[900] + validate_all
        assert after.decode_errors == before.decode_errors and not errors
        peer.send(peer.p.SetHealthPacket(health=15))
        await until(lambda: bot.get_status().health == 15)
        assert bot.get_status().state == api.BotState.ready
        assert bot.capture_statistics().play_packet_counts_in[42] == before.play_packet_counts_in[42] + 3
        assert bot.capture_statistics().decode_errors == before.decode_errors and not errors
