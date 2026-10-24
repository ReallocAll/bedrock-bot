import subprocess
import sys

import pytest


def isolated(code):
    result = subprocess.run([sys.executable, "-I", "-B", "-c", code],
                            capture_output=True, text=True, check=False, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr


def test_runtime_entry_prepares_imports_before_first_dial_without_creating_resources():
    isolated(r'''
import asyncio
import sys
from types import SimpleNamespace

import bedrock_bot as api
import bedrock_protocol as bp

required = ("bedrock_bot.session", "bedrock_bot.transport", "aiortc", "nethernet", "nethernet.identity")
assert all(name not in sys.modules for name in required)
preparing = False

def audit(event, args):
    if preparing and event == "socket.__new__":
        raise AssertionError("Runtime entry created a socket")

sys.addaudithook(audit)

def profile(frame, event, arg):
    if event == "c_call" and getattr(arg, "__name__", "") == "generate_private_key":
        raise AssertionError("Runtime entry generated a private key")
    if event != "call":
        return
    module = frame.f_globals.get("__name__", "")
    name = frame.f_code.co_name
    owner = type(frame.f_locals.get("self")).__name__
    if ((module == "bedrock_bot.auth" and name in ("create_identity", "__post_init__"))
        or (module == "bedrock_bot.session" and name == "__init__" and owner == "Session")
        or (module.startswith(("aiortc", "nethernet")) and name == "__init__"
            and owner in ("RTCPeerConnection", "Connection", "IdentitySigner"))):
        raise AssertionError(f"Runtime entry constructed {module}.{owner}.{name}")

def unexpected_resource(*args, **kwargs):
    raise AssertionError("Runtime entry constructed a codec or timer")

def imports_ready():
    assert all(name in sys.modules for name in required)
    assert all(not getattr(sys.modules[name].__spec__, "_initializing", False) for name in required)

async def main():
    global preparing
    runtime = api.Runtime()
    loop = asyncio.get_running_loop()
    codec, call_at, call_later = bp.Codec, loop.call_at, loop.call_later
    bp.Codec = loop.call_at = loop.call_later = unexpected_resource
    preparing = True
    sys.setprofile(profile)
    try:
        assert await runtime.__aenter__() is runtime
    finally:
        preparing = False
        sys.setprofile(None)
        bp.Codec, loop.call_at, loop.call_later = codec, call_at, call_later
    imports_ready()
    from bedrock_bot import transport
    attempted = asyncio.Event()
    closed = []

    async def send(data):
        pass

    async def recv():
        await asyncio.Event().wait()

    async def close():
        closed.append(True)

    async def open_connection(options, identity):
        imports_ready()
        assert identity.name == options.name
        attempted.set()
        return transport._Connection(SimpleNamespace(send=send, recv=recv, close=close))

    transport._open_connection = open_connection
    try:
        bot = runtime.add_bot(api.BotOptions(server_url="http://controlled"))
        await attempted.wait()
        await bot.disconnect()
        assert closed == [True]
    finally:
        await runtime.__aexit__(None, None, None)

asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)
''')


@pytest.mark.parametrize("target", ["bedrock_bot.session", "nethernet.identity"])
def test_runtime_import_failure_propagates_before_partial_entry_and_can_be_retried(target):
    isolated("target = " + repr(target) + r'''
import asyncio
import importlib.abc
import sys

import bedrock_bot as api

assert target not in sys.modules
failure = ImportError("controlled dependency import failure")

class BlockImport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == globals()["target"]:
            raise failure

async def main():
    runtime = api.Runtime()
    before = asyncio.all_tasks()
    blocker = BlockImport()
    sys.meta_path.insert(0, blocker)
    try:
        try:
            await runtime.__aenter__()
        except ImportError as error:
            assert error is failure
        else:
            raise AssertionError("Runtime entry did not propagate the import failure")
        assert runtime._loop is None and not runtime._exited
        assert not hasattr(runtime, "_stop_event") and not hasattr(runtime, "_lifetime")
        assert asyncio.all_tasks() == before
    finally:
        sys.meta_path.remove(blocker)
        failure.__traceback__ = None
    async with runtime:
        assert runtime._loop is asyncio.get_running_loop()

asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)
''')
