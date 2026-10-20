import asyncio
from types import SimpleNamespace

import pytest
from peer import Peers, submit, until

import bedrock_bot as api
from bedrock_bot import transport


@pytest.mark.parametrize("protocol", [1001, 2193])
@pytest.mark.parametrize("stage", ["admission", "batch"])
async def test_encoding_failure_invalidates_session(monkeypatch, protocol, stage):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer", protocol=protocol))
        await bot.wait_ready(2)
        session = bot._session
        peer = peers.connections[-1]
        before = bot.capture_statistics()
        error = ValueError("controlled encoding failure")

        def fail(*args):
            raise error

        if stage == "admission":
            session.codec = SimpleNamespace(encode_packet=fail)
            with pytest.raises(ValueError) as caught:
                bot.request_chunk_radius(4)
            assert caught.value is error
            assert not session.usable()
        else:
            session.batch = SimpleNamespace(encode=fail, decode=session.batch.decode)
            bot.request_chunk_radius(4)
        await until(lambda: peer.closed)
        assert bot.get_status().state == api.BotState.failed
        assert bot.capture_statistics().packets_out == before.packets_out
        assert session.queue_packets == session.queue_bytes == 0


@pytest.mark.parametrize("failure", [OSError("send failed"), asyncio.CancelledError()])
async def test_send_failure_is_indeterminate_and_keeps_batch_accounted(monkeypatch, failure):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        before = bot.capture_statistics()
        operation = submit(bot)

        def at_send():
            assert operation.phase == api.ItemOperationPhase.submitted
            assert bot._session.queue_packets >= 1
            assert bot._session.queue_bytes > 0

        peer.before_send = at_send
        peer.send_error = failure
        result = await operation.wait(2)
        await until(lambda: peer.closed)
        if isinstance(failure, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await runtime.wait_closed()
        assert result.outcome == api.ItemRequestOutcome.indeterminate
        assert bot.get_status().state == api.BotState.failed
        assert not any(packet.packet_id == 147 for packet in peer.received)
        assert bot.capture_statistics().packets_out == before.packets_out
        assert bot._session.queue_packets == bot._session.queue_bytes == 0


@pytest.mark.parametrize("direction", ["send", "receive"])
async def test_complete_wire_message_limit_invalidates_session(monkeypatch, direction):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        monkeypatch.setattr(transport, "MAX_WIRE_MESSAGE", 10)
        if direction == "send":
            bot._session.batch = SimpleNamespace(encode=lambda packets: b"x" * 11, decode=bot._session.batch.decode)
            bot.request_chunk_radius(4)
        else:
            peer.incoming.put_nowait(b"x" * 11)
        await until(lambda: peer.closed)
        assert bot.get_status().state == api.BotState.failed
        assert "wire message limit" in bot._session.reason


async def test_disconnect_resolves_pending_batch_boundary(monkeypatch):
    peers = Peers(monkeypatch)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        await bot.wait_ready(2)
        peer = peers.connections[-1]
        peer.pause_sender(bot)
        boundary = asyncio.create_task(bot._session.flush())
        await asyncio.sleep(0)
        await bot.disconnect()
        assert boundary.done()
        with pytest.raises(asyncio.CancelledError):
            await boundary
