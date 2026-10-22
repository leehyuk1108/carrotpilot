# Navdy signed-speed display dropout

## Verified Evidence

- USB diagnostics were collected through the dedicated ADB server while the
  display bridge remained in `bluetooth_only` mode. No USB telemetry was sent.
- Installed HUD APK matches the v164 signed artifact exactly:
  `9ad7dc0ff175d6de2d0a1fb09d01f06188971d2555a9269d2737b6d97c9af429`.
  Main HUD PID was unchanged since the prior deliberate installation.
- The persistent ambient journal shows repeated known-gear/unknown transitions
  during the latest retained driving session. Its reverse-OFF commands receive
  successful module protocol responses. Recent door-close fade/OFF is normal.
- Native recorded-state analysis: 629 carState samples, all message-valid and
  CAN-valid, none CAN-timeout; maximum logged carState gap 103 ms. The GM adapter
  intentionally signs wheel speeds, then applies its speed filter. There are
  169 negative filtered samples, including 65 with standstill true and 64 with
  raw speed exactly zero. Minimum is -1.2078565 m/s during reverse movement;
  stopped values oscillate down to tiny negative floating-point magnitudes.

The Navdy display mapper incorrectly required `vEgo >= 0` to accept *all*
vehicle state. Negative values therefore erased gear, availability and set
speed despite valid, fresh CAN data. This is separate from Bluetooth expiry.
Device wall clocks changed during boot, and the Android/Comma clocks are not
proven synchronized: no exact subsecond cross-device causal timing is claimed.

## Correction

Accept finite signed speeds with absolute magnitude below 100 m/s. Missing,
malformed, nonfinite and out-of-bound values remain invalid. Keep existing CAN
validity, timeout and service-age checks, immediate disengagement, reverse
suppression, and nonnegative displayed-speed sanitization. No vehicle-control,
model, Panda, Bluetooth TTL, queue, pairing or ambient policy changes.

## Verification

- New regression tests fail in five cases before the correction. They exercise
  sign crossings in D/N/P/R, reverse suppression and invalid/boundary inputs.
- Bridge integration also verifies zero crossing does not bypass subsequent
  expiry or CAN-invalid rejection.
- Native Comma execution of both pure mappers on all 629 recorded structs:
  460 outputs identical; differences confined to the 169 negative-speed frames.
  Unknown gear counts fall from 173 to the five actual recorded unknowns;
  gear transitions fall from 45 to nine. Reverse active visuals remain disabled.
- This replay publishes no messages and sends no commands to the car or HUD.
- Local focused suite: 571 passed; five native DBC cases excluded on macOS.
  Existing cpp pytest configuration warnings are unrelated.
- Ruff passes for the mapper and its tests. Five existing E701/E702 findings
  in the bridge test file reproduce identically at the base revision; the new
  integration test adds none. `git diff --check` passes.
- Independent source review found no actionable defect in the correction.

## Remaining Ambient / Link Uncertainty

This is **not** a claim that every ambient OFF or radio interruption is solved.
With fresh device/panda onroad state the bridge still refreshes ambient state,
even when speed rejection had hidden widgets. The old generic `output blocked`
journal does not distinguish reverse, vehicle timeout and shutdown. Unknown
gear preserves a reverse latch; it does not independently request OFF/ON.
Current offroad Bluetooth stability cannot establish driving stability.

Existing bounded bridge diagnostics retain future source/receiver transitions
and link failures. Preserve those and the USB journal rather than extending
stale-state TTL or disabling the ambient watchdog. Raw device logs/APK and
recorded frames remain private, outside this repository.

## Deployment

Pending a fresh offroad/ignition-OFF/no-output guard, publish to the requested
WIP-FR branch and fast-forward the matching device. Restart only navdy_bridge;
retain USB diagnostics and Bluetooth-only telemetry. Record actual recovery
and protected-process checks below after deployment; no physical display or
road-validation result is implied by this plan.
