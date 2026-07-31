# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use semantic versioning where practical.

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
