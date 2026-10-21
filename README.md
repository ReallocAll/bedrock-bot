# bedrock-bot

An asyncio Bedrock client with Python-owned session and inventory state, immutable
native protocol views, and NetherNet transport. The supported business protocols
are 1001 and 2193.

Install the matching `bedrock-protocol==0.5.0.dev0` wheel, then install this package
with `python -m pip install .`. Dependencies select the approved temporary
`ReallocAll/nethernet-python` fork at commit
`1fd9b9907e602c3b906ad45abe856ea8e9358861` from `fix/jwk-server-identity`,
with aiortc 1.15.0 and aioice 0.10.2. The JWK change is under review in
[upstream PR #1](https://github.com/EndstoneMC/nethernet-python/pull/1).
NetherNet installs from that immutable Git commit; the PyPI package with that
name is unrelated.

The selected fork supports public EC JWK and DER-string `cpk` values while
retaining JWT signatures, SDP fingerprints, and mandatory server pinning. The
previous official `695095c` attempt failed at its server identity format check;
that failure remains archived. Fresh delivery and live business validation of
the selected `1fd9b99` graph are in progress.

The historical
[JWK candidate `19176ef`](https://github.com/ReallocAll/nethernet-python/commit/19176ef)
passed actual BDS JWT signature verification and SDP fingerprint
checks; one attempt observed `LOGIN_SUCCESS` under an explicit candidate-wheel
override. Its later ICE path lost consent. Those results apply to that historical
candidate and do not establish Ready or trade acceptance for the current pin.

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
