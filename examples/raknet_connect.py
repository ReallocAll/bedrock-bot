"""Connect to a Bedrock server over RakNet and print the spawned position."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys

from endpilot import BotOptions, Runtime


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", required=True, help="RakNet endpoint, for example raknet://127.0.0.1:19132")
    parser.add_argument(
        "--server-key-sha256",
        default="",
        help="optional SHA-256 pin of the server's DER SubjectPublicKeyInfo game-handshake key",
    )
    args = parser.parse_args()
    if args.server_key_sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", args.server_key_sha256):
        parser.error("--server-key-sha256 must be 64 hexadecimal characters")
    return args


async def connect(server_url: str, server_key_sha256: str) -> None:
    async with Runtime() as runtime:
        bot = runtime.add_bot(
            BotOptions(
                server_url=server_url,
                server_key_sha256=server_key_sha256,
                transport="raknet",
                protocol=2193,
                name="RakNetBot",
            )
        )
        await bot.wait_ready(60)
        print(bot.get_status().position)
        await bot.disconnect()


def main() -> None:
    args = parse_args()
    asyncio.run(
        connect(args.server_url, args.server_key_sha256),
        loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None,
    )


if __name__ == "__main__":
    main()
