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

Queued hotbar selections survive position corrections and precede subsequent
interactions. Death, dimension changes, and disconnect invalidate queued selections.

Collections support indexed/keyed reads, iteration, detached `copy()`, and explicit
`to_list()` or `to_dict()`. Dictionary output retains domain enums and opaque
BotToken identity, converts protocol enums and numeric ID wrappers to integers,
and preserves binary values as bytes. NBT remains tagged, including the element
type of an empty list. Item and metadata projections preserve their legacy fields
while retaining immutable native storage. `wire_bytes()` means re-encoding, not
proof of the original bytes received from the peer.

Player observations retain their initial velocity, rotation, and links. Remote
player movement updates position and rotation and emits `actor_motion`; retained
captures keep their previous values.

# Statistics

`connected_bytes_sent` and `connected_bytes_received` are optional integers.
Both remain `None` in every state because official NetherNet exposes no DTLS
byte counters. Application batch and packet counters remain available. Captures
retain their values, and reconnect starts new application counters.

`batch_bytes_in` counts received batch bytes before decoding, including malformed
batches. Each successfully decoded batch counts all its headers, including
unknown, unselected, and subclient-filtered packets, in `packets_in` and the
1024-entry `packet_counts_in`. A native batch decoding failure increments
`decode_errors` and counts its input bytes, but contributes **no packets or partial
histogram prefix**. This intentionally replaces legacy partial-prefix accounting.
A later Python business-validation failure retains the already counted headers.
Outbound bytes, packets, and `packet_counts_out` advance only after awaited
`Connection.send()` returns, independently of the operation's submitted phase.

# Transport and lifetime

The pinned official NetherNet verifier requires the server JWT's `cpk` to be a
base64 DER string. It rejects a JWK object before cryptographic verification or
the pin callback. The historical
[JWK candidate `19176ef`](https://github.com/ReallocAll/nethernet-python/commit/19176ef)
passed actual BDS JWT signature verification and SDP fingerprint
checks, with one `LOGIN_SUCCESS` observed. This used an explicit candidate-wheel override; the
patch is not merged upstream, no upstream PR has been created, and the production
dependency pin remains `695095c`.

Ready and trade remain unaccepted. A separate ICE diagnostic recorded six
unanswered consent requests to `198.18.0.1:49309` before consent expired. That
address belongs to Mihomo / Meta Tunnel; the exact rewriting rule and root cause
are unproven. A verified direct path is required before further live business
validation. Identity verification and mandatory pinning stay enabled.

The application queue accepts at most 4096 packets and 4 MiB of estimated encoded
data, including a batch being submitted. Commands are revalidated before batch
encoding. An encoding or send failure ends the session; failure or cancellation
after an item request is submitted leaves its outcome indeterminate. Never retry
an indeterminate transaction automatically.

Complete wire messages are limited to 32 MiB plus 1 KiB in each direction. The
receive check runs after upstream reassembly. These application limits do not
bound transport backlog or reassembly memory. Official `Connection.send()`
currently provides no capacity wait or atomic fragment reservation.

The adapter uses host ICE candidates through public configuration. Session
cleanup joins its reader, sender, ticker and connection attempt, resolves queued
boundaries and operations, and awaits public connection close. Runtime exit waits
for this bot-owned cleanup; a cancelled close waiter does not cancel it.

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
