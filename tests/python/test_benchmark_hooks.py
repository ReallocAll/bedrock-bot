import dataclasses

import pytest
from peer import Peer, Peers, until
from test_statistics import framed, uvarint

import endpilot as api


def dimension_ack(p, runtime_id):
    zero = p.BlockPos(x=0, y=0, z=0)
    return p.PlayerActionPacket(
        runtime_id=runtime_id,
        action=p.PlayerActionType.CHANGE_DIMENSION_ACK,
        pos=zero,
        result_pos=zero,
        face=-1,
    )


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
        assert [packet.packet_id for packet in peer.received if packet.packet_id in (113, 312)] == (
            [312, 312, 113] if load_screen else [113]
        )
        peer.send(p.ChangeDimensionPacket(dimension_id=1, pos=p.Vec3(x=1, y=2, z=3), respawn=False,
                                         loading_screen_id=73))
        await until(lambda: bot.get_status().dimension == 1)
        peer.send(dimension_ack(p, bot.get_status().actor_runtime_id))
        await until(lambda: any(packet.packet_id == 36 for packet in peer.received))
        loading = [packet for packet in peer.received if packet.packet_id == 312]
        assert len(loading) == 4 * load_screen
        if load_screen:
            assert [packet.loading_screen_id for packet in loading] == [None, None, 73, 73]
            assert [packet.loading_screen_packet_type for packet in loading[2:]] == [
                p.ServerboundLoadingScreenPacketType.START_LOADING_SCREEN,
                p.ServerboundLoadingScreenPacketType.END_LOADING_SCREEN,
            ]
        sequence = [packet.packet_id for packet in peer.received if packet.packet_id in (36, 312)]
        assert sequence[-3 if load_screen else -1:] == ([312, 312, 36] if load_screen else [36])
        assert bot.get_status().state == api.BotState.ready


@pytest.mark.parametrize("protocol", [1001, 2193])
async def test_dimension_confirmation_gates_completion_and_input(monkeypatch, protocol):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=protocol))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        p = peer.p
        assert not bot._options.validate_all_packets
        await until(lambda: any(packet.packet_id == 144 for packet in peer.received))
        peer.send(p.ChangeDimensionPacket(dimension_id=1, pos=p.Vec3(x=1, y=2, z=3), respawn=False,
                                         loading_screen_id=73))
        await until(lambda: bot._session.dimension_deadline is not None)
        await until(lambda: sum(packet.packet_id == 312 for packet in peer.received) == 3)
        with pytest.raises(RuntimeError, match="dimension is changing"):
            bot.select_hotbar(1)
        input_count = sum(packet.packet_id == 144 for packet in peer.received)
        peer.send(dimension_ack(p, bot.get_status().actor_runtime_id + 1))
        await until(lambda: bot.capture_statistics().packet_counts_in[36] == 1)
        bot._session.tick()
        changed_at = runtime._loop.time()
        original_time = runtime._loop.time
        runtime._loop.time = lambda: changed_at + 1.01
        try:
            bot._session.tick()
        finally:
            runtime._loop.time = original_time
        await bot._session.flush()
        assert sum(packet.packet_id == 144 for packet in peer.received) == input_count
        assert sum(packet.packet_id == 36 for packet in peer.received) == 0
        assert sum(packet.packet_id == 312 for packet in peer.received) == 3
        assert bot._session.dimension_deadline is not None

        peer.send(dimension_ack(p, bot.get_status().actor_runtime_id))
        await until(lambda: sum(packet.packet_id == 36 for packet in peer.received) == 1)
        assert bot._session.dimension_deadline is None
        bot._session.require_interaction()
        completion = [packet for packet in peer.received if packet.packet_id in (36, 312)]
        assert [packet.packet_id for packet in completion[-2:]] == [312, 36]
        assert completion[-2].loading_screen_id == 73
        assert completion[-2].loading_screen_packet_type == p.ServerboundLoadingScreenPacketType.END_LOADING_SCREEN
        await until(lambda: sum(packet.packet_id == 144 for packet in peer.received) > input_count)

        peer.send(dimension_ack(p, bot.get_status().actor_runtime_id))
        await until(lambda: bot.capture_statistics().packet_counts_in[36] == 2)
        await bot._session.flush()
        assert sum(packet.packet_id == 36 for packet in peer.received) == 1
        assert sum(packet.packet_id == 312 for packet in peer.received) == 4


@pytest.mark.parametrize("protocol", [1001, 2193])
async def test_missing_dimension_confirmation_times_out_without_completion(monkeypatch, protocol):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=protocol, spawn_timeout_ms=20))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.send(peer.p.ChangeDimensionPacket(
            dimension_id=1, pos=peer.p.Vec3(x=1, y=2, z=3), respawn=False, loading_screen_id=73
        ))
        await until(lambda: bot._session.dimension_deadline is not None)
        start = len(peer.received)
        await until(lambda: bot.get_status().state == api.BotState.failed)
        assert isinstance(bot._session.error, TimeoutError)
        assert not any(packet.packet_id in (36, 144) for packet in peer.received[start:])
        assert sum(packet.packet_id == 312 for packet in peer.received) == 3


@pytest.mark.parametrize("protocol", [1001, 2193])
async def test_disconnect_discards_dimension_completion(monkeypatch, protocol):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=protocol))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.pause_sender(bot)
        start = len(peer.received)
        peer.send(peer.p.ChangeDimensionPacket(
            dimension_id=1, pos=peer.p.Vec3(x=1, y=2, z=3), respawn=False, loading_screen_id=73
        ))
        await until(lambda: bot._session.dimension_deadline is not None)
        peer.send(dimension_ack(peer.p, bot.get_status().actor_runtime_id))
        await until(lambda: bot.capture_statistics().packet_counts_in[36] == 1)
        await bot.disconnect()
        assert peer.closed
        assert not any(packet.packet_id in (36, 312) for packet in peer.received[start:])


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
        assert stats.play_packet_counts_out[312] == 0
        assert stats.packet_counts_out[312] == 2
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
