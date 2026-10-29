import subprocess
import sys


def test_runtime_prepares_imports_before_first_dial():
    result = subprocess.run([sys.executable, "-I", "-B", "-c", r'''
import asyncio
import sys
from types import SimpleNamespace

import endpilot as api

required = ("endpilot.session", "endpilot.transport", "aiortc", "nethernet.identity")
assert all(name not in sys.modules for name in required)

async def main():
    async with api.Runtime() as runtime:
        assert all(name in sys.modules for name in required)
        from endpilot import transport
        attempted = asyncio.Event()
        closed = []

        async def send(data):
            pass

        async def recv():
            await asyncio.Event().wait()

        async def close():
            closed.append(True)

        async def open_connection(options, identity):
            assert identity.name == options.name
            attempted.set()
            return SimpleNamespace(send=send, recv=recv, close=close)

        transport._open_connection = open_connection
        bot = runtime.add_bot(api.BotOptions(server_url="http://controlled"))
        await attempted.wait()
        await bot.disconnect()
        assert closed == [True]

asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)
'''], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr
