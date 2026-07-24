# Changelog

## 1.13.1 — 2026-07-24

### Changed

- Referer now has **자동 / 수동** modes next to the field, styled like existing secondary CTkButtons (accent when selected, #273450 when idle).
- Auto mode requests images normally, then on HTTP 403 quickly probes built-in platform Referers in parallel and caches the first match per host.
- Built-in auto platforms: BabeChat, Crack, Elyn, CAVEDUCK, EdenChat, LUNATALK, Teapot, ChuuChat, BoriChat.
- Manual mode uses the address typed in the Referer field.
- Favorites store the Referer mode; older favorites with a saved Referer open in manual mode.

## 1.13.0 — 2026-07-22

### Build handoff

- Added `BUILD_RELEASE.cmd` for a single-command Windows x64 release build.
- The build script now bootstraps `.venv-build`, installs dependencies, detects or installs Inno Setup 6, runs tests and compile checks, validates PE versions, and creates SHA-256/build metadata.
- Added Grok Build-only instructions that prohibit feature-code changes during packaging.


### Added

- Full mixed situation range parsing for expressions such as `01..50,s01..83`, including `s01..s83`, literals, zero padding, stable ordering, and duplicate removal.
- Automated stale `.part` cleanup for abandoned image and updater downloads.
- Windows CI that runs tests, builds the PyInstaller application, compiles the Inno Setup installer, verifies the output, and uploads `LumaFetch-Setup-1.13.0.exe`.
- Focused regression tests for mixed ranges, corrupted favorites, event-queue pressure, preview-cache lifecycle, worker cleanup, partial files, and release metadata.

### Fixed

- Repeated favorite switching could exhaust the bounded Tk event queue and stop all new preview images from appearing.
- Background workers could deadlock indefinitely while calling blocking `queue.put()` after the UI queue became full.
- Preview cache directories could be deleted while cancelled network/decode workers or the original-image viewer were still reading and writing them.
- A cancelled image transfer could leave `.*.part.<extension>` files behind.
- One invalid favorite entry could cause every otherwise valid favorite to disappear.
- Preview, viewer, update, cache-cleanup, and download threads were not centrally tracked or bounded during shutdown.
- Favorite action cards configured only three weighted columns while rendering four controls.
- Pending Tk redraw and polling callbacks could survive window teardown.

### Changed

- Extracted reusable range, storage, event-bus, worker-lifecycle, and preview-cache logic into the `lumafetch` package.
- Preview cache files are now written transactionally and published only after an atomic rename.
- Download configurations cache expanded character and situation lists instead of repeatedly reparsing them.
- UI event handling now drops stale/high-volume events under pressure while preserving completion and error signals.
- PyInstaller now embeds Windows file/product version `1.13.0.0`.
