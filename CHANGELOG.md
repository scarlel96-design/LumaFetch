# Changelog

## 1.14.0 — 2026-07-24

### Added

- Multi-outfit support: comma/ranges in the outfit field expand to separate jobs (`A1,E1,E2`).
- Clearer download filenames: `{character}_{outfit}_{situation}.ext`.
- Hard cap with a clear error when total job combinations exceed 100,000.

### Fixed

- Outfit values were treated as a single path segment (comma-joined), causing 404s on `{outfit}` templates.
- Auto-Referer now treats HTTP 401 like 403 and prefers real `image/*` 200 responses when probing platforms.
- Templates without `{outfit}`/`{pose}` no longer multiply jobs for unused multi-outfit lists.
- Range zero-padding: bare `0` no longer forces end-width padding (`a0..13` → `a0..a13`, not `a00..a13`).
- Favorite cover loading completeness (sampled probes, parallel loads, cache reuse, empty-slot markers).
- Original image viewer z-order behind preview gallery.
- Favorite name dialog placement and favorites list open hitch.

### Changed

- Version browser (upgrade / reinstall / downgrade) from the sidebar.
- Form placeholders clarify multi-value character/outfit/situation input.
- Favorites page size 20 with incremental card rendering and optional cover strip.

## 1.13.2 — 2026-07-24

### Added

- Version browser with scrollable release history for upgrade, reinstall, or downgrade.
- Favorite cover thumbnails (up to 4) with disk cache.
- Centered favorite-name dialog.

### Fixed

- Favorite cover partial loading.
- Preview viewer stacking and favorites performance.

## 1.13.1 — 2026-07-24

### Changed

- Referer auto/manual modes with built-in platform list for 403 recovery.

## 1.13.0 — 2026-07-22

### Added

- Mixed situation ranges, runtime package split, Windows release build pipeline, regression tests.
