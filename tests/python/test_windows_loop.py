import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows event loop contract")
def test_proactor_refusal_leaves_runtime_reusable_on_selector():
    code = """
import asyncio
from endpilot import Runtime

runtime = Runtime()

async def reject_proactor():
    before = asyncio.all_tasks()
    try:
        await runtime.__aenter__()
    except RuntimeError as error:
        assert "SelectorEventLoop" in str(error)
        assert "loop_factory" in str(error)
    else:
        await runtime.aclose()
        raise AssertionError("Proactor Runtime entry was accepted")
    assert asyncio.all_tasks() == before

async def reuse_on_selector():
    async with runtime:
        await runtime.aclose()

asyncio.run(reject_proactor())
asyncio.run(reuse_on_selector(), loop_factory=asyncio.SelectorEventLoop)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], check=False, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr
