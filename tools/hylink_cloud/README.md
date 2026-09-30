# Hylink key-reissue server patch

The comma-side implementation is in `openpilot/system/hylink`. The companion
Cloudflare changes are archived here at the owner's request; this directory is
not loaded by the comma and does not deploy anything.

- Source repository: `leehyuk1108/Wayon`.
- Tested server source baseline: `7faab119a1c079d74ddf1c59e9def4c9b4b4f29c`.
- Patch: `device-key-reissue.patch` (six server source, migration and test files).
- New endpoint: `POST /api/devices/reissue`.

The endpoint verifies a short-lived, operation-bound comma device signature,
atomically replaces only that device's key hash, invalidates its old push
bindings and relay sessions, and supports idempotent retries. Device IDs alone
are not authorization. Private keys and plaintext Wayon keys are not stored in
the cloud database. The comma API lookup is mocked in the automated tests;
real C3x/C4 identity verification still requires device validation.

## Apply and validate (server checkout only)

Check the current server revision first. Do not replace newer server changes
with this historical baseline. From the server repository root, run
`git apply --check /absolute/path/to/device-key-reissue.patch` before applying
the patch with `git apply`. A newer revision may require deliberate integration.

Run the `test_*.mjs` files in `cloudflare/wayon-cloud/` and its `remote/`
subdirectory. `test_device_reissue.mjs` requires Node 24 with `node:sqlite` and
uses only synthetic credentials, mocked comma responses and in-memory SQLite.

Production activation is a separate operation: apply the additive migration
`cloudflare/wayon-cloud/migrations/0010_device_key_reissues.sql` before deploying
the matching Worker, then update the comma branch. Never rotate real device
keys as part of deployment verification. Existing app bearer keys remain valid
until their user requests reissue; after reissue the user must paste the new
key into the app. Previously issued short-lived relay session tokens must be
requested again after deploying the key-bound session authentication change.

Publishing this archive does not mean the server has been deployed or an
affected C3x has been recovered.
