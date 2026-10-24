import gc
import json
import weakref
from pathlib import Path

import pytest
from peer import Peers

import bedrock_bot as api


async def test_context_exit_preserves_body_exception_and_ends_bot_access(monkeypatch):
    peers = Peers(monkeypatch)
    error = ValueError("body failed")
    with pytest.raises(ValueError) as raised:
        async with api.Runtime() as runtime:
            bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
            await bot.wait_ready(2)
            raise error
    assert raised.value is error and peers.connections[0].closed
    with pytest.raises(RuntimeError):
        bot.get_status()


def test_failed_and_uninitialized_instances_are_collectable():
    for cls in (api.Runtime, api.Bot):
        instance = cls.__new__(cls)
        reference = weakref.ref(instance)
        del instance
        gc.collect()
        assert reference() is None
    for kwargs, error in (({"callback": object()}, TypeError), ({"events": 1 << 63}, ValueError)):
        with pytest.raises(error):
            api.Runtime(**kwargs)


def test_auth_independent_go_key_derivation_and_signed_limits():
    from cryptography.hazmat.primitives.asymmetric import ec

    from bedrock_bot.auth import Identity, derive_session_key

    vectors = json.loads((Path(__file__).parent / "fixtures/wire_vectors.json").read_text())
    identity = Identity("test", "00000000-0000-0000-0000-000000000000", ec.derive_private_key(1, ec.SECP384R1()))
    assert derive_session_key(identity, vectors["handshake"]).hex() == vectors["session_key"]
    assert derive_session_key(identity, vectors["empty_salt_handshake"]).hex() == vectors["empty_salt_session_key"]
    for case in vectors["depth_cases"]:
        if case["depth"] == 16:
            assert derive_session_key(identity, case["jwt"]).hex() == vectors["session_key"]
        else:
            with pytest.raises(ValueError):
                derive_session_key(identity, case["jwt"])
    for case in vectors["quoted_depth_cases"]:
        assert derive_session_key(identity, case["jwt"]).hex() == vectors["session_key"]
    for case in vectors["salt_padding_cases"]:
        for variant in ("padded", "unpadded"):
            assert derive_session_key(identity, case[variant]).hex() == case["session_key"]
    for name in ("wrong_alg", "wrong_curve", "oversize_salt", "bad_spki"):
        with pytest.raises(ValueError):
            derive_session_key(identity, vectors[name])
    for jwt in ("a" * 65537, "a.b.c.d", "a.b.c", ""):
        with pytest.raises(ValueError):
            derive_session_key(identity, jwt)


def test_item_unknown_and_empty_are_distinct():
    import bedrock_protocol as bp
    from peer import empty_item

    token = api.BotToken()
    slot = api.InventorySlot(token, 1, api.InventoryArea.player, 0)
    unknown = api.SlotObservation(slot)
    assert unknown.count is None and unknown.network_id is None and unknown.stack_id is None
    for p in (bp.v1001, bp.v2193):
        empty = api.SlotObservation(slot, empty_item(p))
        assert empty.state == api.SlotState.empty
        assert (empty.count, empty.network_id, empty.stack_id) == (0, 0, 0)


async def test_explicit_close_inside_context_is_idempotent(monkeypatch):
    Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        await runtime.aclose()
        await runtime.aclose()
        with pytest.raises(RuntimeError):
            bot.get_status()


async def test_iterable_ranges_and_owner_are_checked_before_queue_admission(monkeypatch):
    Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        for method, args in (
            (bot.report_blob_status, ((v for v in [1, 2]),)),
            (bot.request_sub_chunks, (0, 0, 0, (v for v in [-1, 0, 1]))),
        ):
            with pytest.raises(RuntimeError):
                method(*args)
        for value in (-1, 1 << 64, "1", 1.5):
            with pytest.raises(TypeError):
                bot.report_blob_status(v for v in [value])
        for value in (-129, 128, "1", 1.5):
            with pytest.raises(TypeError):
                bot.request_sub_chunks(0, 0, 0, (v for v in [value]))


async def test_option_capture_is_immutable_and_independent(monkeypatch):
    Peers(monkeypatch)
    options = api.BotOptions(server_url="http://peer", name="original")
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(options)
        options.name = "mutated"
        captured = bot.capture_options()
        assert captured.name == "original"
        with pytest.raises(AttributeError):
            captured.name = "edited"
    assert captured.name == "original"
