import dataclasses
import gc

import bedrock_protocol as bp
import pytest
from peer import Peers, until
from test_statistics import framed, uvarint

import bedrock_bot as api


async def test_packet_events_interleave_recovery_before_business_and_retain_native_ownership(monkeypatch):
    peers = Peers(monkeypatch, compression_threshold=65535)
    events, observations = [], []

    def callback(bot, event):
        events.append(event)
        if isinstance(event, api.PacketDecodedEvent):
            packet_id = event.packet.header.packet_id
            observations.append((packet_id, bot.capture_statistics().play_packet_counts_in[packet_id],
                                 bot.get_status().health))

    interests = api.event_mask(api.EventKind.packet_decoded) | api.event_mask(api.EventKind.decode_error)
    async with api.Runtime(callback, events=interests) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", validate_all_packets=True,
                                            recover_packet_errors=True))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        before = bot.capture_statistics()
        events.clear()
        observations.clear()
        peer.incoming.put_nowait(framed(
            peer.codec.encode_packet(peer.p.SetHealthPacket(health=17)),
            uvarint(42),
            peer.codec.encode_packet(bp.UnknownPacket(packet_id=900, body=b"retained native payload")),
            peer.codec.encode_packet(peer.p.SetHealthPacket(health=18), sender_subclient_id=1),
            peer.codec.encode_packet(peer.p.SetHealthPacket(health=16)),
        ))
        await until(lambda: bot.get_status().health == 16)
        assert [type(event) for event in events] == [api.PacketDecodedEvent, api.DecodeErrorEvent,
                                                    api.PacketDecodedEvent, api.PacketDecodedEvent,
                                                    api.PacketDecodedEvent]
        assert events[1].packet_id == 42 and events[1].recoverable is True
        assert [entry[0] for entry in observations] == [42, 900, 42, 42]
        assert [entry[1] for entry in observations] == [before.play_packet_counts_in[42] + 1,
                                                       before.play_packet_counts_in[900] + 1,
                                                       before.play_packet_counts_in[42] + 2,
                                                       before.play_packet_counts_in[42] + 3]
        assert [entry[2] for entry in observations] == [None, 17, 17, 17]
        retained = events[2]
        assert not hasattr(retained, "copy") and not hasattr(retained, "to_dict")
        body = memoryview(retained.packet.payload.body)
        assert body.readonly and events[3].packet.header.sender_subclient_id == 1
        with pytest.raises(dataclasses.FrozenInstanceError):
            retained.packet = None
        with pytest.raises(AttributeError):
            retained.packet.header.packet_id = 1
        with pytest.raises(AttributeError):
            retained.packet.payload.body = b"changed"
    events.clear()
    gc.collect()
    assert retained.packet.protocol == 2193
    assert retained.packet.header.packet_id == 900
    assert body.tobytes() == retained.packet.payload.body.to_bytes() == b"retained native payload"


@pytest.mark.parametrize("subscribe", [False, True])
async def test_packet_event_is_opt_in_and_can_be_disabled_at_ready(monkeypatch, subscribe):
    peers = Peers(monkeypatch)
    events = []

    def callback(_bot, event):
        events.append(event)
        if isinstance(event, api.StateEvent) and event.state == api.BotState.ready:
            runtime.set_event_interests(api.DEFAULT_EVENTS)

    interests = api.DEFAULT_EVENTS | (api.event_mask(api.EventKind.packet_decoded) if subscribe else 0)
    async with api.Runtime(callback, events=interests) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        assert any(isinstance(event, api.PacketDecodedEvent) for event in events) is subscribe
        events.clear()
        peer = peers.connections[-1]
        peer.send(peer.p.SetHealthPacket(health=19))
        await until(lambda: bot.get_status().health == 19)
        assert not any(isinstance(event, api.PacketDecodedEvent) for event in events)


async def test_packet_callback_disconnect_stops_business_handling_and_later_packet_events(monkeypatch):
    peers = Peers(monkeypatch)
    packets = []

    def callback(bot, event):
        if event.packet.header.packet_id == 42:
            packets.append(event)
            bot.disconnect()

    async with api.Runtime(callback, events=api.event_mask(api.EventKind.packet_decoded)) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        before = bot.capture_statistics()
        peer.send(peer.p.SetHealthPacket(health=17), peer.p.SetHealthPacket(health=16))
        await until(lambda: peer.closed)
        assert len(packets) == 1 and bot.get_status().health is None
        assert bot.capture_statistics().play_packet_counts_in[42] == before.play_packet_counts_in[42] + 1
