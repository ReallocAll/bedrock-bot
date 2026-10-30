"""Controlled-byte villager cycle tests."""

import dataclasses

import pytest
from peer import until
from trade_peer import (
    action,
    close_packet,
    deliver,
    inventory_packet,
    open_trade,
    requests,
    response_packet,
    scene,
    sent,
    side,
    stack,
    wire_slot,
)

import endpilot as api


def inventory_record(bot):
    return [
        (
            slot.state,
            slot.network_id,
            slot.count,
            slot.stack_id,
            None if slot.item is None else bytes(slot.item.user_data_buffer),
        )
        for slot in bot.get_player_inventory()
    ]


def check_conservation(before, after, *, price=3):
    expected = list(before)
    source = list(expected[9])
    source[2] -= price
    if source[2] == 0:
        source = [api.SlotState.empty, 0, 0, 0, b""]
    else:
        source[3] = after[9][3]
    expected[9] = tuple(source)
    expected[10] = (api.SlotState.occupied, 42, 1, after[10][3], b"\0" * 10)
    if after != expected:
        raise RuntimeError("inventory conservation failed")


@pytest.mark.parametrize("version", [1001, 2193])
@pytest.mark.parametrize("initial_count", [4, 10])
async def test_stage_four_trade_three_return_one_and_exact_close(monkeypatch, version, initial_count):
    retained = []
    async with scene(monkeypatch, version) as (bot, peer, events):
        p = peer.p
        await deliver(
            bot,
            peer,
            inventory_packet(
                p, {9: stack(p, 43, initial_count, 101), 20: stack(p, 44, 6, 701, data=b"unrelated item")}
            ),
        )
        window = await open_trade(bot, peer, staged=0)
        input_a, input_b, source, output = (
            bot.trade_input(window, 0),
            bot.trade_input(window, 1),
            bot.player_slot(9),
            bot.player_slot(10),
        )
        quote = bot.get_trade_offers(window).offers[0]
        assert quote.input.count == 3 and quote.output.count == 1
        before = inventory_record(bot)
        stage = bot.begin_move_item(source, input_a, 4)
        assert bot.get_item(source).count == initial_count and bot.get_item(input_a).count == 0
        request = await sent(peer)
        place = action(request.actions[0])
        assert "Place" in type(place).__name__ and place.amount == 4
        assert wire_slot(side(place, "source")) == (29, 0, 101)
        assert wire_slot(side(place, "destination")) == (47, 4, 0)
        peer.send(
            response_packet(p, -1, [(29, 0, initial_count - 4, 101 if initial_count > 4 else 0), (47, 4, 4, 301)])
        )
        retained.append((stage, await stage.wait(2)))
        trade = bot.begin_trade(quote.ref, output)
        assert bot.get_item(input_a).count == 4 and bot.get_item(output).count == 0
        await sent(peer, 2)
        peer.send(response_packet(p, -3, [(47, 4, 1, 301), (29, 1, 1, 501)]))
        retained.append((trade, await trade.wait(2)))
        residual = bot.get_item(input_a).count
        assert residual == 1
        returned = bot.begin_move_item(input_a, source, residual)
        request = await sent(peer, 3)
        place = action(request.actions[0])
        assert "Place" in type(place).__name__ and place.amount == 1
        assert wire_slot(side(place, "source")) == (47, 4, 301)
        assert wire_slot(side(place, "destination")) == (29, 0, 101 if initial_count > 4 else 0)
        peer.send(
            response_packet(p, -5, [(47, 4, 0, 0), (29, 0, initial_count - 3, 101 if initial_count > 4 else 401)])
        )
        retained.append((returned, await returned.wait(2)))
        assert [request.client_request_id.raw_id for request in requests(peer)] == [-1, -3, -5]
        assert all(result.outcome == api.ItemRequestOutcome.completed for _, result in retained)
        assert all(bot.get_item(slot).state == api.SlotState.empty for slot in (input_a, input_b))
        assert bot.get_item(output).count == 1 and bot.get_item(source).count == initial_count - 3
        after = inventory_record(bot)
        check_conservation(before, after)
        tampered = list(after)
        damaged_output = list(tampered[10])
        damaged_output[2] = 2
        tampered[10] = tuple(damaged_output)
        with pytest.raises(RuntimeError, match="conservation"):
            check_conservation(before, tampered)
        bot.close_container(window)
        assert bot.get_trade_window().closing
        await deliver(bot, peer, close_packet(p, 6))
        assert bot.get_trade_window().ref == window
        assert not any(isinstance(event, api.ContainerEvent) and event.server_closed for event in events)
        await deliver(bot, peer, close_packet(p))
        await until(lambda: bot.get_trade_window() is None)
        closed = [event for event in events if isinstance(event, api.ContainerEvent) and event.server_closed]
        assert len(closed) == 1 and closed[0].reference == window
        assert inventory_record(bot) == after
        assert all(result.key.window == window for _, result in retained)
        await bot.disconnect()
    assert peer.closed
    for operation, result in retained:
        assert operation.result is result and operation.phase == api.ItemOperationPhase.terminal
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.reason = "changed"
