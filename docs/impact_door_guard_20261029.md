# Traverse parking impact door exclusion

## Cause and Scope

The installed Carrot impact service had no `impact_*` configuration. Its
`impact_require_door_locked` default was false, so it reported `armed` even
while the lock state was unknown. The original Wayon lock gate existed but
was not enabled. It checked locks, not the four physical door switches.

Only the parking impact service changes. Driving controls, Panda, model, Navdy,
phone APK, remote-start exclusion, impact thresholds and uploader are unchanged.
Other vehicle fingerprints retain their prior defaults.

## Behavior

- `CHEVROLET_TRAVERSE` enables lock and closed-door requirements by default.
  Explicit `impact_require_door_locked` and `impact_require_doors_closed`
  configuration remains supported. The vehicle installation enables both.
- Receive bus 0, `0x130`, byte 0 mask `0x06` uses Wayon's existing decoder:
  masked value 2 means unlocked. This patch does not invent another encoding.
- Receive bus 0, `0x12A` (`BCMDoorBeltStatus`) checks all four door switches:
  byte 1 mask `0x07`, byte 2 mask `0x80`. Transmit/forward echoes do not qualify.
- Unlocked, open, unknown or stale state blocks impact detection and discards
  pending candidates. Malformed door frames clear the door observation.
- After closing AND locking, retain the 3-second settling delay and the
  detector's 5-second arming/warmup. Open/close in one CAN drain also restarts
  warmup, including when there is no new accelerometer sample.
- Hold guarded impact candidates for 0.5 seconds before queueing. A door edge,
  missing guard, IMU interruption, ignition or incomplete CAN drain cancels
  them. Preserve the original event timestamp for remote-start exclusion.
- Recheck ignition and door freshness immediately before queueing an impact.
  Lock/unlock notifications are not blocked by the impact-only queue check.
- Process restart never trusts saved lock/door files as current evidence.

The pre-existing 10-second CAN state expiry remains. A sleeping vehicle with
no CAN frames reports `waitingForDoorState`, not `armed`. This deliberately
does not promise impact coverage while door/lock state is unknown. The 0.5-second
confirmation interval does not guarantee suppression if a door edge is lost or
delayed longer than that interval while older evidence is still fresh.

## Verification

On the Comma's native Python/Cap'n Proto runtime, isolated staged modules pass
363 tests across impact, impactd, vehicle events and cloud uploader. Tests do
not write production queues, send notifications or actuate the vehicle.

Recorded route `000001aa--5e1e07e506`, segments 0/16/17:

| Signal | Received | Result |
| --- | ---: | --- |
| Bus 0 `0x130` lock frame | 166 | All masked value 2, blocked as unlocked |
| Bus 0 `0x12A` door frame | 1,661 | Every result agrees with native GM DBC parser |

Recorded door transitions also align with `carState.doorOpen` transitions.
The sampled logs contain no physical lock-to-unlock transition, so they prove
frame reception and parser agreement, not that both physical lock states have
been verified. Locked/closed rearming and impact release are isolated tests.

A 15-second live observation received 1,502 CAN envelopes but zero CAN frames.
Fresh device/Panda state showed offroad, ignition off and controls disallowed.
No remote door/ignition command or physical door/impact trial was performed.

Runtime diagnostics are `/data/wayon_cloud/impact_status.json` and
`door_lock_status.json`. The latter now includes `doorOpen`; the former also
reports `requireDoorsClosed` and distinguishes `pausedDoorOpen` from missing
door or lock evidence. Config backups must stay private because they contain
cloud credentials.
