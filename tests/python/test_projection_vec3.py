import gc
import subprocess
import sys
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path

import bedrock_protocol as bp
import pytest
from peer import Peers, actor_packet, motion_packet, until

import bedrock_bot as api
from bedrock_bot._projection import ReadonlyMap, ReadonlySequence, Record, project


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


def test_other_record_still_projects_and_detaches_nested_values():
    @dataclass(frozen=True, slots=True)
    class NestedRecord(Record):
        values: object

    native = bp.v1001.Vec3(x=4, y=5, z=6)
    record = NestedRecord({"vectors": [native]})
    assert project(record) is record
    assert isinstance(record.values, ReadonlyMap)
    assert isinstance(record.values["vectors"], ReadonlySequence)
    assert type(record.values["vectors"][0]) is api.Vec3
    copied = record.copy()
    assert copied is not record and copied.values is not record.values
    assert copied.to_dict() == {"values": {"vectors": [{"x": 4, "y": 5, "z": 6}]}}


def test_distinct_record_layouts_and_subclasses_freeze_runtime_values_independently():
    def layout(field_name):
        return dataclass(frozen=True, slots=True)(type(
            "SameName", (Record,), {"__module__": __name__, "__annotations__": {field_name: int}}
        ))

    SequenceRecord = layout("values")
    MappingRecord = layout("mapping")

    @dataclass(frozen=True, slots=True)
    class ChildRecord(SequenceRecord):
        status: object

    values = [bp.v1001.Vec3(x=1, y=2, z=3)]
    mapping = {"count": 7}
    sequence = SequenceRecord(values)
    mapped = MappingRecord(mapping)
    child = ChildRecord(values, bp.v2193.PlayStatus.PLAYER_SPAWN)
    # Projection follows actual values, even when their annotations are scalar.
    assert isinstance(sequence.values, ReadonlySequence)
    assert isinstance(mapped.mapping, ReadonlyMap)
    assert type(child.status) is int
    values[:] = [bp.v1001.Vec3(x=4, y=5, z=6), None]
    mapping["count"] = 9
    copied = child.copy()
    assert copied is not child and copied.values is not child.values
    expected = {"values": [{"x": 1, "y": 2, "z": 3}], "status": int(bp.v2193.PlayStatus.PLAYER_SPAWN)}
    assert child.to_dict() == copied.to_dict() == expected
    assert sequence.to_dict() == {"values": expected["values"]}
    assert mapped.to_dict() == mapped.copy().to_dict() == {"mapping": {"count": 7}}
    assert SequenceRecord([None]).to_dict() == {"values": [None]}
    assert MappingRecord({"other": 8}).to_dict() == {"mapping": {"other": 8}}


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


@pytest.mark.parametrize("first", ["bedrock_bot._projection", "bedrock_bot.types", "bedrock_bot.observations"])
def test_fresh_import_projects_vectors_without_partial_initialization(first):
    code = """
import importlib
import sys
sys.path.insert(0, sys.argv[1])
importlib.import_module(sys.argv[2])
from bedrock_bot._projection import project
from bedrock_bot.observations import ActorSnapshot
from bedrock_bot.types import Vec3
import bedrock_protocol as bp
vector = Vec3(1, 2, 3)
assert project(vector) is vector
assert ActorSnapshot(91, -92, 'actor', vector).position is vector
assert project(bp.v2193.Vec3(x=4, y=5, z=6)) == Vec3(4, 5, 6)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(Path(api.__file__).resolve().parent.parent), first],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
