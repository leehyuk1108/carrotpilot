# N16 Device Installation

## Scope

The user requested N16 on the paired Orin NX 8 GB and explicitly excluded the
external cluster. This installation changes only the inference runtime and its
comma adapter. It does not connect the experimental phone navigation producer,
alter vehicle control/Panda, replace the internal fallback model, or change HUD
assets. Without a qualified navigation producer the map and instruction inputs
remain zero, cleared each frame; this is not live route-following validation.

## Deployment Identity

The upstream Jetson image is boot-gated by a signed base-release manifest. Its
signing private key is not available here. A device-local, explicitly installed
inference overlay therefore retains the signed base identity and records its
own content identity separately. It is not an upstream-signed release.

The comma requires a matching `/data/models/carrot-jetlink-n16.json` selection
and the host's `carrot_n16_overlay` capability. Model, overlay content, base
release, runtime, protocol and stateful capability must match. A missing or
mismatched overlay on this selected Jetson does not silently select Cinque.
Other unselected Jetsons and supported phone/Mac apps retain their existing
selection paths. An upstream base replacement requires a new matching overlay.

The prepared TensorRT engine is specific to this NX/runtime. The experimental
engine and previous Cinque cache/release are retained for recovery. The N16
server refuses automatic in-process rebuilds. Empty-cache installation remains
unqualified and must not be described as generally reliable.

## Runtime Corrections

- First external inference failure now uses the same five-second invalid-output
  hold as an already active session; late local fallback is not marked valid.
- Raw prediction capture precedes destructive parsing for diagnostic parity.
- No timeout, frame validity, actuation or driver-monitoring check is relaxed.

## Verification

Before installation, 264 focused N16 and Mac-selection tests passed. A separate
fault/resource group passed 73 tests with 13 native-IPC cases unavailable on
macOS. These overlapping suites are not a unique combined count.

Native installation, camera/fallback and restart results must be recorded below
after execution. Prior synthetic results are documented separately in
`n16_reverification_20261021.md`; they are not road validation.
