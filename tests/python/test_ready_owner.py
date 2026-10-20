import asyncio
import sys

import pytest
from peer import Peer

import bedrock_bot as api
from bedrock_bot import transport


@pytest.mark.parametrize("pending_failure", [False, True])
async def test_ready_coroutine_rejects_foreign_consuming_loop(monkeypatch, pending_failure):
    dial = asyncio.Event()

    async def open_connection(options, identity):
        await dial.wait()
        return Peer(options.protocol).connection

    monkeypatch.setattr(transport, "_open_connection", open_connection)
    async with api.Runtime() as runtime:
        bot = runtime.add_bot(api.BotOptions(server_url="http://peer"))
        ready = bot._ready
        waiter = bot.wait_ready(timeout=0.05)
        failure = LookupError("owner-only delivery")
        if pending_failure:
            runtime._background_failure(failure)

        def consume():
            try:
                asyncio.run(waiter, loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)
            except BaseException as error:
                return error
            return None

        error = await asyncio.wait_for(asyncio.to_thread(consume), 2)
        assert isinstance(error, RuntimeError) and "owner loop" in str(error)
        assert bot._ready is ready and not ready.cancelled()
        if pending_failure:
            assert runtime._failure[0] is failure
            with pytest.raises(LookupError) as caught:
                await runtime.wait_closed()
            assert caught.value is failure
            await runtime.wait_closed()
        else:
            assert not ready.done()
            dial.set()
            await bot.wait_ready(2)
            assert bot.get_status().state == api.BotState.ready
