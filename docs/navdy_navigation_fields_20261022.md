# Navdy navigation field corrections, 2026-10-22

## Scope

Target: `leehyuk1108/carrotpilot`, `WIP-FR`, based on `a51e7ab61f`.
The selected Navdy source remains restart-latched. Bluetooth is retained for
telemetry; USB remains diagnostic-only. No phone/HUD APK, pairing, driving model,
Panda firmware or vehicle actuation change is included.

## Corrections

- Publish an explicit external-navigation source and active flag. The onroad
  NAVI badge requires a live, valid publisher and accepted Navdy navigation;
  stale network addresses cannot make selected-but-inactive Navdy look active.
  Vehicle navigation (`vNAVI`) remains separate.
- Preserve the independent `roadPreview.bumpDistanceM` when another sign owns
  the guidance SDI slot. Both source components must be fresh and guidance
  active. An explicit type-22 clear/invalid distance takes precedence over an
  older preview, so a passed bump cannot be resurrected by the fallback.
- Retain existing camera/section priority over a secondary bump. Bump-only
  output uses the bump description rather than the empty primary SDI label.
- Publish an expired/removed Navdy road limit as zero (unknown) in CarrotMan and
  navigation instruction. Missing limits cannot lower the cruise set speed to
  zero through negative `ApplyModelSpeed`: `_auto_speed_up` retains the existing
  set speed when no positive road limit exists. Valid-limit behavior is retained.
- Preserve bounded, ordered G/O/R traffic segments in CarrotMan and the web live
  payload, and clear them with expired/inactive guidance. This is data delivery,
  not a new traffic UI widget or congestion-based control policy.

Existing Cap'n Proto ordinals are unchanged; new fields are appended. Readers
and the manager must reload the schema and Python modules together. Restarting
only a forked UI child is insufficient because the manager preimports modules.

## Validation

- The new failing-before tests reproduced missing bump fallback/description,
  stale published limits, missing availability and discarded traffic segments.
- Independent review found the zero-distance bump resurrection; a seven-case
  regression now covers explicit zero/invalid SDI versus older preview data.
- Focused desktop tests: 119 passed, including real Cap'n Proto serialization
  and web-payload clearing. A broader desktop run passed 738 cases; five native
  DBC cases could not import the macOS-missing Params extension. Native service
  imports also require the target's msgq extension, so these need target tests.
- On the target, an isolated candidate parsed the live Bluetooth feedback:
  road limit 30 km/h, camera 112 m, bump 96 m, and all 17 G/O/R traffic segments.
  Production code and services were unchanged during this probe.
- The isolated target suite passed 914 tests, including the native DBC cases,
  external/vehicle navigation priority, cruise buttons, Navdy transport/model
  boundaries, parser, publication, schema and HUD status. Test dependencies were
  staged only in `/tmp`; three warnings concern deliberately unloaded optional
  pytest plugins. No CAN commands, phone actions or forced onroad were issued.

## Deliberately Unresolved

The native TMAP secondary distance basis is not yet proven. Naver exports a
current-position distance, but the HUD guidance payload strips provider
identity. Network input historically uses an inter-turn distance. Do not switch
all Navdy inputs based on a guessed provider or `secondary >= primary`.
Current legacy internal addition and displayed secondary distance remain an
identified discrepancy pending an explicit producer distance-basis contract.

TMAP full-route NoO remains unsupported by the current producer:
`modelNavigation` is absent. Local road preview is capped at 400 m and cannot
be substituted for a verified complete selected route and fresh model guidance.
NAVI availability is not proof of model navigation readiness.

Offroad parser/publication tests do not establish physical bump braking,
onroad optical display correctness, or long-duration radio/ambient stability.
