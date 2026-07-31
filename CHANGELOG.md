# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use semantic versioning where practical.

## [2.5.7] - 2026-08-01

### Fixed

- Make automatic reasoning fallback take over Codex Desktop upstream-disconnect failures instead of requiring a manual switch from high to medium.
- Prefer the active Codex state database at `~/.codex/state_5.sqlite`, retain the old path as a fallback, and throttle repeated database warnings.

### Verified

- 60 unit tests pass with the macOS system Python.
- The live Codex state database resolves correctly and returns the current thread's actual reasoning effort.

## [2.5.6] - 2026-08-01

### Fixed

- Restart the foreground app after an in-place rebuild when it was already open, preventing a stale process from continuing to show the old interface.
- Confirmed the sidebar selection no longer displays the macOS blue focus ring.

### Verified

- 50 real Accessibility navigation clicks across all five sidebar pages.
- 50 process-level macOS drills plus the full unit-test suite.

## [2.5.5] - 2026-08-01

### Added

- Automatic reasoning-effort fallback when the selected model pool is at capacity.
- Default fallback path from high to medium to low, with a configurable floor.
- Periodic attempts to restore the original reasoning effort without changing global Codex settings.
- User-facing switches for automatic fallback and restoration.

### Changed

- Clarified that daily cost starts at local midnight and refreshes every 30 seconds.
- Removed the macOS system focus ring from sidebar navigation while retaining the selected state.
- Generalized the example configuration so it no longer contains a developer-specific home path.

### Verified

- 55 unit tests.
- 50 process-level macOS drills covering recovery, failover, provider snapshots, state recovery, sleep checks, and app launch.

## [2.5.4] - 2026-07-31

### Added

- CC Switch Codex and Claude Desktop provider monitoring.
- Real multiplier, balance, health reason, and CNY cost reconciliation.
- OpenResty HTML 400 temporary P1 bypass with order preservation.
- Automatic repair of the Codex route back through CC Switch failover.
