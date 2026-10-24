# Navdy solid and dashed inner lanes (2026-10-25)

## Scope and contract

The Comma C4 renderer reads `carState.leftLaneLine` and `rightLaneLine`.
For an inner model lane with raw probability greater than 0.3, nonnegative
codes ending in zero are dashed and other codes are drawn solid. Mirror that
display policy in the Navdy sender; do not change the vehicle/vision producer
or lane-change control policy. Documented color/type codes 0..38 are accepted
except reserved type 9. Invalid, missing or low-confidence inputs are unknown.
Raw code 0 remains dashed, including the schema's default zero. This is not a
new independent claim that a camera positively classified that marking.

The installed v169 APK already implements `navLaneLeftType` and
`navLaneRightType` as **strings**: `solid`, `dashed`, `unknown`. No APK change
or reinstall is required. Unknown/missing types retain its existing dashed
fallback. Do not send `centerSolid` or `centerDashed`, which would also change
the color. Far lane types, road edges, colors, geometry, vehicles and all
control fields stay unchanged. Double-line codes follow C4's solid convention;
this feature does not add a second stroke or change the Navdy layout.

Types accompany every active valid-geometry snapshot. The native renderer
resets missing types to unknown when full geometry arrives, keeps types with
geometry on vehicle-only updates, and clears both on invalid/empty geometry,
inactive state or null input. Existing 500 ms service freshness and receiver
deadlines remain unchanged. No old type is cached in the sender.

## Source and recorded evidence

The pinned APK SHA256 is
`d1268a7bebbecd34e4ca48f1abac2518138f5048ab14d912c9eda53758c26651`.
Its actual `OpenpilotStateReceiver` passes the original parsed JSONObject to
`OpenpilotPathView`; `readLaneType` accepts the strings above and `drawLane`
uses a null PathEffect only for solid types. Other lanes independently reset
the shared Paint state. REA Android analysis was unavailable because the local
JADX MCP provider was not configured; the exact APK was inspected with the
existing pinned dexlib/baksmali tools instead.

Eight retained recent-drive qlogs contain 5,344 valid carState samples:
4,440 pairs (0,0), 195 (0,1), 454 (1,0), and 255 (1,1). Both sides really
change in the source data. These counts alone do not prove optical recognition
accuracy or synchronize a physical Navdy screen with the C4 screen.

A separate regression replay used 1,756 retained display snapshots with a
synthetic 3x3 lane-code matrix: 15,804 comparisons, 11,160 style-bearing cases.
Removing only the two added keys produces the exact prior JSON, including
geometry, vehicles, order and scalar values. Compressed requests add 15..30
bytes (mean 22.49) on those fixtures. At the unchanged 5 Hz this is at most
150 additional compressed payload bytes/second in this sample, not a measured
radio-capacity or stability guarantee. The genuine lower-level Bluetooth
stalls in `navdy_radio_stall_20261024.md` are separate and remain unresolved.

## Validation status

Native rendering, final source review and device application are recorded
below after execution. Private APKs, recorded inputs and device logs are not
included in Git.
