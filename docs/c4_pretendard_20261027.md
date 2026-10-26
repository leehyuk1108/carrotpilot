# C4 Pretendard typography

Scope: C4 (`BIG_UI=False`) default UI fonts on WIP-FR, based on 4ae0bee6.
The user expanded the numeric-font request to Korean and English as well.

## Implementation

- All C4 text roles use the existing Pretendard SemiBold source, verified
  OS/2 weight 600. Sizes, positions, tracking, and event/timer conditions stay
  unchanged. This is not a promise of pixel-identical Wayon rasterization.
- Keep the 200px Latin/numeric atlas. Generate a separate 48px full-Hangul
  atlas from that same TTF; SCons declares both additional outputs. No extra
  font binary or manual generated-asset installation is required.
- Fallback checks actual nonempty glyph metrics, not just codepoint presence.
  Unsupported jamo/symbols use unifont; the same font selection is used for
  measuring, wrapping, elision, and drawing. Existing emoji rendering remains.
- Missing generated Korean atlas falls back to the existing TTF at 48px,
  not a 200px all-Hangul allocation. Font coverage is cached and cleared on
  shutdown. Font generation releases its temporary native allocations.
- C3/BIG_UI retains its prior fonts and locale policy. External HUD font
  loaders, vehicle control, DM, Navdy/BT, and settings were not changed.

## Local Verification

- 522 tests and 1212 subtests passed across font, alert, timer, offroad,
  new-UI, BSM/lead/DM presentation, bootstrap, and text-offset suites.
- The broader raygui scan still fails on the pre-existing `GuiTextAlignment`
  reference in `mici/layouts/welcome.py:21`, verified at unchanged HEAD.
  This one test is explicitly excluded from the passing aggregate above.
- Native CPU atlas generation verified all 11172 modern Hangul syllables,
  common compatibility jamo, unifont jamo/symbol fallback, Korean translation
  and event characters, and unusually long timers within BSM side margins.
- Generated atlas dimensions: Latin 4096x2048, Korean 8192x4096, unifont
  4096x4096. The Korean atlas has half the pixels of the old 8192x8192 KaiGen
  atlas; this is not a measured whole-device memory or FPS claim.
- Native desktop font generation uses a test-only adapter for its six-argument
  LoadFontData ABI; production retains the target's seven-argument API.
- New font tests pass Ruff. Existing whole-file lint findings elsewhere in
  application/alert tests are unchanged. `git diff --check` passes.

No push, device installation, reboot, live C4 screenshot, or road test was
performed in this request. The atlas specimen was rendered and inspected
locally; it is not a device capture. A future deployment must build the new
atlas and verify the installed assets under the normal parked-device guard.
