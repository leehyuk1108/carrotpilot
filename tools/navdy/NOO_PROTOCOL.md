# Navdy Model Navigation v1 (Development)

This is separate from the legacy display/navigation feedback. Legacy turn
numbers or receipt times MUST NOT be promoted into model provenance. The
existing phone RFCOMM owner and comma USB-first/Bluetooth transport stay intact.

## Phone To HUD

A dedicated `WAYON_MODEL_NAVIGATION_V1` notification carries a JSON object:

- `version`: integer 1; `source`: `tmap` or `naver`.
- `session`: producer-process UUID; `sequence`: increasing native guidance
  observation number, at most 2^53-1. Re-sending bytes never increments it.
- `observedAtMs`: original observation wall time. The phone retains monotonic
  age locally and rejects expired queued observations before writing.
- `active`: boolean; `offRoute`: boolean. Inactive, off-route or unknown route
  immediately clears model guidance. Display guidance remains independent.
- `routeId`: a new identifier (at most 64 characters) for each route generation,
  even when geometry is unchanged. No guessed chord or truncated route is accepted.
- `maneuver`: canonical `type`, `modifier`, `distanceMeters`. Unknown native
  maneuver codes disable model guidance; display mapping remains unchanged.
- Optional `position`: latitude, longitude, `bearingDegrees`, `speedMps`, and
  ORIGINAL `observedAtMs`. Missing direction is not synthesized as north.
- Optional `route`: `polyline6`, `pointCount`, `sha256`. The SHA-256 covers the
  complete ASCII polyline6 (latitude then longitude). Sent on route change/reconnection,
  not on every display update. Maximum 4096 points and 65536 encoded bytes.

The HUD validates native observation identity, sizes, numbers and source before
storing. At first receipt it bounds age using original phone wall time; accepted
clock uncertainty must be charged conservatively, not extend freshness. After
receipt all expiry uses monotonic time. Duplicates cannot extend deadlines.
Unsupported clock/skew or absent original timestamps fail closed.

## HUD To Comma

The normal ACK gains `modelNavigation` (object or null). Existing fields retain
their format. The object carries `version`, `source`, `session`, `sequence`,
`active`, `offRoute`, `routeId`, `ageMs`, `validForMs`, `maneuver`, optional
`position` (with independent `ageMs`), and optional `routeChunk`:

```
{"encoding":"polyline6","index":0,"count":4,"pointCount":300,
 "encodedLength":3400,"sha256":"...","data":"..."}
```

Each chunk is at most 1024 ASCII characters; all except the last have exactly
1024. There are at most 64 chunks. The comma request's optional `modelRoute`
object contains `routeId` and `index`; a missing/mismatched ID requests index 0.
Once complete, index -1 suppresses route transfer. Chunk transfer never delays
or replaces ordinary display/feedback frames. Total ACK limit stays 32768 bytes.

The host reassembles one route only, verifies exact length, SHA-256, point count,
coordinate bounds and complete polyline termination before publication. No
partial path is usable. Source/session/route changes clear old geometry. A
missing component, inactive/off-route state, stale source or disconnected link
clears model readiness immediately. Reconnection requires geometry again.

Both USB and Bluetooth charge the full measured exchange duration to model
feedback age and subtract it from remaining validity. Position keeps its own
original age. Polls never renew an unchanged source sequence. Guidance is
limited to 2 seconds from source observation and link silence to 2 seconds.

## Validation Boundary

Unit tests and offroad transport tests do not establish N16 inference or driving
performance. Jetson inference must be checked separately with the actual device.
No forced-onroad or vehicle actuation is part of this protocol's validation.
