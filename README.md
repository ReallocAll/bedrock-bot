# bedrock-bot

An asyncio Bedrock client with Python-owned session and inventory state, immutable
native protocol views, and NetherNet transport. The supported business protocols
are 1001 and 2193.

Install the matching `bedrock-protocol==0.5.0.dev0` wheel, then install this package
with `python -m pip install .`. Dependencies select official NetherNet commit
`695095c7d853e56fd60076bfe6db9fab6714e88b`, aiortc 1.15.0 and aioice 0.10.2.
NetherNet installs directly from its official Git repository; the PyPI package
with that name is unrelated.

This migration is a candidate. The pinned NetherNet verifier accepts a DER-string
`cpk` and rejects JWK objects before login. The historical
[JWK candidate `19176ef`](https://github.com/ReallocAll/nethernet-python/commit/19176ef)
passed actual BDS JWT signature verification and SDP fingerprint
checks; one attempt observed `LOGIN_SUCCESS`. The patch is not merged upstream, no upstream PR has
been created, and the dependency pin above is unchanged. These results used an
explicit candidate-wheel override.

Ready and trade remain unaccepted because a separate ICE path lost consent.
Further live validation requires a verified direct path. The migration remains
incomplete; mandatory identity verification and server pinning remain enabled.

```python
import asyncio
import sys
from bedrock_bot import BotOptions, Runtime

async def main():
    async with Runtime() as runtime:
        bot = runtime.add_bot(BotOptions(
            server_url="http://server.example:19132",
            server_key_sha256="YOUR_VERIFIED_64_HEX_DIGIT_SERVER_KEY_DIGEST",
            name="ExampleBot",
        ))
        await bot.wait_ready(60)
        status = bot.get_status()
        print(status.session_id, status.position)
        await bot.disconnect()

asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)
```

The endpoint is an HTTP signaling URL. Obtain and explicitly configure the server
identity key digest before connecting. All mutations and captures use the owning
asyncio loop; only `Runtime.request_stop()` is thread-safe. Ready does not imply
that every inventory slot has been observed.

On Windows, enter Runtime on `asyncio.SelectorEventLoop`. The default Proactor
loop is currently unsupported because a CPython UDP cleanup bug can hang close.
The Selector loop supports at most 512 sockets and cannot use asyncio subprocess
pipes. Runtime preserves the caller's loop; use the explicit factory shown above.

Use `begin_move_item(source, destination, count)` and
`begin_trade(offer.ref, destination)` to retain an operation, then await
`operation.wait(timeout)`. The corresponding `request_*` methods return a request
key. Only one item request may be pending. A wait timeout or cancellation leaves
the operation tracked; explicit cancellation succeeds only while queued. Never
retry an indeterminate transaction.

Captures, events, operation results, nested collections, and native buffers are
immutable and remain valid across updates, reconnect, and Runtime destruction.
`copy()` detaches the selected subtree; `to_dict()` and collection `to_list()`
explicitly materialize it. `wire_bytes()` re-encodes the captured value.

With official NetherNet, `connected_bytes_sent` and `connected_bytes_received`
are `None`; application packet and batch counters remain available. Queue and
complete-message limits do not bound transport backlog or reassembly memory.

The [business API and measurement notes](docs/business-api.md) describe inventory
authority, statistics boundaries, and the direct asyncio examples. Run tests with
`python -m pytest tests`; real BDS gameplay requires its separate server harness.
