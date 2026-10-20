import dataclasses

import pytest
from peer import Peers, actor_packet, slot_packet, until

import bedrock_bot as api


def attribute_data(p, name, minimum, current, maximum):
    return p.AttributeData(
        min_value=minimum,
        max_value=maximum,
        current_value=current,
        default_min_value=minimum,
        default_max_value=maximum,
        default_value=maximum,
        name=name,
        modifiers=[],
    )


def attribute_values(actor):
    return [(value.name, value.min_value, value.current_value, value.max_value) for value in actor.attributes]


@pytest.mark.parametrize("version", [1001, 2193])
@pytest.mark.parametrize("subscribed", [False, True])
async def test_remote_attributes_merge_without_mutating_retained_snapshots(monkeypatch, version, subscribed):
    peers = Peers(monkeypatch)
    events = []

    def callback(bot, event):
        events.append(event)
        if event.kind == api.ActorEvent.Kind.metadata:
            assert bot.capture_actor(event.actor.runtime_id) is event.actor

    mask = api.event_mask(api.EventKind.actor) if subscribed else 0
    async with api.Runtime(callback, events=mask) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        p = peer.p
        actor = actor_packet(p)
        peer.send(
            p.AddActorPacket(
                entity_id=actor.entity_id,
                runtime_id=actor.runtime_id,
                actor_type=actor.actor_type,
                pos=actor.pos,
                velocity=actor.velocity,
                rot=actor.rot,
                y_head_rotation=actor.y_head_rotation,
                y_body_rotation=actor.y_body_rotation,
                attributes=[
                    p.SyncedAttribute(name="minecraft:health", min_value=0, current_value=20, max_value=40),
                    p.SyncedAttribute(name="minecraft:movement", min_value=0, current_value=1, max_value=10),
                    p.SyncedAttribute(name="minecraft:attack_damage", min_value=0, current_value=3, max_value=20),
                ],
                data=actor.data,
                synched_properties=actor.synched_properties,
                links=actor.links,
            ),
            p.SetHealthPacket(health=18),
        )
        await until(lambda: bot.get_status().health == 18)
        original = bot.capture_actor(91)
        events.clear()

        peer.send(
            p.UpdateAttributesPacket(
                runtime_id=91,
                attribute_data=[
                    attribute_data(p, "minecraft:movement", 0, 2, 10),
                    attribute_data(p, "minecraft:health", 0, 17, 40),
                    attribute_data(p, "minecraft:armor", 0, 4, 20),
                    attribute_data(p, "minecraft:armor", 1, 6, 30),
                    attribute_data(p, "minecraft:health", -1, 11, 40),
                ],
                tick=1,
            ),
            slot_packet(p),
        )
        await until(lambda: bot.capture_inventory(0) is not None)
        updated = bot.capture_actor(91)
        assert attribute_values(updated) == [
            ("minecraft:health", -1, 11, 40),
            ("minecraft:movement", 0, 2, 10),
            ("minecraft:attack_damage", 0, 3, 20),
            ("minecraft:armor", 1, 6, 30),
        ]
        assert all(isinstance(value, p.SyncedAttribute) for value in updated.attributes)
        assert updated is not original
        for field in dataclasses.fields(original):
            if field.name != "attributes":
                assert getattr(updated, field.name) == getattr(original, field.name)
        assert bot.get_status().health == 18
        assert events == ([api.ActorEvent(api.ActorEvent.Kind.metadata, updated)] if subscribed else [])

        peer.send(
            p.UpdateAttributesPacket(
                runtime_id=91, attribute_data=[attribute_data(p, "minecraft:health", 0, 9, 40)], tick=2
            ),
            slot_packet(p, 1),
        )
        await until(lambda: bot.get_item(bot.player_slot(1)).state == api.SlotState.empty)
        latest = bot.capture_actor(91)
        assert attribute_values(latest)[0] == ("minecraft:health", 0, 9, 40)
        assert len(events) == (2 if subscribed else 0)
        await bot.disconnect()
        assert attribute_values(original) == [
            ("minecraft:health", 0, 20, 40),
            ("minecraft:movement", 0, 1, 10),
            ("minecraft:attack_damage", 0, 3, 20),
        ]
        assert attribute_values(updated)[0] == ("minecraft:health", -1, 11, 40)
        assert attribute_values(latest)[0] == ("minecraft:health", 0, 9, 40)
        assert original.metadata.data is not None
        assert updated.properties.int_entries is not None
        assert len(latest.links) == 0


@pytest.mark.parametrize("version", [1001, 2193])
async def test_unknown_and_empty_remote_attributes_leave_player_health_updates_intact(monkeypatch, version):
    peers = Peers(monkeypatch)
    events = []
    mask = api.event_mask(api.EventKind.actor) | api.event_mask(api.EventKind.health)
    async with api.Runtime(lambda bot, event: events.append(event), events=mask) as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        p = peer.p
        peer.send(actor_packet(p), p.SetHealthPacket(health=18))
        await until(lambda: bot.get_status().health == 18)
        original = bot.capture_actor(91)
        events.clear()
        peer.send(
            p.UpdateAttributesPacket(
                runtime_id=999, attribute_data=[attribute_data(p, "minecraft:health", 0, 2, 20)], tick=1
            ),
            p.UpdateAttributesPacket(runtime_id=91, attribute_data=[], tick=2),
            p.UpdateAttributesPacket(
                runtime_id=bot.get_status().actor_runtime_id,
                attribute_data=[
                    attribute_data(p, "minecraft:movement", 0, 3, 10),
                    attribute_data(p, "minecraft:health", 0, 13, 20),
                    attribute_data(p, "minecraft:health", 0, 12, 20),
                ],
                tick=3,
            ),
            slot_packet(p),
        )
        await until(lambda: bot.capture_inventory(0) is not None)
        assert bot.capture_actor(91) is original
        assert bot.capture_actor(999) is None
        assert len(bot.get_observed_entities()) == 1
        assert bot.get_status().health == 12
        assert events == [api.HealthEvent(13), api.HealthEvent(12)]
