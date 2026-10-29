"""Codec-built server packets for trade tests."""

from contextlib import asynccontextmanager

import bedrock_protocol as bp
from peer import Peers, actor_packet, empty_item, until

import endpilot as api

CRAFT_RESULT_BODIES = {
    1001: bytes.fromhex("54010000000a00000000000000000000"),
    2193: bytes.fromhex("010111") + b"minecraft:emerald" + bytes.fromhex("000100000a00000000000000000000"),
}


def compound(value):
    return bp.Nbt(bp.NbtType.COMPOUND, value)


def tag(kind, value):
    return bp.Nbt(getattr(bp.NbtType, kind), value)


def recipe(*, second=False, **overrides):
    def item(name, count, damage):
        return compound({"Name": tag("STRING", name), "Count": tag("BYTE", count), "Damage": tag("SHORT", damage)})

    fields = {
        "netId": tag("INT", 123),
        "uses": tag("INT", 1),
        "maxUses": tag("INT", 8),
        "tier": tag("INT", 0),
        "unknown_recipe_data": tag("INT64", 9007199254740993),
        "buyA": item("minecraft:wheat", 3, 32767),
        "sell": item("minecraft:emerald", 1, 0),
    }
    if second:
        fields["buyB"] = item("minecraft:emerald", 2, 0)
    fields.update(overrides)
    return compound(fields)


def container(p, name, dynamic_id=None):
    return p.FullContainerName(name=p.ContainerEnumName(name), dynamic_id=dynamic_id)


def stack(p, item_id=42, count=10, stack_id=101, *, data=b"\0" * 10, aux=0, block=0, unconfirmed=False):
    if not item_id:
        return empty_item(p)
    net_id = stack_id
    if p is bp.v1001:
        net_id = (
            bp.Variant(1, p.ItemStackRequestId(raw_id=stack_id))
            if unconfirmed
            else bp.Variant(0, p.ItemStackNetId(raw_id=stack_id))
        )
    return p.SerializedNetworkItemStackDescriptor(
        id=item_id,
        stack_size=count,
        aux_value=aux,
        net_id_variant=net_id,
        block_runtime_id=block,
        user_data_buffer=data,
    )


def inventory_packet(p, slots=None, *, inventory_id=0, size=36):
    items = [empty_item(p) for _ in range(size)]
    for index, item in (slots or {}).items():
        items[index] = item
    return p.InventoryContentPacket(
        inventory_id=p.ContainerID(inventory_id),
        slots=items,
        full_container_name=container(p, 29 if inventory_id == 0 else 47),
        storage_item=empty_item(p),
    )


def slot_packet(p, index, item, *, inventory_id=0):
    return p.InventorySlotPacket(
        inventory_id=p.ContainerID(inventory_id), slot=index, item=item, full_container_name=None, storage_item=None
    )


def registry_packet(p, *, duplicate=False, component=False, missing=False):
    items = [
        p.ItemData(
            name=name,
            id=item_id,
            is_component_based=component,
            item_version=p.ItemVersion(0),
            component_data=compound({}),
        )
        for name, item_id in [("minecraft:emerald", 42), ("minecraft:wheat", 43), ("minecraft:potato", 44)]
        if not (missing and item_id == 42)
    ]
    return p.ItemRegistryPacket(items=items + ([items[0]] if duplicate else []))


def trade_packet(p, recipes=None, *, data=None, new_screen=True):
    if data is None:
        data = compound(
            {
                "Recipes": bp.Nbt(
                    bp.NbtType.LIST, [recipe()] if recipes is None else recipes, element_type=bp.NbtType.COMPOUND
                )
            }
        )
    return p.UpdateTradePacket(
        container_id=p.ContainerID(5),
        type=p.ContainerType(15),
        size=3,
        trader_tier=0,
        entity_unique_id=-92,
        last_trading_player=1,
        display_name="Trader",
        use_new_trade_screen=new_screen,
        using_economy_trade=True,
        data=data,
    )


def open_packet(p):
    return p.ContainerOpenPacket(
        container_id=p.ContainerID(5), type=p.ContainerType(15), pos=p.BlockPos(x=1, y=2, z=3), entity_unique_id=-92
    )


def close_packet(p, window_id=5):
    return p.ContainerClosePacket(
        container_id=p.ContainerID(window_id), container_type=p.ContainerType(15), server_initiated_close=False
    )


def response_info(p, request_id=-1, rows=(), *, result=0, absent=False):
    entries = []
    for row in rows:
        name, index, amount, net_id, *extras = row
        extra = extras[0] if extras else {}
        confirmed = p.ItemStackNetId(raw_id=net_id)
        if p is bp.v2193 and (not amount or extra.get("missing_id")):
            confirmed = None
        entries.append(
            p.ItemStackResponseContainerInfo(
                full_container_name=container(p, name, extra.get("dynamic_id")),
                slots=[
                    p.ItemStackResponseSlotInfo(
                        requested_slot=extra.get("requested_slot", index),
                        slot=index,
                        amount=amount,
                        item_stack_net_id=confirmed,
                        custom_name=p.RedactableString(unredacted_string=extra.get("name", ""), redacted_string=""),
                        durability_correction=extra.get("durability", 0),
                    )
                ],
            )
        )
    return p.ItemStackResponseInfo(
        result=p.ItemStackNetResult(result),
        client_request_id=p.ItemStackRequestId(raw_id=request_id),
        containers=None if result != 0 or (absent and p is bp.v2193) else entries,
    )


def response_packet(p, request_id=-1, rows=(), **kwargs):
    return p.ItemStackResponsePacket(responses=[response_info(p, request_id, rows, **kwargs)])


async def deliver(bot, peer, *packets):
    peer.marker = getattr(peer, "marker", 18) - 1
    if peer.marker < 10:
        peer.marker = 18
    marker = peer.marker
    peer.send(*packets, peer.p.SetHealthPacket(health=marker))
    await until(lambda: bot.get_status().health == marker)


async def open_trade(bot, peer, *, second=False, staged=7, only_open=False, **trade_options):
    p = peer.p
    await deliver(bot, peer, actor_packet(p), registry_packet(p))
    bot.interact_actor(bot.get_observed_entities()[0].ref)
    trade = open_packet(p) if only_open else trade_packet(p, [recipe(second=second)], **trade_options)
    slots = {0: stack(p, 43, staged, 301)} if staged else {}
    if second:
        slots[1] = stack(p, 42, 5, 302)
    await deliver(bot, peer, trade, inventory_packet(p, slots, inventory_id=5, size=3))
    assert bot.get_trade_window() is not None
    return bot.get_trade_window().ref


@asynccontextmanager
async def scene(monkeypatch, version, *, callback=None, timeout_ms=5000):
    peers = Peers(monkeypatch)
    events = []

    def collect(bot, event):
        events.append(event)
        if callback:
            callback(bot, event)

    async with api.Runtime(collect, events=api.ALL_EVENTS) as runtime:
        bot = runtime.add_bot(
            api.BotOptions(server_url="http://peer", protocol=version, item_request_timeout_ms=timeout_ms)
        )
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        await deliver(
            bot,
            peer,
            registry_packet(peer.p),
            inventory_packet(peer.p, {9: stack(peer.p), 20: stack(peer.p, 44, 6, 701, data=b"unrelated item")}),
        )
        yield bot, peer, events


def requests(peer):
    return [request for packet in peer.received if packet.packet_id == 147 for request in packet.requests]


async def sent(peer, count=1):
    await until(lambda: len(requests(peer)) == count)
    return requests(peer)[-1]


def action(value):
    return value.value if isinstance(value, bp.Variant) else value


def side(value, which):
    return getattr(value, which if hasattr(value, which) else {"source": "src", "destination": "dst"}[which])


def wire_slot(value):
    return int(value.full_container_name.name), value.slot, value.net_id_variant


def request_events(events):
    return [event for event in events if isinstance(event, api.ItemRequestEvent)]


def slot_events(events):
    return [event for event in events if isinstance(event, api.InventorySlotEvent)]


def assert_unknown_touched(bot, *indices):
    assert all(bot.get_item(bot.player_slot(index)).state == api.SlotState.unknown for index in indices)
    assert not bot.capture_inventory(0).complete
    unrelated = bot.get_item(bot.player_slot(20))
    assert (unrelated.count, unrelated.stack_id, bytes(unrelated.item.user_data_buffer)) == (6, 701, b"unrelated item")
