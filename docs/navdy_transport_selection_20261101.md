# Navdy wired / wireless selection

`NavdyConnectionMode` is a persistent integer, shown beside Navigation Input in
Carrot Web's Speed & Decel / Speed Camera settings.

| Value | Policy |
| --- | --- |
| 0 | Preserve the existing Bluetooth configuration and USB-first fallback policy. |
| 1 | USB only, directly or through an already paired Jetson relay. Never fall back to Comma Bluetooth. |
| 2 | Comma Bluetooth only. USB remains available for diagnostics. |

This chooses the Comma-to-Navdy link, not the phone-to-Navdy connection. Phone
Bluetooth, phone automatic reconnection, ambient BLE, pairing identities, and
`CarrotNaviSource` are unchanged. A missing cable in mode 1 is a disconnected
state, not permission to switch back to Bluetooth. The Jetson relay can use its
existing SSH management network; its final connection to Navdy is USB.

The bridge checks the setting every five seconds, plus any already-running
bounded connection attempt. A policy change closes the old transport, clears
navigation/model cache data and resets retry backoff. Change modes while parked:
the handover briefly interrupts navigation input. Invalid stored values fail
closed instead of silently permitting a different transport. The API rejects
invalid values before numeric clamping; ordinary trailing whitespace in the
backing integer file is accepted.

## Deployment requirement

Rebuild native `openpilot/common/params_pyx.so` and restart the manager before
enabling the setting on an old installation. `Params::clearAll` removes keys
unknown to its loaded native table, including at ignition transitions. Merely
writing a raw parameter file or updating Python is insufficient. Verify the
native key type and retention across manager/onroad/offroad clears in an isolated
Params directory, then confirm the real setting after a fresh manager/reboot.

## Verification scope

- Policy and bridge simulations cover explicit USB/BT, legacy behavior, missing
  cable/peer, Jetson fallback, bidirectional changes, retry during backoff, stale
  feedback clearing, corrupt settings and unchanged Bluetooth configuration.
- Actual catalog/API tests cover registration, selection labels, save/reload,
  preservation of Navigation Input and rejected malformed writes. Shared web
  control/dialog tests exercise selection and cancellation.
- No APK update is required: the installed v175 already contains the USB service
  on port 8765 and common navigation/SECTION/model feedback paths.
- A USB reply is not proof of completed display rendering. The existing USB path
  posts updates to the HUD main thread before returning its reply. Its display
  queue-age behavior is not identical to Bluetooth's lease validation.
- No steering/braking calculation, navigation TTL, phone reconnection or Bluetooth
  deadline is changed. Offroad transport tests do not prove live route reception,
  panel appearance, physical unplug behavior or long-duration driving stability.

Device results are recorded separately after deployment; do not interpret the
source tests as proof of a successful vehicle install.
