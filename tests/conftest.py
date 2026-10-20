import asyncio
import sys


def pytest_asyncio_loop_factories(config, item):
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return None
