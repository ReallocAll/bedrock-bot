import gc
from dataclasses import FrozenInstanceError, dataclass

import bedrock_protocol as bp
import pytest
from peer import Peers, actor_packet, motion_packet, until

import endpilot as api
from endpilot._projection import project


def test_exact_public_vector_keeps_identity_and_immutability():
    vector = api.Vec3(1.5, -2.5, 3.5)
    actor = api.ActorSnapshot(91, -92, "minecraft:villager_v2", vector, vector)
    assert project(vector) is vector
    assert actor.position is actor.velocity is vector
    with pytest.raises(FrozenInstanceError):
        vector.x = 9
    with pytest.raises(FrozenInstanceError):
        del vector.y
    assert vector.to_dict() == {"x": 1.5, "y": -2.5, "z": 3.5}


def test_public_vector_subclass_preserves_its_fields_and_copy():
    @dataclass(frozen=True, slots=True)
    class TaggedVector(api.Vec3):
        label: str = "tag"

    vector = TaggedVector(1, 2, 3)
    assert project(vector) is vector
    copied = vector.copy()
    assert type(copied) is TaggedVector and copied is not vector
    assert copied.to_dict() == {"x": 1, "y": 2, "z": 3, "label": "tag"}
    with pytest.raises(FrozenInstanceError):
        copied.label = "changed"


@pytest.mark.parametrize("version", [1001, 2193])
def test_native_vectors_convert_to_independent_public_vectors(version):
    p = getattr(bp, f"v{version}")
    native = p.Vec3(x=1.5, y=-2.5, z=3.5)
    projected = project(native)
    assert type(projected) is api.Vec3 and projected is not native
    assert project(projected) is projected
    assert projected.to_dict() == {"x": 1.5, "y": -2.5, "z": 3.5}
    with pytest.raises(FrozenInstanceError):
        projected.z = 0
    del native
    gc.collect()
    assert projected == api.Vec3(1.5, -2.5, 3.5)


@pytest.mark.parametrize("version", [1001, 2193])
def test_nested_collections_preserve_public_identity_and_native_conversion(version):
    vector = api.Vec3(1, 2, 3)
    p = getattr(bp, f"v{version}")
    projected = project({"vectors": (vector, p.Vec3(x=4, y=5, z=6)), "other": [None, 7, "value"]})
    assert project(projected) is projected
    vectors = projected["vectors"]
    assert project(vectors) is vectors
    assert projected["vectors"][0] is vector
    assert type(projected["vectors"][1]) is api.Vec3
    assert projected.to_dict() == {
        "vectors": [{"x": 1, "y": 2, "z": 3}, {"x": 4, "y": 5, "z": 6}],
        "other": [None, 7, "value"],
    }
    with pytest.raises(TypeError):
        projected["vectors"] = []
    with pytest.raises(TypeError):
        projected["vectors"][0] = api.Vec3()


@pytest.mark.parametrize("version", [1001, 2193])
async def test_captured_vectors_survive_actor_replacement_and_reconnect(monkeypatch, version):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=version))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.send(actor_packet(peer.p))
        await until(lambda bot=bot: bot.capture_actor(91) is not None)
        original = bot.capture_actor(91)
        peer.send(motion_packet(peer.p, x=7))
        await until(lambda bot=bot: bot.capture_actor(91).position.x == 7)
        updated = bot.capture_actor(91)
        assert updated is not original
        assert updated.velocity is original.velocity
        assert updated.position == api.Vec3(7, 8, 9)
        await bot.reconnect()
        await bot.wait_ready(2)
        assert bot.capture_actor(91) is None
    del bot, peer, peers, runtime
    gc.collect()
    assert original.position == api.Vec3(1, 2, 3)
    assert original.velocity == updated.velocity == api.Vec3(0, 5, 0)
    assert updated.position == api.Vec3(7, 8, 9)
    with pytest.raises(FrozenInstanceError):
        original.position.x = 0
