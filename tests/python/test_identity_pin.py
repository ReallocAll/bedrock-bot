import asyncio
import base64
import hmac
import json
import struct
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from peer import Peer, Peers

import bedrock_bot as api
from bedrock_bot import auth, session, transport
from bedrock_bot.auth import build_login, create_identity

UUID = "00000000-0000-0000-0000-000000000001"


def envelope(raw):
    size = struct.unpack_from("<I", raw)[0]
    return json.loads(raw[4 : 4 + size])


def unbase(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_identity_token_is_reused_and_independently_verifiable():
    identity = create_identity("offline test", UUID)
    token = identity.token
    options = api.BotOptions(server_url="http://peer")
    first, second = envelope(build_login(identity, options)), envelope(build_login(identity, options))
    same_token = hmac.compare_digest(first["Token"], token) and hmac.compare_digest(second["Token"], token)
    assert same_token
    assert first["AuthenticationType"] == second["AuthenticationType"] == 2
    assert first["Certificate"] == '{"chain":[""]}'
    assert len(token.encode("ascii")) <= 16384
    header, payload, signature = token.split(".")
    header_data, claims = json.loads(unbase(header)), json.loads(unbase(payload))
    assert header_data["alg"] == "ES384"
    public = identity.key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert base64.b64decode(header_data["x5u"]) == base64.b64decode(claims["cpk"]) == public
    raw_signature = unbase(signature)
    assert len(raw_signature) == 96
    der = utils.encode_dss_signature(
        int.from_bytes(raw_signature[:48], "big"), int.from_bytes(raw_signature[48:], "big")
    )
    identity.key.public_key().verify(der, (header + "." + payload).encode("ascii"), ec.ECDSA(hashes.SHA384()))
    assert claims["xid"] == claims["ipt"] == claims["mid"] == claims["tid"] == ""
    assert claims["leguuid"] == UUID and claims["xname"] == "offline test"
    assert claims["aud"] == ["api://auth-minecraft-services/multiplayer"]
    assert claims["exp"] - claims["nbf"] == 43200
    other = create_identity("offline test", UUID)
    different_token = not hmac.compare_digest(other.token, token)
    assert different_token
    assert other.key.public_key().public_numbers() != identity.key.public_key().public_numbers()


def test_identity_repr_omits_key_and_token_and_token_size_is_bounded():
    identity = create_identity("offline test", UUID)
    safe_repr = "key=" not in repr(identity) and "token=" not in repr(identity)
    assert safe_repr
    with pytest.raises(ValueError, match="16384"):
        create_identity("x" * 13000, UUID)


@pytest.mark.parametrize("pin", [None, 123, b"a" * 64, "a" * 63, "a" * 65, "g" * 64, " " + "a" * 63])
async def test_invalid_server_pin_is_rejected_before_session_creation(pin):
    async with api.Runtime() as runtime:
        with pytest.raises(ValueError, match="server_key_sha256"):
            runtime.add_bot(api.BotOptions(server_url="http://peer", server_key_sha256=pin))


@pytest.mark.parametrize("pin", ["", "0123456789abcdef" * 4, "ABCDEF0123456789" * 4])
async def test_server_pin_capture_is_normalized_immutable_and_independent(monkeypatch, pin):
    Peers(monkeypatch)
    options = api.BotOptions(server_url="http://peer", server_key_sha256=pin)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(options)
        captured = bot.capture_options()
        assert captured.server_key_sha256 == pin.lower()
        options.server_key_sha256 = "f" * 64
        assert captured.server_key_sha256 == pin.lower()
        with pytest.raises(AttributeError):
            captured.server_key_sha256 = "0" * 64
        assert not hasattr(captured, "key") and not hasattr(captured, "token")
        await bot.wait_ready(2)
    assert captured.server_key_sha256 == pin.lower()


async def test_session_opener_identity_is_the_login_identity_and_refreshes_on_reconnect(monkeypatch):
    identities, peers = [], []

    async def open_connection(options, identity):
        identities.append(identity)
        peer = Peer(options.protocol)
        peers.append(peer)
        return peer.connection

    monkeypatch.setattr(transport, "_open_connection", open_connection)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", uuid=UUID))
        for index in range(2):
            if index:
                await bot.reconnect()
            await bot.wait_ready(2)
            assert bot._session.connection is peers[index].connection
            packet = next(packet for packet in peers[index].received if packet.packet_id == 1)
            same_token = hmac.compare_digest(
                envelope(packet.connection_request.to_bytes())["Token"], identities[index].token
            )
            assert same_token
            assert identities[index].uuid == UUID
    different_token = not hmac.compare_digest(identities[0].token, identities[1].token)
    assert different_token
    assert identities[0].key.public_key().public_numbers() != identities[1].key.public_key().public_numbers()


@pytest.mark.parametrize("cancel_before_start", [False, True], ids=["delayed-start", "cancel-before-start"])
async def test_identity_is_created_after_long_start_delay(monkeypatch, cancel_before_start):
    delay = 7 * 3600
    now = 2_000_000_000
    initial_time = now
    entered, release = asyncio.Event(), asyncio.Event()
    creation_times, identities, peers = [], [], []
    original_sleep = asyncio.sleep

    async def controlled_sleep(seconds):
        nonlocal now
        if seconds == delay:
            entered.set()
            await release.wait()
            now += delay
        else:
            await original_sleep(seconds)

    def timed_identity(name, uuid, **kwargs):
        creation_times.append(now)
        return create_identity(name, uuid, **kwargs)

    async def open_connection(options, identity):
        identities.append(identity)
        peer = Peer(options.protocol)
        peers.append(peer)
        return peer.connection

    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(session, "create_identity", timed_identity)
    monkeypatch.setattr(asyncio, "sleep", controlled_sleep)
    monkeypatch.setattr(transport, "_open_connection", open_connection)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", uuid=UUID, start_delay_ms=delay * 1000))
        await asyncio.wait_for(entered.wait(), 2)
        if cancel_before_start:
            await bot.disconnect()
            assert bot.get_status().state == api.BotState.closed
            assert not peers and not creation_times
        else:
            release.set()
            await bot.wait_ready(2)
            claims = json.loads(unbase(identities[0].token.split(".")[1]))
            assert claims["nbf"] == initial_time + delay - 21600
            assert claims["exp"] == initial_time + delay + 21600
            assert creation_times == [initial_time + delay]
            packet = next(packet for packet in peers[0].received if packet.packet_id == 1)
            same_token = hmac.compare_digest(
                envelope(packet.connection_request.to_bytes())["Token"], identities[0].token
            )
            assert same_token
