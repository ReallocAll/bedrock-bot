# bedrock-bot

An asyncio client for Minecraft Bedrock automation. It manages connections,
movement, world observations, inventories and trading over NetherNet. Gameplay
support covers protocols **1001** and **2193**.

## Quick start

Use Python 3.12 or newer with the matching native protocol runtime installed.
The server must provide a NetherNet HTTP signaling endpoint. Obtain its public
identity key digest through a trusted channel before connecting.

Save this as `connect.py`, then pass the endpoint and its verified SHA-256 digest
as arguments:

```python
import asyncio
import sys

from bedrock_bot import BotOptions, Runtime


async def main():
    async with Runtime() as runtime:
        bot = runtime.add_bot(BotOptions(
            server_url=sys.argv[1],
            server_key_sha256=sys.argv[2],
            name="ExampleBot",
        ))
        await bot.wait_ready(60)
        print(bot.get_status().position)
        await bot.disconnect()


asyncio.run(
    main(),
    loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None,
)
```

```shell
python connect.py http://server.example:19672 "VERIFIED_64_HEX_DIGIT_SHA256"
```

The digest is required: the client verifies the signed server identity, SDP
fingerprint and configured key. NetherNet provides DTLS encryption. RakNet and
Xbox account authentication are not supported; an offline XUID is only a declared
identity field.

## Installation

The SDK depends on `bedrock-protocol-native` and the NetherNet implementation
specified in [pyproject.toml](pyproject.toml). Use native wheels compatible with
your Python interpreter and platform. See the protocol project's
[Python guide](https://github.com/EndstoneMC/bedrock-protocol/blob/main/docs/python.md)
for its compiler and native runtime packages.

From this checkout or an extracted SDK source distribution, install into a virtual
environment. Point `--find-links` at the matching compiler and native wheels:

```shell
python -m pip install --find-links ../bedrock-protocol/dist .
```

Replace that wheel-directory path for your layout. Other dependencies are resolved
from their declared sources; the NetherNet Git dependency requires network access.

## Using the API

- **Loop ownership:** enter and use `Runtime` on one asyncio loop. Only
  `Runtime.request_stop()` is thread-safe. Context exit awaits connection cleanup.
- **Windows:** use `asyncio.SelectorEventLoop` as shown above. Proactor UDP cleanup
  is unsupported; Selector has a 512-socket limit and no asyncio subprocess pipes.
- **Observations:** gameplay captures, events and results are immutable. Retained values
  remain valid after updates, reconnect or runtime destruction. `copy()` detaches
  a subtree; `to_dict()` and `to_list()` materialize it.
- **Inventory:** readiness does not mean every slot is known. Operations use
  observed items and current references. A timed-out or cancelled waiter does not
  cancel a submitted transaction; never retry an indeterminate result.
- **Transport statistics:** application packet and batch counters are available;
  NetherNet connected-byte counters are unknown (`None`). Application limits do
  not bound transport backlog or reassembly memory.

The [API guide](docs/api.md) covers observations, operations, events,
statistics and error handling. The runnable examples demonstrate an
[observed trade](examples/first_trade.py) and a
[trade/restock cycle](examples/villager_cycle.py); both require a prepared server
and explicit target identifiers.

## Contributing

Changes to the Python API should preserve loop ownership, immutable captures and
transaction outcomes. See the [API guide](docs/api.md) for these contracts.

Build the wheel and source distribution from the repository root:

```shell
python -m pip install build
python -m build --outdir dist
```

In an isolated test environment, install the built SDK wheel with test dependencies
and access to the compatible protocol wheels. The filenames below follow the
current package version; use the filenames produced by your build:

```shell
python -m pip install --find-links ../bedrock-protocol/dist "dist/bedrock_bot-0.2.0.dev0-py3-none-any.whl[test]"
mkdir ../bedrock-bot-test-source
tar -xf dist/bedrock_bot-0.2.0.dev0.tar.gz -C ../bedrock-bot-test-source
cd ../bedrock-bot-test-source/bedrock_bot-0.2.0.dev0
python -I -m pytest tests/python tests/integration/nethernet/test_transport.py
```

Choose a new extraction directory outside the checkout. Run pytest with the
interpreter where the wheels are installed; isolated mode prevents importing the
SDK from the checkout. These tests include controlled native transport checks;
real-server gameplay needs a separately prepared server.

## License

[Apache-2.0](LICENSE).
