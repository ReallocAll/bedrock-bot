"""One observed trade with a nonzero residual return; server preparation is external."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import bedrock_bot as bb


class Events:
    def __init__(self):
        self.rows = []
        self.changed = asyncio.Event()

    def __call__(self, bot, event):
        if not isinstance(event, (bb.TickEvent, bb.PositionEvent, bb.ActorMotionEvent)):
            self.rows.append((bot, event))
        self.changed.set()

    async def wait(self, bot, predicate, timeout, *, session_id=None):
        async with asyncio.timeout(timeout):
            while True:
                self.changed.clear()
                status = bot.get_status()
                if status.state in (bb.BotState.failed, bb.BotState.closed) or not status.alive:
                    raise RuntimeError("ready/alive context was lost")
                if session_id is not None and status.session_id != session_id:
                    raise RuntimeError("unexpected session change")
                result = predicate()
                if result:
                    return result
                await self.changed.wait()


def validate_parameters(target_unique_id, prepare_timeout, operation_timeout, max_distance, stage_extra=1):
    if type(target_unique_id) is not int or not -(1 << 63) <= target_unique_id < 1 << 63:
        raise ValueError("target unique ID must fit signed 64 bits")
    if not all(math.isfinite(value) and value > 0 for value in (prepare_timeout, operation_timeout, max_distance)):
        raise ValueError("timeouts and distance must be positive finite values")
    if type(stage_extra) is not int or not 1 <= stage_extra <= 63:
        raise ValueError("stage_extra must be in 1..63")
    if operation_timeout > 0xFFFFFFFF / 1000:
        raise ValueError("operation timeout exceeds unsigned 32-bit milliseconds")


def connection_options(args):
    parsed = urlsplit(args.server_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("server-url must be an HTTP signaling URL without credentials")
    if args.server_key_sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", args.server_key_sha256):
        raise ValueError("server-key-sha256 must be empty or a 64-digit hexadecimal pin")
    if not args.name.strip():
        raise ValueError("name must not be empty")
    if args.uuid and (len(args.uuid) != 36 or str(UUID(args.uuid)) != args.uuid.lower()):
        raise ValueError("invalid account UUID")
    return bb.BotOptions(
        server_url=args.server_url,
        server_key_sha256=args.server_key_sha256,
        protocol=args.protocol,
        name=args.name,
        uuid=args.uuid,
        item_request_timeout_ms=math.ceil(args.operation_timeout * 1000),
    )


def inventory_record(bot):
    slots = bot.get_player_inventory()
    if any(
        slot.state == bb.SlotState.unknown or slot.state == bb.SlotState.occupied and slot.identifier is None
        for slot in slots
    ):
        raise RuntimeError("complete known player inventory is required")
    return [
        {
            "index": slot.slot.index,
            "state": slot.state.name,
            "identifier": slot.identifier,
            "count": slot.count,
            "stack_id": slot.stack_id,
            "item": None if slot.item is None else slot.item.to_dict(),
        }
        for slot in slots
    ]


def totals(inventory):
    result = Counter()
    for item in inventory:
        if item["state"] == "unknown" or item["state"] == "occupied" and item["identifier"] is None:
            raise RuntimeError("conservation requires known identities")
        if item["state"] == "occupied":
            result[item["identifier"]] += item["count"]
    return +result


def check_conservation(before, after, inputs, output):
    expected = totals(before)
    for name, amount in inputs.items():
        expected[name] -= amount
    expected[output.identifier] += output.count
    if any(amount < 0 for amount in expected.values()) or totals(after) != +expected:
        raise RuntimeError("inventory conservation failed")
    involved = {*inputs, output.identifier, None}
    if [slot for slot in before if slot["identifier"] not in involved] != [
        slot for slot in after if slot["identifier"] not in involved
    ]:
        raise RuntimeError("unrelated inventory conservation failed")


def find_target(bot, unique_id, max_distance, *, trader=True):
    found = [entity for entity in bot.get_observed_entities() if entity.ref.unique_id == unique_id]
    if len(found) > 1:
        raise RuntimeError("target identity is ambiguous")
    if not found:
        return None
    target = found[0]
    if trader and target.type not in ("minecraft:villager", "minecraft:villager_v2"):
        raise RuntimeError("target is not a villager")
    status = bot.get_status()
    distance = math.dist(
        (status.position.x, status.position.y, status.position.z),
        (target.position.x, target.position.y, target.position.z),
    )
    return target if math.isfinite(distance) and distance <= max_distance else None


async def open_trade(bot, events, target, timeout):
    if bot.get_trade_window() is not None:
        raise RuntimeError("a trade window is already open")
    status = bot.get_status()
    dx, dy, dz = (
        target.position.x - status.position.x,
        target.position.y + 1 - status.position.y,
        target.position.z - status.position.z,
    )
    bot.set_look(-math.degrees(math.atan2(dy, math.hypot(dx, dz))), math.degrees(math.atan2(-dx, dz)))
    await events.wait(bot, lambda: bot.get_status().tick > status.tick, timeout, session_id=status.session_id)
    bot.interact_actor(target.ref, bb.Vec3(0, 1, 0))

    def matched():
        window = bot.get_trade_window()
        if window is None:
            return None
        if (
            window.closing
            or window.ref.trader_unique_id != target.ref.unique_id
            or window.ref.session_id != status.session_id
            or window.ref.bot is not target.ref.bot
        ):
            raise RuntimeError("unexpected trade window identity")
        return window.ref

    window = await events.wait(bot, matched, timeout, session_id=status.session_id)
    await events.wait(bot, lambda: bot.get_trade_offers(window), timeout, session_id=status.session_id)
    await events.wait(
        bot,
        lambda: all(bot.get_item(bot.trade_input(window, i)).state != bb.SlotState.unknown for i in range(2)),
        timeout,
        session_id=status.session_id,
    )
    if any(bot.get_item(bot.trade_input(window, i)).state != bb.SlotState.empty for i in range(2)):
        raise RuntimeError("initial trade inputs must both be known empty")
    return window


async def close_trade(bot, events, window, timeout):
    if any(bot.get_item(bot.trade_input(window, i)).state != bb.SlotState.empty for i in range(2)):
        raise RuntimeError("cannot close with unknown or occupied trade inputs")
    start = len(events.rows)
    bot.close_container(window)
    await events.wait(
        bot,
        lambda: (
            bot.get_trade_window() is None
            and any(
                handle is bot
                and isinstance(event, bb.ContainerEvent)
                and event.server_closed
                and event.reference == window
                for handle, event in events.rows[start:]
            )
        ),
        timeout,
        session_id=window.session_id,
    )


@dataclass(frozen=True)
class Exchange:
    window: bb.WindowRef
    quote: bb.TradeOffer
    before: bb.Snapshot
    after: bb.Snapshot
    operations: tuple[bb.ItemOperationResult, ...]
    output_slot: int
    returned: tuple[int, ...]


async def exchange(bot, events, window, quote, *, operation_timeout=12, stage_extra=1, consolidate=False):
    validate_parameters(window.trader_unique_id, operation_timeout, operation_timeout, 1, stage_extra)
    if not quote.supported or quote.ref is None or quote.ref.window != window or quote.output is None:
        raise RuntimeError("unsupported or foreign quote")
    if quote.max_uses is not None and quote.max_uses >= 0 and quote.uses >= quote.max_uses:
        raise RuntimeError("trade is exhausted")
    before = bot.capture_snapshot()
    baseline = inventory_record(bot)
    operations, sources, returned = [], [], []
    ingredients = [item for item in (quote.input, quote.second_input) if item is not None]
    if not ingredients or any(item.count + stage_extra > 64 for item in ingredients):
        raise RuntimeError("quote cannot stage a nonzero residual within a stack")
    if any(bot.get_item(bot.trade_input(window, i)).state != bb.SlotState.empty for i in range(2)):
        raise RuntimeError("trade inputs must be known empty")
    output = next(
        (slot.slot for slot in bot.get_player_inventory() if slot.slot.index >= 9 and slot.state == bb.SlotState.empty),
        None,
    )
    if output is None:
        raise RuntimeError("no known empty output slot")

    async def terminal(operation):
        if (
            operation.key.bot is not window.bot
            or operation.key.session_id != window.session_id
            or operation.key.window != window
        ):
            raise RuntimeError("unexpected operation identity")
        result = await operation.wait(operation_timeout)
        operations.append(result)
        if result.outcome != bb.ItemRequestOutcome.completed or result.server_result != 0:
            raise RuntimeError(f"item request {result.key.request_id}: {result.outcome.name}: {result.reason}")
        if result.response is None or result.response.client_request_id != operation.key.request_id:
            raise RuntimeError("terminal response identity mismatch")
        return result

    for index, ingredient in enumerate(ingredients):
        amount = ingredient.count + stage_extra
        source = next(
            (
                slot
                for slot in bot.get_player_inventory()
                if slot.identifier == ingredient.identifier and slot.count >= amount and slot.slot != output
            ),
            None,
        )
        if source is None:
            raise RuntimeError("no known source stack can stage the required surplus")
        sources.append(source.slot)
        await terminal(bot.begin_move_item(source.slot, bot.trade_input(window, index), amount))
        if bot.get_item(bot.trade_input(window, index)).count != amount:
            raise RuntimeError("staging did not settle exactly")
    fresh = next(
        (
            offer
            for offer in bot.get_trade_offers(window).offers
            if offer.ref and offer.ref.recipe_net_id == quote.ref.recipe_net_id
        ),
        None,
    )
    if (
        fresh is None
        or fresh.input != quote.input
        or fresh.second_input != quote.second_input
        or fresh.output != quote.output
        or fresh.uses != quote.uses
    ):
        raise RuntimeError("quote changed while staging")
    await terminal(bot.begin_trade(fresh.ref, output))
    for index, source in enumerate(sources):
        residual = bot.get_item(bot.trade_input(window, index))
        if residual.identifier != ingredients[index].identifier or residual.count != stage_extra:
            raise RuntimeError("unexpected residual input")
        await terminal(bot.begin_move_item(residual.slot, source, residual.count))
        returned.append(residual.count)
    if bot.get_item(output).identifier != quote.output.identifier or bot.get_item(output).count != quote.output.count:
        raise RuntimeError("trade output did not settle exactly")
    if consolidate:
        destination = next(
            (
                slot
                for slot in bot.get_player_inventory()
                if slot.slot != output
                and slot.identifier == quote.output.identifier
                and slot.count + quote.output.count <= 64
            ),
            None,
        )
        if destination is not None:
            await terminal(bot.begin_move_item(output, destination.slot, quote.output.count))
            if bot.get_item(output).state != bb.SlotState.empty:
                raise RuntimeError("output consolidation did not empty its source")
    if any(bot.get_item(bot.trade_input(window, i)).state != bb.SlotState.empty for i in range(2)):
        raise RuntimeError("residual inputs were not returned")
    inputs = Counter()
    for item in ingredients:
        inputs[item.identifier] += item.count
    check_conservation(baseline, inventory_record(bot), inputs, quote.output)
    after = bot.capture_snapshot()
    after_record = inventory_record(bot)
    await close_trade(bot, events, window, operation_timeout)
    if inventory_record(bot) != after_record:
        raise RuntimeError("inventory changed during container close")
    return Exchange(window, fresh, before, after, tuple(operations), output.index, tuple(returned))


async def first_trade(
    bot,
    events,
    *,
    target_unique_id,
    prepared_items,
    prepare_timeout=120,
    operation_timeout=12,
    max_distance=6,
    stage_extra=1,
):
    validate_parameters(target_unique_id, prepare_timeout, operation_timeout, max_distance, stage_extra)
    if not prepared_items or any(
        not name or type(count) is not int or count <= 0 for name, count in prepared_items.items()
    ):
        raise ValueError("prepared_items must contain positive item counts")
    await bot.wait_ready(prepare_timeout)
    session_id = bot.get_status().session_id

    def prepared():
        if not all(
            (count := bot.count_player_item(name)).complete and count.observed_count >= minimum
            for name, minimum in prepared_items.items()
        ):
            return None
        return find_target(bot, target_unique_id, max_distance)

    target = await events.wait(bot, prepared, prepare_timeout, session_id=session_id)
    window = await open_trade(bot, events, target, operation_timeout)

    def eligible(offer):
        if not offer.supported or not offer.ref or not offer.input or not offer.output:
            return False
        if offer.max_uses >= 0 and offer.uses >= offer.max_uses:
            return False
        needed = Counter()
        for ingredient in (offer.input, offer.second_input):
            if ingredient is not None:
                if ingredient.identifier not in prepared_items or ingredient.count + stage_extra > 64:
                    return False
                needed[ingredient.identifier] += ingredient.count + stage_extra
        return all(bot.count_player_item(name).observed_count >= count for name, count in needed.items())

    quote = next((offer for offer in bot.get_trade_offers(window).offers if eligible(offer)), None)
    if quote is None:
        raise RuntimeError("no supported prepared offer")
    return await exchange(bot, events, window, quote, operation_timeout=operation_timeout, stage_extra=stage_extra)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--server-key-sha256", default="")
    parser.add_argument("--protocol", type=int, default=2193, choices=(1001, 2193))
    parser.add_argument("--name", default="python-trader")
    parser.add_argument("--uuid", default="")
    parser.add_argument("--target-unique-id", type=int, required=True)
    parser.add_argument("--prepared-item", action="append", required=True, metavar="IDENTIFIER=COUNT")
    parser.add_argument("--prepare-timeout", type=float, default=120)
    parser.add_argument("--operation-timeout", type=float, default=12)
    parser.add_argument("--max-distance", type=float, default=6)
    parser.add_argument("--stage-extra", type=int, default=1)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        validate_parameters(
            args.target_unique_id, args.prepare_timeout, args.operation_timeout, args.max_distance, args.stage_extra
        )
        args.prepared_items = {}
        for value in args.prepared_item:
            name, separator, count = value.rpartition("=")
            if not separator or not name or not count.isdecimal() or int(count) <= 0:
                raise ValueError("prepared-item must be IDENTIFIER=POSITIVE_COUNT")
            args.prepared_items[name] = int(count)
        args.options = connection_options(args)
    except ValueError as error:
        parser.error(str(error))
    return args


def archive_value(value):
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, bb.BotToken):
        return "opaque-bot-token"
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        return {name: getattr(value, name) for name in value.__dataclass_fields__}
    raise TypeError(f"cannot archive {type(value).__name__}")


async def run(args):
    events = Events()
    result = None
    async with bb.Runtime(events, events=bb.ALL_EVENTS) as runtime:
        bot = runtime.add_bot(args.options)
        try:
            result = await first_trade(
                bot,
                events,
                target_unique_id=args.target_unique_id,
                prepared_items=args.prepared_items,
                prepare_timeout=args.prepare_timeout,
                operation_timeout=args.operation_timeout,
                max_distance=args.max_distance,
                stage_extra=args.stage_extra,
            )
        finally:
            await bot.disconnect()
    args.archive.parent.mkdir(parents=True, exist_ok=True)
    with args.archive.open("x", encoding="utf-8") as archive:
        json.dump(result, archive, default=archive_value, indent=2)
        archive.write("\n")
    return result


def main(argv=None):
    asyncio.run(run(parse_args(argv)), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)


if __name__ == "__main__":
    main()
