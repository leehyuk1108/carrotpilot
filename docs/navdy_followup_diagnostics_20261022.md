# Navdy Intermittent Flicker Follow-Up, 2026-10-22

## Observation

The user still reports intermittent loss of Comma widgets and ambient light
after v164. The same Comma was found on its current LAN by route/ARP and verified
SSH identity, not by assuming the old address still applied. Installed WIP-FR
was `d96722aab612e5cdd6c4228b16fcd98dc107d152`; Bluetooth negotiated protocol 2.
Navdy was absent from the Comma USB bus and the dedicated ADB server on port
5038. No app/ambient journal was available from USB in this observation.

A completed passive 125.083-second HCI/status capture, while offroad, contains
624 complete CRC-valid request/ACK pairs, all display status zero. The transport
stayed on the same connection and receiver session. Receiver clears stayed
205 -> 205, power invalidations 220 -> 220, and USB updates zero. These initial
counters are historical cumulative values, not new failures during this test.
Application RTT median/max was 59.25/140.28 ms; the largest consecutive observed
ACK receipt gap was 280.546 ms. No Bluetooth disconnection was captured.

HCI request/ACK median/p95/p99/max was 55.185/88.354/111.221/137.356 ms.
ACK-reader receipt minus HCI receipt median/max was 0.618/17.425 ms. Use the
actual feedback receipt timestamp, not the later display-loop poll timestamp,
when assessing the pipelined client's ACK reader.

The kernel also logged unknown ACL handle 3804, while the real encrypted link
used handle 2 and continued acknowledging every observed request. This warning
alone does not establish the intermittent failure's cause. The short offroad
test does not reproduce onroad payload sizes, CPU load, phone/ambient activity
or prove that the reported driving problem is fixed.

## Confirmed Diagnostic Gap

The bridge's disconnected status contained its exception, but the next successful
status replaced it with an empty reason. There was no retained per-link failure
event to distinguish radio failure, source expiry, receiver state clearing or a
local status-file error after recovery. Receiver clear/invalidations counters
also do not by themselves distinguish a live TTL expiry from a closed session.

The Navdy source review additionally found that generic session-end logs and
ambient `output blocked` records can hide the original timeout reason. Those
receiver paths are not changed by this Comma-only diagnostic patch. Their
internal BLE/app journals still need collection, or a separately tested receiver
diagnostic extension, before attributing the ambient blink to a specific cause.

## Diagnostic Change

- Keep the last failure after recovery and orderly bridge shutdown, with stage,
  exception type, connection timestamp, bounded link metrics and source state.
- Keep at most 16 transition events in status; log transitions through the
  existing nonblocking swaglog IPC for later analysis across process restarts.
  A per-run identifier separates bridge lifetimes. Status alone is process-local;
  normal swaglog retention still applies.
- Record receiver cumulative counters first as a baseline, then record only
  changed counters/session/owner/power state with the actual ACK receipt time.
  Per-reply elapsed time is separate from the batch's latest link summary.
  Normal USB heartbeat increments do not generate events or evict history.
- Record source validity and requested ambient/power state changes separately
  from transport exceptions. Error stage identifies connect, payload, exchange,
  feedback or status processing; it does not assert the underlying root cause.
- Whitelist receiver/link diagnostic fields. Do not record navigation route,
  destination, phone media, peer keys or the full telemetry payload.
  Only explicitly recognized public exception text or OS error/type categories
  enter persistent logs; subprocess command arguments are never included.
- Leave capture cadence, transport selection, 500 ms freshness, 1.5 s response
  deadline, ambient watchdog, controls and driving-model configuration unchanged.

The diagnostic patch is not a demonstrated fix for intermittent flicker.

## Validation

556 local Python tests pass, including new checks for recovery/shutdown retention,
bounded history, private-field exclusion, historical baselines, counter resets,
source-vs-link distinction, and logger failure isolation. Five existing native
DBC cases are excluded on macOS, as in the preceding pipeline validation; they
are not counted as passes for this change. Ruff passes for the changed production
modules and new test file. Native deployment/recovery results, if performed, are
recorded separately from this pre-deployment source validation.
