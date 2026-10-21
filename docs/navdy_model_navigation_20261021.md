# Navdy Model Navigation

## Scope

Extend WIP-FR `06ee2975dfe2c794f81ebc78ff954e9a5071034d` with a separate,
bounded phone-to-HUD-to-comma model-navigation channel. Legacy display TBT,
OBD speed, ambient, USB-first/Bluetooth policy and vehicle control are unchanged.
The exact protocol is in `tools/navdy/NOO_PROTOCOL.md`.

Phone native guidance observations retain their original sequence and timestamp.
Whole route geometry is transferred once per generation/reconnection, checked by
SHA-256, point count and coordinate bounds, then cached separately from the small
live feedback. The comma ACK carries at most one 1024-character chunk. Partial,
stale, cancelled, disconnected or mismatched generations cannot enable N16 input.
The complete exchange time is charged to both guidance and independent GPS age.
No heartbeat or retransmission creates a new observation.

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

## Verification

- Comma isolated candidate, fresh ignition-OFF/noOutput guard: 1206 tests and
  146 subtests passed in 67.78 seconds after the final reader/freshness review.
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
