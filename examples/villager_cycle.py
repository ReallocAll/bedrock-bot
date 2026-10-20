"""Exhaust, observe natural restock, trade again, attack once, and reconnect the same account."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

from first_trade import (
    Events,
    archive_value,
    close_trade,
    connection_options,
    exchange,
    find_target,
    inventory_record,
    open_trade,
    totals,
    validate_parameters,
)

import bedrock_bot as bb

WHEAT, EMERALD = "minecraft:wheat", "minecraft:emerald"


def select_quote(bot, window):
    offers = bot.get_trade_offers(window)
    matches = (
        []
        if offers is None
        else [
            offer
            for offer in offers.offers
            if offer.supported
            and offer.ref
            and offer.input
            and offer.input.identifier == WHEAT
            and offer.second_input is None
            and offer.output
            and offer.output.identifier == EMERALD
            and offer.output.count == 1
        ]
    )
    if len(matches) != 1:
        raise RuntimeError("wheat-to-one-emerald quote is missing or ambiguous")
    quote = matches[0]
    if quote.max_uses is None or quote.max_uses < 16 or quote.uses is None or quote.uses < 0:
        raise RuntimeError("cycle requires a known offer with at least sixteen uses")
    if not 1 <= quote.input.count < 64:
        raise RuntimeError("cycle requires a price permitting a nonzero staged residual")
    return quote


def health(bot, entity):
    actor = bot.capture_actor(entity.ref.runtime_id)
    if actor is None or actor.unique_id != entity.ref.unique_id:
        return None
    return next(
        (attribute.current_value for attribute in actor.attributes if attribute.name == "minecraft:health"), None
    )


def check_cycle_inventory(before, after, prices):
    expected = totals(before)
    expected[WHEAT] -= sum(prices)
    expected[EMERALD] += len(prices)
    if expected[WHEAT] < 0 or totals(after) != +expected:
        raise RuntimeError("cycle inventory conservation failed")
    if [row for row in before if row["identifier"] not in (WHEAT, EMERALD, None)] != [
        row for row in after if row["identifier"] not in (WHEAT, EMERALD, None)
    ]:
        raise RuntimeError("cycle changed unrelated inventory")


def check_reconnected_inventory(before, after):
    if len(before) != 36 or len(after) != 36:
        raise RuntimeError("same-account reconnect requires complete known inventory")
    for index, (old, new) in enumerate(zip(before, after, strict=True)):
        for row in (old, new):
            if (
                type(row.get("index")) is not int
                or row["index"] != index
                or row.get("state") not in ("empty", "occupied")
                or row["state"] == "occupied"
                and row.get("identifier") is None
            ):
                raise RuntimeError("same-account reconnect requires complete known inventory")
        if all(
            row["state"] == "occupied"
            and type(row.get("stack_id")) is int
            and row["stack_id"] > 0
            and isinstance(row.get("item"), dict)
            and type(row["item"].get("net_id")) is int
            and row["item"]["net_id"] == row["stack_id"]
            for row in (old, new)
        ):
            old = {**old, "stack_id": 0, "item": {**old["item"], "net_id": 0}}
            new = {**new, "stack_id": 0, "item": {**new["item"], "net_id": 0}}
        if old != new or any(type(old[key]) is not type(new[key]) for key in old):
            raise RuntimeError("same-account reconnect changed inventory")
        old_item, new_item = old.get("item"), new.get("item")
        if isinstance(old_item, dict) and any(type(old_item[key]) is not type(new_item[key]) for key in old_item):
            raise RuntimeError("same-account reconnect changed inventory")


async def villager_cycle(
    bot,
    events,
    *,
    trader_unique_id,
    attack_target_unique_id,
    prepared_wheat=2048,
    prepare_timeout=600,
    operation_timeout=12,
    restock_timeout=1800,
    max_distance=3.5,
):
    validate_parameters(trader_unique_id, prepare_timeout, operation_timeout, max_distance)
    validate_parameters(attack_target_unique_id, prepare_timeout, operation_timeout, max_distance)
    if trader_unique_id == attack_target_unique_id:
        raise ValueError("trader and attack target must be distinct")
    if (
        type(prepared_wheat) is not int
        or prepared_wheat <= 0
        or not math.isfinite(restock_timeout)
        or restock_timeout <= 0
    ):
        raise ValueError("prepared wheat and restock timeout must be positive")
    await bot.wait_ready(prepare_timeout)
    initial_status = bot.get_status()
    initial_options = bot.capture_options()
    session_id = initial_status.session_id

    def prepared():
        wheat, emerald = bot.count_player_item(WHEAT), bot.count_player_item(EMERALD)
        if (
            not wheat.complete
            or not emerald.complete
            or wheat.observed_count != prepared_wheat
            or emerald.observed_count
        ):
            return None
        trader = find_target(bot, trader_unique_id, max_distance)
        target = find_target(bot, attack_target_unique_id, max_distance, trader=False)
        if trader and target and (value := health(bot, target)) is not None and value > 0:
            return trader, target
        return None

    trader, target = await events.wait(bot, prepared, prepare_timeout, session_id=session_id)
    baseline = inventory_record(bot)
    initial = bot.capture_snapshot()
    exchanges, prices = [], []

    async def reopen():
        current = find_target(bot, trader_unique_id, max_distance)
        if current is None or current.ref != trader.ref or bot.get_status().dimension != initial_status.dimension:
            raise RuntimeError("prepared trader or dimension context changed")
        window = await open_trade(bot, events, current, operation_timeout)
        return window, select_quote(bot, window)

    async def perform(window, quote):
        result = await exchange(bot, events, window, quote, operation_timeout=operation_timeout, consolidate=True)
        exchanges.append(result)
        prices.append(quote.input.count)
        check_cycle_inventory(baseline, inventory_record(bot), prices)
        return result

    async with asyncio.timeout(prepare_timeout):
        window, quote = await reopen()
        while quote.uses:
            await close_trade(bot, events, window, operation_timeout)
            tick = bot.get_status().tick
            await events.wait(
                bot, lambda tick=tick: bot.get_status().tick >= tick + 20, prepare_timeout, session_id=session_id
            )
            window, quote = await reopen()
    maximum = quote.max_uses
    for expected_uses in range(maximum):
        if quote.uses != expected_uses or quote.max_uses != maximum:
            raise RuntimeError("offer uses did not advance exactly once per completed trade")
        await perform(window, quote)
        window, quote = await reopen()
    if quote.uses != maximum or quote.max_uses != maximum:
        raise RuntimeError("fresh exhausted quote was not observed")
    exhausted_quote = quote
    empty = next((slot.slot for slot in bot.get_player_inventory() if slot.state == bb.SlotState.empty), None)
    if empty is None:
        raise RuntimeError("no empty destination for local exhaustion refusal")
    before_refusal = inventory_record(bot)
    request_count = bot.capture_statistics().packet_counts_out[147]
    event_start = len(events.rows)
    try:
        bot.begin_trade(quote.ref, empty)
    except RuntimeError as error:
        if str(error) != "trade is exhausted":
            raise
    else:
        raise RuntimeError("exhausted quote unexpectedly created an operation")
    await close_trade(bot, events, window, operation_timeout)
    if (
        inventory_record(bot) != before_refusal
        or bot.capture_statistics().packet_counts_out[147] != request_count
        or any(handle is bot and isinstance(event, bb.ItemRequestEvent) for handle, event in events.rows[event_start:])
    ):
        raise RuntimeError("local exhaustion refusal changed inventory or emitted a request")
    async with asyncio.timeout(restock_timeout):
        while True:
            tick = bot.get_status().tick
            await events.wait(
                bot, lambda tick=tick: bot.get_status().tick >= tick + 20, restock_timeout, session_id=session_id
            )
            window, quote = await reopen()
            check_cycle_inventory(baseline, inventory_record(bot), prices)
            if quote.uses == 0:
                break
            await close_trade(bot, events, window, operation_timeout)
    restocked_quote = quote
    last = await perform(window, quote)

    fresh_target = find_target(bot, attack_target_unique_id, max_distance, trader=False)
    if fresh_target is None or fresh_target.ref != target.ref or (before_health := health(bot, target)) is None:
        raise RuntimeError("attack target identity or health is unavailable")
    bot.select_hotbar(0)
    status = bot.get_status()
    dx, dy, dz = (
        target.position.x - status.position.x,
        target.position.y + 1 - status.position.y,
        target.position.z - status.position.z,
    )
    bot.set_look(-math.degrees(math.atan2(dy, math.hypot(dx, dz))), math.degrees(math.atan2(-dx, dz)))
    await events.wait(bot, lambda: bot.get_status().tick > status.tick, operation_timeout, session_id=session_id)
    attack_start = len(events.rows)
    bot.attack_actor(target.ref, bb.Vec3(0, 1, 0))

    def damaged():
        for handle, event in events.rows[attack_start:]:
            if (
                handle is bot
                and isinstance(event, bb.ActorEvent)
                and event.kind == bb.ActorEvent.Kind.metadata
                and event.actor.runtime_id == target.ref.runtime_id
                and event.actor.unique_id == target.ref.unique_id
            ):
                value = next(
                    (
                        attribute.current_value
                        for attribute in event.actor.attributes
                        if attribute.name == "minecraft:health"
                    ),
                    None,
                )
                if value is not None and value < before_health:
                    return event.actor
        return None

    damaged_actor = await events.wait(bot, damaged, operation_timeout, session_id=session_id)
    before_reconnect = inventory_record(bot)
    retained = tuple(result.copy() for trade in exchanges for result in trade.operations)
    check_cycle_inventory(baseline, before_reconnect, prices)
    await bot.reconnect()
    await bot.wait_ready(operation_timeout)
    new_status = bot.get_status()
    if (
        new_status.session_id == session_id
        or new_status.dimension != initial_status.dimension
        or bot.capture_options().uuid != initial_options.uuid
        or bot.player_slot(0).bot is not trader.ref.bot
    ):
        raise RuntimeError("same-account reconnect identity failed")
    await events.wait(
        bot, lambda: bot.count_player_item(WHEAT).complete, operation_timeout, session_id=new_status.session_id
    )
    after_reconnect = inventory_record(bot)
    if totals(before_reconnect) != totals(after_reconnect):
        raise RuntimeError("same-account reconnect changed inventory totals")
    check_reconnected_inventory(before_reconnect, after_reconnect)
    for action in (
        lambda: bot.interact_actor(trader.ref),
        lambda: bot.close_container(last.window),
        lambda: bot.begin_trade(last.quote.ref, bot.player_slot(last.output_slot)),
        lambda: bot.get_item(bb.InventorySlot(trader.ref.bot, session_id, bb.InventoryArea.player, 0)),
    ):
        try:
            action()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("stale reference was accepted after reconnect")
    originals = tuple(result for trade in exchanges for result in trade.operations)
    if any(original.to_dict() != copied.to_dict() for original, copied in zip(originals, retained, strict=True)):
        raise RuntimeError("retained item results changed")
    return {
        "initial": initial,
        "exchanges": tuple(exchanges),
        "exhausted": exhausted_quote,
        "restocked": restocked_quote,
        "attack_before_health": before_health,
        "attack_after": damaged_actor,
        "old_session_id": session_id,
        "new_session_id": new_status.session_id,
        "before_reconnect": before_reconnect,
        "after_reconnect": after_reconnect,
        "retained_results": retained,
        "requires_server_gameplay_corroboration": True,
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--server-key-sha256", required=True)
    parser.add_argument("--name", default="python-villager-cycle")
    parser.add_argument("--uuid", default="")
    parser.add_argument("--protocol", type=int, default=2193, choices=(1001, 2193))
    parser.add_argument("--trader-unique-id", required=True, type=int)
    parser.add_argument("--attack-target-unique-id", required=True, type=int)
    parser.add_argument("--prepared-wheat", type=int, default=2048)
    parser.add_argument("--prepare-timeout", type=float, default=600)
    parser.add_argument("--operation-timeout", type=float, default=12)
    parser.add_argument("--restock-timeout", type=float, default=1800)
    parser.add_argument("--max-distance", type=float, default=3.5)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        validate_parameters(args.trader_unique_id, args.prepare_timeout, args.operation_timeout, args.max_distance)
        validate_parameters(
            args.attack_target_unique_id, args.prepare_timeout, args.operation_timeout, args.max_distance
        )
        if args.trader_unique_id == args.attack_target_unique_id or args.prepared_wheat <= 0:
            raise ValueError("targets must differ and prepared wheat must be positive")
        if not math.isfinite(args.restock_timeout) or args.restock_timeout <= 0:
            raise ValueError("restock timeout must be positive and finite")
        args.options = connection_options(args)
    except ValueError as error:
        parser.error(str(error))
    return args


async def run(args):
    events = Events()
    async with bb.Runtime(events, events=bb.ALL_EVENTS) as runtime:
        bot = runtime.add_bot(args.options)
        try:
            result = await villager_cycle(
                bot,
                events,
                trader_unique_id=args.trader_unique_id,
                attack_target_unique_id=args.attack_target_unique_id,
                prepared_wheat=args.prepared_wheat,
                prepare_timeout=args.prepare_timeout,
                operation_timeout=args.operation_timeout,
                restock_timeout=args.restock_timeout,
                max_distance=args.max_distance,
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
