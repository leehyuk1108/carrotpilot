# Navdy wired interruption and bounded recovery

## Incident evidence

The 2026-11-04 incident has two distinct parts:

- Navdy last received fresh Comma data at 11:33:09.815 KST. The host ADB
  USB read failed with `Protocol error` at 11:33:11.636. Navdy's existing
  3001 ms freshness watchdog then cleared stale output at 11:33:13.571.
- The dedicated ADB server on 5038 and an unused default server on 5037
  subsequently competed for the offline device. The bridge searched only 5038,
  ignored offline devices, and could neither reuse the other owner nor repair
  the stuck USB transport. Removing the unused server and restarting 5038 did
  not recover it; a Navdy-device-only USB reset did.

Phone connection-state heartbeats remained CONNECTED around the incident. This
is not an independent HCI continuity measurement. Both observed host and Navdy
USB suspended-time counters were zero; host USB power/control was `on`.
The Navdy kernel log covering the initial failure was lost in an earlier reboot.

`EPROTO` alone cannot distinguish cabling, adapters, peripheral firmware or host
controller faults. See the [Linux USB error documentation](https://docs.kernel.org/driver-api/usb/error-codes.html).
No claim is made that the competing ADB server caused the first USB error, or
that software has eliminated the unresolved physical/driver failure.

## Changes

- Query existing 5038/5037 servers through the ADB smart socket without starting
  another server. Reuse the server that verifies the installed Navdy HUD package.
  Unknown server state never authorizes a competing server or a USB reset.
- For one previously verified, exact Navdy USB device that remains offline,
  attempt a targeted ADB reconnect after 10 seconds. After 30 seconds, permit a
  device-only reset only with fresh ignition-OFF/offroad evidence. No controller,
  Panda, Bluetooth, phone pairing, app, OS or vehicle-control reset is performed.
- Recheck ADB ownership/state and USB identity immediately before recovery. The
  reset child independently checks fresh device/panda messages and `IsOnroad=0`,
  including after opening the exact character device. Unknown or empty panda
  data does not authorize recovery. Online/unauthorized/ambiguous targets fail
  closed.
- Retain verified identity and budget across bridge restarts in boot-scoped
  state. At most two reset attempts per boot, separated by at least five minutes.
  Failed attempts count. Invalid/symlink budget files fail closed.
- Background USB probes use their own subscriber, a serialized recovery monitor
  and a bounded diagnostic queue. They do not block healthy fallback display
  traffic or consume its main-thread messages. Closing a probe cancels recovery,
  including a launched reset child through an inherited cancellation pipe.
  IPC failure denies recovery rather than interrupting the bridge.
- Save bounded USB/kernel/ADB failure evidence off the display thread, with a
  60-second collection limit and at most four 256 KiB local files. Preserve the
  original direct-connect failure before any Jetson fallback error replaces it.

Wired/Bluetooth selection, phone auto-reconnection, installed Navdy APK,
navigation freshness and vehicle control are unchanged.

## Verification

Guarded deployment and bounded installed soak are recorded separately below.
Final local focused suite: 69 passed. Final native full suite: **1453 passed in
104.69 seconds**, isolated from production publication/settings. The initial native suite before the
additional evidence collector passed 1431 tests. Its follow-up caught a collector
constructor assuming a diagnostic journal was present; that optional-journal
dependency was removed before deployment. This failed revision was not installed.

The real device's new read-only guard confirms fresh OFF/one recognized Panda,
reuses port 5038 and rejects a reset of the healthy online device. Native evidence
collection writes 64 bounded, redacted rows (11425 bytes) without disrupting the
existing link. Independent source/mock review confirmed the final cancellation,
IPC-failure and descriptor-cleanup corrections with no remaining blocking findings.
An early standalone-file helper probe lacked the repository import path; the
production helper now uses module invocation, and native candidate entry was
verified separately. These tests are not a reproduction of the original EPROTO.

First-incident evidence, raw device logs and test receipts remain private, outside Git.
