# Automatic Navdy Diagnostics

This is a recording change, not a Bluetooth dropout fix. The user requested
automatic logs on both devices while keeping Bluetooth and phone reconnection.

## Comma

`navdy_bridge` starts one daemon journal writer automatically. Files are outside
the route-upload tree at `/data/navdy/diagnostics/events.jsonl`, with three older
generations `events.1.jsonl` through `events.3.jsonl`. Each is bounded to 4 MiB
(16 MiB total); directory mode is 0700 and file mode is 0600.

Only allowlisted metadata is persisted: boot/run identity, wall and monotonic
time, transport lifecycle, source availability, receiver session/sequence,
display-power/ambient state, bounded phase timing summaries, and logging loss
counters. Existing cloudlog event cadence is preserved; new five-second
summaries stay local. Raw payloads, routes, song data, peer addresses and keys
are not copied into the journal.

The bounded 256-record queue never waits for the filesystem. Records are limited
to 4 KiB with an 8/s rate and 16-record burst. Storage retries are 30 seconds
apart and stop below 64 MiB or 5% available space. Shutdown waits at most 250 ms;
a kernel-blocked writer can outlive that wait. Logging is best-effort: queue
overflow, disk failure and abrupt power removal can lose recent records.

A representative full 1,394-byte heartbeat gives about 16.72 hours of quiet-link
retention at five-second intervals. This is an estimate, not a guaranteed
retention period: connection events reduce it. Collect logs soon after an issue.

## Navdy

The companion v169 APK adds a bounded phone-state journal. It retains the v168
socket cancellation fix and the existing main-process wireless/ambient logs.
The new phone journal uses a separate app-private 4 x 1 MiB ring, ten-second
heartbeat, and an invocation-only exception guard around the new hook. It does
not alter phone search, reconnect, pairing, freshness or wire protocol.

The Navdy base-state hook runs after subclass entry work. Treat its state as the
last observed base entry, not proof of a live phone connection. Compare monotonic
times within a Navdy boot; align Comma observations using existing ACK session
and sequence metadata. Wall clocks may jump. USB is only needed to retrieve the
Navdy files later, not for automatic recording.

## Source Verification

Focused diagnostics/bridge/reconnect tests: 144 passed. Navdy suite: 715 passed,
five DBC-dependent tests excluded on this Mac because native `params_pyx` is
unavailable. Lint and whitespace checks pass. No control, model, Panda, transport
or reconnect-policy source changes are included.

Phone-connected navigation/reconnection and road endurance cannot be validated
while the phone is unavailable. Installation and live persistence evidence will
be appended after final pinned-source review and parked-device verification.
