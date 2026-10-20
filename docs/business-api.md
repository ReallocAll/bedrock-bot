# Observed business operations

Player slots are `bot.player_slot(0..35)`. Trade inputs are
`bot.trade_input(window, 0..1)`. `get_item()` distinguishes unknown slots,
known-empty slots, and occupied items whose registry identity is unresolved.
Optional item counts and stack IDs remain `None` when unavailable. A complete
inventory count requires all 36 player slots and unambiguous registry identities.

`get_trade_offers(window)` returns `None` before trade data is observed, a present
empty collection for an empty Recipes list, and unsupported rows with reasons
for malformed or unsupported recipes. A malformed top-level Recipes value raises
an error. Offers carry the window generation and observed revision; fresh trade
data invalidates old offer references for future admissions.

Move and trade admission validates ownership, current references, known item
identities, confirmed stack IDs, available counts, and destination compatibility.
Failed validation consumes no request ID. Accepted requests use -1, -3, -5, and so
on within each connection, including explicitly cancelled queued requests.
Admission captures the intended changes without editing observed inventory.

An operation publishes `submitted` immediately before the transport send can have
a side effect. Successful responses settle all validated changes atomically.
Only updates observed after submission can substitute for omitted touched-slot
responses. Rejection preserves known state; indeterminate settlement invalidates
only touched slots. Results are immutable and published before item-request
callbacks. A reconnect preserves the Bot handle and token but invalidates old
session, entity, window, offer, slot, and request references.

Collections support indexed/keyed reads, iteration, detached `copy()`, and explicit
`to_list()` or `to_dict()`. Dictionary output retains domain enums and opaque
BotToken identity, converts protocol enums and numeric ID wrappers to integers,
and preserves binary values as bytes. NBT remains tagged, including the element
type of an empty list. Item and metadata projections preserve their legacy fields
while retaining immutable native storage. `wire_bytes()` means re-encoding, not
proof of the original bytes received from the peer.

# Statistics

`capture_statistics()` samples the current connection's synchronous
`get_dtls_byte_counts()` accessor. `connected_bytes_sent` and
`connected_bytes_received` count cumulative bytes at the DTLS ICE-facing
boundary, including handshake, SCTP, and retransmissions. They exclude HTTP
signaling, UDP/IP headers, and ICE/STUN/TURN overhead. They are not batch-byte or
queue-occupancy estimates. Final closed-state callbacks occur after awaited
transport close; captures retain their values, and a new connection starts new
counters. Attempts that fail before returning a Connection do not provide
complete attempt-traffic statistics.

`batch_bytes_in` counts received batch bytes before decoding, including malformed
batches. Each successfully decoded batch counts all its headers, including
unknown, unselected, and subclient-filtered packets, in `packets_in` and the
1024-entry `packet_counts_in`. A native batch decoding failure increments
`decode_errors` and counts its input bytes, but contributes **no packets or partial
histogram prefix**. This intentionally replaces legacy partial-prefix accounting.
A later Python business-validation failure retains the already counted headers.
Outbound bytes, packets, and `packet_counts_out` advance only after synchronous
send acceptance, independently of the operation's submitted phase.

# Direct asyncio scenarios

`examples/first_trade.py` exposes `first_trade()` and the smaller `exchange()` for
an already prepared, observed window. `examples/villager_cycle.py` exposes
`villager_cycle()`. Create the example's `Events` callback with a Runtime using
`ALL_EVENTS`, await readiness and the external preparation/player gate, then call
the coroutine directly. The caller owns Runtime cleanup; each CLI awaits
disconnect and exits its async Runtime context.

The first exchange preserves its baseline snapshot, stages the price plus a
nonzero surplus through legitimate moves, trades, returns the residual, requires
both inputs to be known empty, checks inventory conservation, and waits for the
same window's server-close acknowledgement. Returned results remain inspectable
after cleanup. Timeouts and uncertain outcomes raise; these scenarios do not
retry uncertain transactions.

The complete cycle requires at least sixteen uses and follows the actual observed
maximum, verifies local exhausted-offer refusal, waits for natural restock by
observing refreshed windows, and trades at the refreshed price. It preserves
unrelated inventory, issues one attack requiring a fresh target-health decrease,
and reconnects the same account to check totals and stale-reference refusal.
Server preparation, work/restock corroboration, attack corroboration, and fresh
player gates belong to the server harness. Controlled byte tests and local DTLS
tests do not establish actual BDS gameplay or performance acceptance.

Both CLIs require `--server-url`, `--server-key-sha256`, exact target unique IDs,
and an output `--archive`. Run either script with `--help` for the preparation and
timeout arguments. They do not invoke another bot CLI, RPC service, or server
commands.
