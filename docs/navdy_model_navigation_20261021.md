# Navdy Model Navigation

## Scope

Extend WIP-FR `06ee2975dfe2c794f81ebc78ff954e9a5071034d` with a separate,
bounded phone-to-HUD-to-comma model-navigation channel. Legacy display TBT,
OBD speed, ambient, USB-first/Bluetooth policy and vehicle control are unchanged.
The exact protocol is in `tools/navdy/NOO_PROTOCOL.md`.

Phone native guidance observations retain their original sequence and timestamp.
Whole route geometry is offered on generation/reconnection and re-offered on
fresh native observations with bounded backoff after socket writes. A socket
write is not a HUD acceptance acknowledgement. Geometry is checked by
SHA-256, point count and coordinate bounds, then cached separately from the small
live feedback. The comma ACK carries at most one 1024-character chunk. Partial,
stale, cancelled, disconnected or mismatched generations cannot enable N16 input.
The complete exchange time is charged to both guidance and independent GPS age.
No heartbeat or retransmission creates a new observation. Re-offers retain the
original sequence/time and can take up to 30 seconds plus native-observation
cadence and transfer time after silent receiver loss.

Carrot's selected Navdy source publishes only verified route geometry paired
with canonical native maneuvers. Legacy Navdy numeric display codes are not
relabelled as model maneuvers and Mapbox does not silently replace this source.
Optional phone GPS requires its own original fix time and heading; otherwise
the existing verified comma GPS sources remain in use.

## APK Boundary

The accompanying phone/HUD changes are maintained in the separate local Navdy
project, not installed by Git Pull. They require same-signer APK updates without
uninstalling or clearing data. Existing installed APKs were pulled and hashed
before patching. No keys, private route captures or proprietary APKs belong here.

The initial phone producer supports verified Naver native guidance. TMAP's
existing 400-metre preview is not a proven full route and is not accepted as
such. Unsupported native maneuvers and oversized routes fail closed. These
limitations must not be reported as complete support for every navigation app.

Native phone diagnostics identified the user's active approximately 406-km
route as 5627 points, `RouteGuiding`, `VALID`, `RouteOn`, and `Right`. The initial
4096-point gate rejected it. Navdy's complete-route limit is now 8192 points;
the 65536 encoded-byte/64-chunk/32-KiB ACK bounds are unchanged. No route points
are truncated, decimated or replaced by destination chords. Other navigation
sources retain their previous 4096-point bound.

## Verification

- Comma isolated candidate, fresh ignition-OFF/noOutput guard: 1217 tests and
  146 subtests passed in 70.94 seconds after the reader/freshness and whole-route
  capacity corrections.
  The suite includes native map rasterization, N16 input packing, Navdy,
  Jetlink faults, GM manual hold/display speed, NNFF and vehicle tuning.
- The new tracker/reader tests cover duplicate source age, independent position
  age before initial assembly, cross-source replay, sidecar deletion and the
  race where new geometry arrives immediately after old input expires.
- Phone host tests: 88 passed. HUD host tests: 22 cases / 845 assertions passed.
  These are distinct suites and not an aggregate real-device result.
- Native APK preservation checks cover all unrelated DEX classes, resources,
  signatures and API-21 compatibility for new HUD helpers.

Actual route transport and installed APK checks are recorded separately after
deployment. Jetson is disconnected/off at the user's request: no native Jetson
inference, sustained driving-model timing or road validation is claimed here.
No forced-onroad, CAN actuation or Panda firmware change is part of these tests.

## Independent Review And Reader Recovery

Claude Code performed two independent read-only source reviews. The first found
that reader-only expiration could permanently reject a still-valid bridge
sidecar, and that sampling time before a lock or file read could make fresh
feedback appear to come from the future. The second confirmed those fixes and
found a remaining 100-ms polling-gate gap. Its exact pending-frame sequence
failed on the isolated comma candidate before correction and passed afterward.
Claude did not execute the tests; native test results are a separate check.

Each complete bridge assembly now has a local random generation token, written
to the sidecar before its corresponding outer feedback. Readers import geometry
only when the tokens match. Bridge clear/reassembly rotates the token even if
route identity and geometry are unchanged. A reader-only timeout can recover
the unchanged bridge cache with a new, fresh source sequence. Tokens do not
renew observations or bypass sequence high-water marks, cancellation or the
two-second deadline. They are local consistency markers, not authentication.

Readers take the clock inside the lock and after I/O, read waiting feedback
before expiration, and bypass the polling interval once when an existing value
would expire. Unchanged files and replayed source sequences still expire.
Specific rejection reasons survive subsequent status/snapshot calls.

The first complete post-review native run passed 1228 tests and 146 subtests in
70.36 seconds. The subsequent polling-gap correction passed all 59 focused
model-navigation tests. The final native regression, including the actual
`bridge.run()` sidecar-before-feedback/clear/reassembly regression, passed
1231 tests and 146 subtests in 70.05 seconds. These are isolated candidate tests,
not an inference or driving validation.

A third Claude review found that another freshness check could clear the value
between clock samples, disabling the value-dependent polling bypass. The gate
now uses the deadline crossing since the last poll, which survives that clear
and permits at most one extra poll. Its exact reproducer failed before the fix;
60 focused tests and the final native 1232-test / 146-subtest regression passed
(76.12 seconds). An independent reviewer also checked 32 reader/cache scenarios.

Same-signer installed APK hashes were verified on the actual phone (bridge and
Naver base APKs plus both original splits). The actual Navdy ART helper suite
passed 22 cases / 912 assertions using `app_process`; plain `dalvikvm` lacked
the SystemClock JNI initialization and is not counted as a passing test.
These helper tests do not establish delivery through the running HUD app.

At this checkpoint the phone reports fresh native guidance and route writes,
but the actual Bluetooth HUD response still contains `modelNavigation: null`.
The transport is connected. End-to-end route delivery is therefore not yet
validated and must not be described as working based on socket-write counters.
