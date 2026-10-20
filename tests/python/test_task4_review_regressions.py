"""Independent legacy regressions through controlled native-codec peer bytes."""

import bedrock_protocol as bp
import pytest
from peer import actor_packet, empty_item, until
from trade_peer import (
    deliver,
    open_packet,
    request_events,
    requests,
    response_packet,
    scene,
    sent,
    slot_events,
    stack,
    trade_packet,
)

import bedrock_bot as api


async def prepare_window_trigger(bot, peer):
    await deliver(bot, peer, actor_packet(peer.p))
    bot.interact_actor(bot.get_observed_entities()[0].ref)
    await until(lambda: any(packet.packet_id == 30 for packet in peer.received))
    assert bot.get_trade_window() is None


@pytest.mark.parametrize("version", [1001, 2193])
@pytest.mark.parametrize("trigger", ["open", "trade"])
@pytest.mark.parametrize("phase", ["queued", "submitted"])
async def test_new_window_settles_player_move_before_container_callback(monkeypatch, version, trigger, phase):
    operation = None
    callbacks = []

    def callback(bot, event):
        if operation is not None and isinstance(event, (api.ItemRequestEvent, api.ContainerEvent)):
            callbacks.append((type(event), operation.phase, operation.result))

    async with scene(monkeypatch, version, callback=callback) as (bot, peer, events):
        await prepare_window_trigger(bot, peer)
        if phase == "queued":
            peer.capacity.clear()
        operation = bot.begin_move_item(bot.player_slot(9), bot.player_slot(0), 3)
        assert operation.key.window is None
        if phase == "submitted":
            await sent(peer)
        else:
            await peer.reserving.wait()
            assert operation.phase == api.ItemOperationPhase.queued
            assert not requests(peer)
        events.clear()
        await deliver(bot, peer, open_packet(peer.p) if trigger == "open" else trade_packet(peer.p))
        assert operation.result is not None, "new window left the player move pending"
        retained = operation.result
        expected = api.ItemRequestOutcome.cancelled if phase == "queued" else api.ItemRequestOutcome.indeterminate
        assert retained.outcome == expected and retained.key.window is None
        assert bot.get_trade_window().ref.window_id == 5
        assert callbacks == [
            (api.ItemRequestEvent, api.ItemOperationPhase.terminal, retained),
            (api.ContainerEvent, api.ItemOperationPhase.terminal, retained),
        ]
        expected_states = (
            [api.SlotState.occupied, api.SlotState.empty]
            if phase == "queued"
            else [api.SlotState.unknown, api.SlotState.unknown]
        )
        assert [slot.state for slot in retained.touched_slots] == expected_states
        assert [slot.slot.index for slot in retained.touched_slots] == [9, 0]
        assert bot.get_item(bot.player_slot(20)).count == 6
        peer.capacity.set()
        probes = sum(packet.packet_id == 115 for packet in peer.received)
        bot.probe_latency()
        await until(lambda: sum(packet.packet_id == 115 for packet in peer.received) == probes + 1)
        assert len(requests(peer)) == (0 if phase == "queued" else 1)
        await deliver(bot, peer, response_packet(peer.p, rows=[(29, 9, 7, 101), (28, 0, 3, 201)]))
        assert operation.result is retained and len(request_events(events)) == 1
        assert not slot_events(events)


@pytest.mark.parametrize("version", [1001, 2193])
@pytest.mark.parametrize("trigger", ["open", "trade"])
async def test_window_settlement_callback_invalidation_suppresses_container(monkeypatch, version, trigger):
    operation = None
    callbacks, closing = [], []

    def callback(bot, event):
        if operation is None:
            return
        if isinstance(event, api.ItemRequestEvent):
            callbacks.append(("request", operation.phase, operation.result))
            closing.append(bot.disconnect("window settlement callback"))
        elif isinstance(event, api.ContainerEvent):
            callbacks.append(("container", operation.phase, operation.result))

    async with scene(monkeypatch, version, callback=callback) as (bot, peer, _):
        await prepare_window_trigger(bot, peer)
        operation = bot.begin_move_item(bot.player_slot(9), bot.player_slot(0), 3)
        await sent(peer)
        peer.send(open_packet(peer.p) if trigger == "open" else trade_packet(peer.p))
        await until(lambda: bool(callbacks))
        assert callbacks[0][0] == "request", "container callback ran before window settlement"
        assert callbacks[0][1] == api.ItemOperationPhase.terminal
        assert callbacks[0][2].outcome == api.ItemRequestOutcome.indeterminate
        assert len(closing) == 1
        await closing[0]
        assert peer.closed and len(callbacks) == 1
        assert operation.result is callbacks[0][2]


@pytest.mark.parametrize("version", [1001, 2193])
@pytest.mark.parametrize("property_name", ["metadata_vec3", "signal_fire_at_position"])
async def test_vector_payload_properties_return_domain_vec3(monkeypatch, version, property_name):
    async with scene(monkeypatch, version) as (bot, peer, events):
        p = peer.p
        if property_name == "metadata_vec3":
            original = actor_packet(p)
            fields = {key: getattr(original, key) for key in original.to_dict()}
            fields["data"] = p.SynchedActorData.CopyableDataList(
                data=[
                    p.DataItemEntry(
                        id=8, payload=bp.Variant(8, p.DataItemVec3Payload(value=p.Vec3(x=1.5, y=2.5, z=3.5)))
                    )
                ]
            )
            await deliver(bot, peer, p.AddActorPacket(**fields))
            entry = bot.capture_actor(91).metadata.entries[0]
            assert entry.kind == "vec3"
            value = entry.value
            expected = api.Vec3(1.5, 2.5, 3.5)
        else:
            await deliver(
                bot,
                peer,
                p.ActorEventPacket(
                    runtime_id=91,
                    event_id=p.ActorEvent.FIREWORKS_EXPLODE,
                    data=7,
                    fire_at_position=p.Vec3(x=12, y=13, z=14),
                ),
            )
            signal = next(event.packet for event in events if isinstance(event, api.ActorSignalEvent))
            value = signal.fire_at_position
            expected = api.Vec3(12, 13, 14)
        assert type(value) is api.Vec3, f"{property_name} leaked {type(value)!r}"
        assert value == expected
        assert (value.x, value.y, value.z) == (expected.x, expected.y, expected.z)


def normal_transaction(p):
    actions = []
    for inventory_id, index, item in [
        (5, 0, stack(p, 43, 4, 301)),
        (0, 9, stack(p, count=7)),
        (5, 1, stack(p, 42, 2, 302)),
    ]:
        actions.append(
            p.InventoryAction(
                source=p.InventorySource(
                    type=p.InventorySourceType.CONTAINER_INVENTORY,
                    container_id=p.ContainerID(inventory_id),
                    flags=None,
                ),
                slot=index,
                from_item_descriptor=empty_item(p),
                to_item_descriptor=item,
            )
        )
    return p.InventoryTransactionPacket(
        legacy_request_id=0,
        legacy_set_item_slots=None,
        transaction=bp.Variant(0, p.NormalTransactionData(transaction=p.InventoryTransaction(actions=actions))),
    )


def merged_counts(bot):
    trade, player = bot.capture_inventory(5), bot.capture_inventory(0)
    return trade.slots[0].stack_size, player.slots[9].stack_size, trade.slots[1].stack_size


@pytest.mark.parametrize("version", [1001, 2193])
async def test_normal_transaction_emits_first_seen_inventory_order_after_all_merges(monkeypatch, version):
    observations = []
    armed = False

    def callback(bot, event):
        if armed and isinstance(event, api.InventoryEvent):
            observations.append((event.inventory_id, merged_counts(bot)))

    async with scene(monkeypatch, version, callback=callback) as (bot, peer, events):
        armed = True
        events.clear()
        await deliver(bot, peer, normal_transaction(peer.p))
        assert observations == [(5, (4, 7, 2)), (0, (4, 7, 2))]
        assert not slot_events(events)


@pytest.mark.parametrize("version", [1001, 2193])
async def test_first_normal_inventory_callback_invalidation_suppresses_remaining_callbacks(monkeypatch, version):
    observations, closing = [], []
    armed = False

    def callback(bot, event):
        if armed and isinstance(event, api.InventoryEvent):
            observations.append(merged_counts(bot))
            closing.append(bot.disconnect("first inventory callback"))

    async with scene(monkeypatch, version, callback=callback) as (_bot, peer, _):
        armed = True
        peer.send(normal_transaction(peer.p))
        await until(lambda: bool(closing))
        await closing[0]
        assert peer.closed and observations == [(4, 7, 2)] and len(closing) == 1
