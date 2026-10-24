import gc

import bedrock_protocol as bp
import pytest
from peer import actor_packet
from trade_peer import (
    deliver,
    open_packet,
    open_trade,
    response_info,
    response_packet,
    scene,
    sent,
    stack,
    trade_packet,
)

import bedrock_bot as api
from bedrock_bot._projection import project


@pytest.mark.parametrize("version", [1001, 2193])
def test_versioned_projection_uses_native_provenance_and_types_without_module_metadata(monkeypatch, version):
    p = getattr(bp, f"v{version}")
    codec = bp.Codec(version)

    def decode(packet):
        return codec.decode_packet(codec.encode_packet(packet)).payload

    cases = [
        (
            p.SynchedActorData.CopyableDataList(data=[]),
            decode(actor_packet(p)).data,
            bp.v1001.SynchedActorData.CopyableDataList,
            bp.v2193.SynchedActorData.CopyableDataList,
        ),
        (open_packet(p), decode(open_packet(p)), bp.v1001.ContainerOpenPacket, bp.v2193.ContainerOpenPacket),
        (trade_packet(p), decode(trade_packet(p)), bp.v1001.UpdateTradePacket, bp.v2193.UpdateTradePacket),
        (
            response_info(p),
            decode(response_packet(p)).responses[0],
            bp.v1001.ItemStackResponseInfo,
            bp.v2193.ItemStackResponseInfo,
        ),
    ]
    for constructed, decoded, type1001, type2193 in cases:
        assert constructed.protocol is None and decoded.protocol == version
        monkeypatch.setattr(type(constructed), "__module__", "unrelated_module")
        expected = 1001 if type1001 is type2193 else version
        assert project(constructed).protocol == expected
        assert project(constructed).copy().to_dict()["protocol"] == expected
        assert project(decoded).protocol == version
        assert project(decoded).copy().to_dict()["protocol"] == version


def test_shared_native_type_uses_decoded_provenance_and_constructed_1001_fallback(monkeypatch):
    codec = bp.Codec(2193)
    constructed = open_packet(bp.v2193)
    decoded = codec.decode_packet(codec.encode_packet(constructed)).payload
    monkeypatch.setattr(bp.v1001, "ContainerOpenPacket", bp.v2193.ContainerOpenPacket)
    assert constructed.protocol is None
    assert api.ContainerSnapshot(open=constructed).open.protocol == 1001
    projected = api.ContainerSnapshot(open=decoded).open
    assert projected.protocol == 2193 and projected.copy().to_dict()["protocol"] == 2193


@pytest.mark.parametrize("version", [1001, 2193])
async def test_owned_capture_copy_and_legacy_item_projection(monkeypatch, version):
    async with scene(monkeypatch, version) as (bot, peer, _):
        original = bot.capture_snapshot()
        copied = original.copy()
        slot = bot.get_item(bot.player_slot(9))
        projected = slot.item
        expected = {
            "protocol": version,
            "kind": f"serialized_{version}",
            "id": 42,
            "stack_size": 10,
            "aux_value": 0,
            "block_runtime_id": 0,
            "user_data_buffer": b"\0" * 10,
            "net_id": 101,
            "net_id_kind": "stack" if version == 1001 else "raw",
        }
        assert projected.to_dict() == expected
        assert original.to_dict()["inventories"][0]["slots"][9] == expected
        assert set(slot.to_dict()) == {"slot", "identifier", "item"}
        assert slot.copy().slot.bot is slot.slot.bot
        assert slot.to_dict()["slot"]["area"] is api.InventoryArea.player
        assert slot.to_dict()["slot"]["bot"] is slot.slot.bot
        assert copied.inventories[0].slots[9] is not projected
        buffer = memoryview(copied.inventories[0].slots[9].user_data_buffer)
        iterator = iter(original.inventories[0].slots)
        registry_iterator = iter(original.item_registry)
        operation = bot.begin_move_item(bot.player_slot(9), bot.player_slot(0), 3)
        await sent(peer)
        peer.send(response_packet(peer.p, rows=[(29, 9, 7, 101), (28, 0, 3, 201)]))
        result = await operation.wait(2)
        assert result.touched_slots.to_list()[0]["item"]["stack_size"] == 7
        assert result.copy().key.bot is result.key.bot
        assert result.to_dict()["outcome"] is api.ItemRequestOutcome.completed
        assert original.inventories[0].slots[9].stack_size == 10
        await bot.reconnect()
        await bot.wait_ready(2)
        assert bot.capture_inventory(0) is None
    del original, copied, projected, bot, peer
    gc.collect()
    assert buffer.readonly and buffer.tobytes() == b"\0" * 10
    assert next(iterator) == 0
    assert next(registry_iterator).name == "minecraft:emerald"


@pytest.mark.parametrize("version", [1001, 2193])
async def test_versioned_payload_and_event_dictionaries(monkeypatch, version):
    async with scene(monkeypatch, version) as (bot, peer, events):
        window = await open_trade(bot, peer)
        offers = bot.get_trade_offers(window)
        assert offers.source.protocol == version
        assert offers.source.to_dict() == offers.to_dict()["source"]
        assert offers.copy().window.bot is window.bot
        assert type(offers.source.to_dict()["container_id"]) is int
        assert offers.offers.to_list()[0]["ref"]["recipe_net_id"] == 123
        event = next(event for event in events if isinstance(event, api.ContainerEvent))
        assert set(event.to_dict()) == {"container", "reference", "server_closed"}
        operation = bot.begin_trade(offers.offers[0].ref, bot.player_slot(10))
        await sent(peer)
        peer.send(response_packet(peer.p, rows=[(47, 4, 4, 301), (29, 10, 1, 501)]))
        result = await operation.wait(2)
        event = next(event for event in events if isinstance(event, api.ItemRequestEvent))
        assert set(event.to_dict()) == {"session_id", "request_id", "outcome", "response", "reason", "bot", "window"}
        assert event.to_dict()["bot"] is window.bot
        assert event.copy().bot is event.bot
        assert event.response.to_dict() == event.to_dict()["response"] == result.to_dict()["response"]
        assert event.response.protocol == version
        assert type(event.response.client_request_id) is int
        assert type(event.response.containers[0].slots[0].item_stack_net_id) is int


@pytest.mark.parametrize("alternative", [None, 0, 1, 2])
def test_item_net_id_alternatives(alternative):
    p = bp.v1001
    net_id = (
        None
        if alternative is None
        else bp.Variant(
            alternative, (p.ItemStackNetId, p.ItemStackRequestId, p.ItemStackLegacyRequestId)[alternative](raw_id=17)
        )
    )
    item = p.SerializedNetworkItemStackDescriptor(
        id=42, stack_size=2, aux_value=0, net_id_variant=net_id, block_runtime_id=0, user_data_buffer=b"\xff"
    )
    observation = api.SlotObservation(api.InventorySlot(api.BotToken(), 1, api.InventoryArea.player, 0), item)
    expected_kind = None if alternative is None else ("stack", "request", "legacy_request")[alternative]
    assert observation.item.net_id_kind == expected_kind
    assert observation.item.net_id == (None if alternative is None else 17)
    assert observation.stack_id == (17 if alternative == 0 else None)
    assert observation.copy().item.to_dict() == observation.item.to_dict()


@pytest.mark.parametrize("version", [1001, 2193])
async def test_all_metadata_kinds_native_ownership_and_typed_nbt(monkeypatch, version):
    async with scene(monkeypatch, version) as (bot, peer, events):
        p = peer.p
        mutable = bytearray(b"\0\x7f\x80\xff")
        nbt = bp.Nbt(
            bp.NbtType.COMPOUND,
            {
                "bytes": bp.Nbt(bp.NbtType.BYTE_ARRAY, mutable),
                "typed": bp.Nbt(bp.NbtType.LIST, [], element_type=bp.NbtType.INT),
                "long": bp.Nbt(bp.NbtType.INT64, 9007199254740993),
            },
        )
        payloads = [
            p.DataItemBytePayload(value=-7),
            p.DataItemShortPayload(value=-1234),
            p.DataItemIntPayload(value=-123456),
            p.DataItemFloatPayload(value=1.5),
            p.DataItemStringPayload(value="large" * 13000),
            p.DataItemCompoundTagPayload(value=nbt),
            p.DataItemPosPayload(value=p.BlockPos(x=-1, y=2, z=-3)),
            p.DataItemInt64Payload(value=9007199254740993),
            p.DataItemVec3Payload(value=p.Vec3(x=1.5, y=2.5, z=3.5)),
        ]
        original = actor_packet(p)
        values = {key: getattr(original, key) for key in original.to_dict()}
        values["data"] = p.SynchedActorData.CopyableDataList(
            data=[p.DataItemEntry(id=i, payload=bp.Variant(i, value)) for i, value in enumerate(payloads)]
        )
        await deliver(bot, peer, p.AddActorPacket(**values))
        mutable[:] = b"xxxx"
        actor = bot.capture_actor(91)
        copied = actor.copy()
        entries = copied.metadata.entries
        kinds = ["byte", "short", "int", "float", "string", "compound", "block_pos", "int64", "vec3"]
        assert [entry.kind for entry in entries] == kinds
        assert actor.metadata.to_dict() == actor.to_dict()["metadata"]
        assert actor.metadata.protocol == version
        assert entries[7].value == 9007199254740993
        assert entries[6].to_dict() == {"id": 6, "kind": "block_pos", "value": {"x": -1, "y": 2, "z": -3}}
        assert entries[8].to_dict()["value"] == {"x": 1.5, "y": 2.5, "z": 3.5}
        subtree = copied.metadata.entries[5].value
        buffer = memoryview(subtree["bytes"])
        iterator = iter(entries)
        native_iterator = iter(actor.metadata.data)
        assert buffer.format == "b" and buffer.readonly and list(buffer) == [0, 127, -128, -1]
        assert subtree["typed"].to_dict() == {"type": "list", "element_type": "int", "value": []}
        assert copied.metadata._native is not actor.metadata._native
        assert copied.metadata.wire_bytes() == actor.metadata.wire_bytes()
        await bot.reconnect()
        await bot.wait_ready(2)
        events.clear()
    del bot, peer, actor, copied, entries, subtree, payloads, original, values, nbt, mutable
    gc.collect()
    assert buffer.tobytes() == b"\0\x7f\x80\xff"
    assert next(iterator).value == -7
    assert next(native_iterator).payload.value.value == -7


def test_network_and_raw_zero_item_projection():
    p = bp.v1001
    item = p.NetworkItemStackDescriptor(
        id=42,
        stack_size=2,
        aux_value=0,
        net_id=p.ItemStackNetId(raw_id=17),
        block_runtime_id=0,
        user_data_buffer=b"data",
    )
    slot = api.InventorySlot(api.BotToken(), 1, api.InventoryArea.player, 0)
    observation = api.SlotObservation(slot, item)
    assert observation.stack_id == 17
    projected = observation.item
    assert projected.protocol is None and projected.kind == "network"
    assert projected.net_id == 17 and projected.net_id_kind == "stack"
    raw = api.SlotObservation(slot, stack(bp.v2193, stack_id=0)).item
    assert raw.net_id == 0 and raw.net_id_kind == "raw"
    assert raw.to_dict()["net_id"] == 0
