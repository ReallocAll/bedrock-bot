# bedrock-bot

An asyncio Bedrock client with Python-owned session and inventory state, immutable
native protocol views, and NetherNet transport. The supported business protocols
are 1001 and 2193.

```python
import asyncio
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

asyncio.run(main())
```

The endpoint is an HTTP signaling URL. Obtain and explicitly configure the server
identity key digest before connecting. All mutations and captures use the owning
asyncio loop; only `Runtime.request_stop()` is thread-safe. Ready does not imply
that every inventory slot has been observed.

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

The [business API and measurement notes](docs/business-api.md) describe inventory
authority, statistics boundaries, and the direct asyncio examples. Run tests with
`python -m pytest tests`; real BDS gameplay requires its separate server harness.
