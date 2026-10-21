import dataclasses

import bedrock_protocol as bp
import pytest
from peer import Peers, actor_packet, empty_item, until
from trade_peer import inventory_packet, registry_packet, response_info, stack

import bedrock_bot as api


def test_all_eighteen_event_materialization_contracts():
    p = bp.v2193
    token = api.BotToken()
    window = api.WindowRef(token, 1, 1, 5, 15, -92)
    key = api.ItemRequestKey(token, 1, -1, window)
    actor = api.ActorSnapshot(91, -92, "minecraft:villager_v2", api.Vec3(1, 2, 3))
    inventory = api.InventorySnapshot({0: empty_item(p)})
    signal = p.ActorEventPacket(runtime_id=91, event_id=p.ActorEvent(1), data=7, fire_at_position=None)
    examples = [
        (api.StateEvent(api.BotState.ready), {"state", "reason"}),
        (api.TickEvent(3, 0), {"tick", "lateness_us"}),
        (api.PositionEvent(api.Vec3(1, 2, 3)), {"position", "teleport", "correction"}),
        (
            api.ChunkEvent(api.ChunkEvent.Kind.column, 0, 1, 2, blob_ids=(42,)),
            {"kind", "dimension", "x", "z", "entries", "subchunk_limit", "blob_ids"},
        ),
        (api.DeathEvent(), set()),
        (api.RespawnEvent(), set()),
        (api.DimensionEvent(1, api.Vec3()), {"dimension", "position"}),
        (api.LatencyEvent(1000), {"round_trip_us"}),
        (api.ActorEvent(api.ActorEvent.Kind.added, actor), {"kind", "actor"}),
        (api.ActorSignalEvent(signal), {"packet"}),
        (api.TakeItemEvent(91, 1), {"item_runtime_id", "actor_runtime_id"}),
        (api.InventoryEvent(0, inventory), {"inventory_id", "inventory"}),
        (api.ItemRegistryEvent(registry_packet(p).items), {"items"}),
        (api.ContainerEvent(api.ContainerSnapshot(), window), {"container", "reference", "server_closed"}),
        (api.HealthEvent(19), {"health"}),
        (
            api.ItemRequestEvent(key, api.ItemRequestOutcome.completed, response_info(p), ""),
            {"session_id", "request_id", "outcome", "response", "reason", "bot", "window"},
        ),
        (
            api.ActorMotionEvent(1, 91, -92, api.Vec3(), api.Vec3(), 0, 0, 0, 0),
            {"session_id", "runtime_id", "unique_id", "position", "velocity", "pitch", "yaw", "head_yaw", "body_yaw"},
        ),
        (api.InventorySlotEvent(1, 0, 0, empty_item(p)), {"session_id", "inventory_id", "slot", "item"}),
    ]
    assert len(examples) == int(api.EventKind.count) == 18
    for event, keys in examples:
        assert set(event.to_dict()) == keys
        cloned = event.copy()
        assert cloned is not event and cloned.to_dict() == event.to_dict()
        materialized = event.to_dict()
        for field in dataclasses.fields(event):
            value = getattr(event, field.name)
            with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
                setattr(event, field.name, value)
            assert event.to_dict() == materialized
    assert examples[0][0].to_dict()["state"] is api.BotState.ready
    assert examples[3][0].blob_ids.to_list() == [42]
    assert examples[8][0].to_dict()["kind"] is api.ActorEvent.Kind.added
    assert type(examples[9][0].packet.to_dict()["event_id"]) is int
    assert examples[15][0].to_dict()["response"]["containers"] == []
    assert examples[15][0].copy().bot is token


@pytest.mark.parametrize("version", [1001, 2193])
async def test_complete_captures_maps_and_direct_admissions(monkeypatch, version):
    peers = Peers(monkeypatch)
    async with api.Runtime(events=api.ALL_EVENTS) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.send(registry_packet(peer.p), actor_packet(peer.p), inventory_packet(peer.p, {9: stack(peer.p)}))
        await until(lambda: bot.capture_inventory(0) is not None)
        snapshot = bot.capture_snapshot()
        assert snapshot.actors[91] is bot.capture_actor(91)
        assert bot.capture_actors().to_dict()[91]["position"] == {"x": 1.0, "y": 2.0, "z": 3.0}
        assert bot.capture_inventory(0).slots.to_dict()[9]["net_id"] == 101
        assert bot.capture_item_registry().items.to_list()[0]["name"] == "minecraft:emerald"
        assert bot.capture_item_registry().copy().observed
        assert bot.count_player_item("minecraft:emerald").observed_count == 10
        assert bot.count_player_item("minecraft:emerald").complete
        assert bot.get_trade_window() is None and bot.capture_container() is None
        entity = bot.get_observed_entities()[0]
        bot.attack_actor(entity.ref)
        bot.set_input(api.Input(position=bot.get_status().position, move=api.Vec2(0.1, 0)))
        bot.set_look(1, 2)
        bot.clear_look()
        bot.select_hotbar(0)
        bot.request_chunk_radius(4)
        bot.report_blob_status([42], [43])
        bot.request_sub_chunks(0, 1, 2, [-1, 0, 1])
        bot.probe_latency()
        await until(lambda: {30, 31, 69, 115, 135, 175}.issubset({packet.packet_id for packet in peer.received}))
        retained = snapshot.copy()
        with pytest.raises(TypeError):
            snapshot.inventories[0] = None
        with pytest.raises(TypeError):
            snapshot.inventories[0].slots[9] = None
        await bot.disconnect()
    assert retained.inventories[0].slots[9].stack_size == 10
