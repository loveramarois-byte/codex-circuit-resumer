# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases use semantic versioning where practical.

## [2.8.0] - 2026-08-02

### Added

- Track auto-resume launches, confirmed auto-resume successes, and manual recoveries separately.
- Add a per-launch attribution marker so a user's manual Continue action is never credited as an automatic success.
- Expedite pending retries after CC Switch records a successful Codex request, while preserving explicit upstream `Retry-After` delays.

### Changed

- Show “自动成功 / 已发起” and “你手动接回” as separate macOS and Windows status metrics.
- Preserve pre-2.8 launch history separately and start the precise attribution counters at zero after upgrade.
- Rename launch events to “已发起自动续接，等待确认结果” until the resumed turn actually completes.
- Keep same-turn self-completions separate so they are never reported as a user's manual Continue action.
- Keep daemon health separate from tasks that need manual login or token repair.
- Store future macOS app upgrade backups as compressed archives, retain the latest three, and restore the previous app automatically if a build fails.
- Only wake a pending retry when the observed successful request is newer than that task's failure.

### Verified

- 95 unit tests plus a macOS build-rollback integration test.
- 50 process-level macOS drills.
- macOS app build and Windows Python/static syntax checks.

## [2.7.1] - 2026-08-02

### Fixed

- Allow due retries to use a free resume slot while another task is already being resumed.
- Continue scanning Codex rollout errors during unrelated resume work, so simultaneous capacity/503 failures are not missed.
- Resume genuinely stalled active turns instead of delaying them forever; active turns that are still writing logs remain protected.

### Changed

- Raise the default unattended resume concurrency from 1 to 2 for low-rate CC Switch failover setups.

### Verified

- 78 unit tests.
- 50 process-level macOS drills.

## [2.7.0] - 2026-08-02

### Added

- Windows 10/11 x64 图形界面、独立后台 EXE、双击安装入口、桌面快捷方式和当前用户计划任务开机自启。
- Windows 可重复安装升级、停止与卸载脚本，不要求管理员权限。
- 双平台 GitHub Release 工作流，同步发布 macOS arm64 与 Windows x64 压缩包及 SHA-256。

### Changed

- 共享监控核心支持 Windows 应用数据目录、Codex npm 路径、进程识别、管道等待与电源检查。
- 档位恢复目标按每个对话满载前实际选择记录，完整支持“极高 → 高 → 中 → 低”并恢复原档位。

### Verified

- Windows CI 覆盖单元测试、PSScriptAnalyzer、完整安装生命周期烟测与 Microsoft Defender 扫描。
- macOS CI 覆盖单元测试、Swift 类型检查、应用构建与签名检查；本地保留 50 次进程级操练脚本。

## [2.6.1] - 2026-08-01

- 修复桌面升档目标误取全局配置的问题：现在记住每个对话满载前实际选择的原档位，完整支持“极高 → 高 → 中 → 低”逐档降级并恢复极高。

### Fixed

- Preserve the original reasoning tier after a successful lower-tier recovery instead of discarding promotion state.
- Restore an idle Codex Desktop thread through the official `thread/settings/update` app-server method after the capacity cooldown.
- Detect a manual high-to-medium fallback that immediately follows a Desktop capacity failure and still schedule the safe promotion.
- Never change the effort of a turn that is already running; the restored tier applies to the next request.

### Verified

- Added regression coverage for pending promotion, active-turn protection, Desktop synchronization, and manual fallback discovery.

## [2.6.0] - 2026-08-01

### Added

- Persist overflow recovery candidates and process them in safe batches so large incidents cannot silently lose conversations.
- Discover Codex and CC Switch by bundle identifier before falling back to common install locations.
- Read older CC Switch provider schemas even when optional provider or health fields are absent.

### Changed

- Move periodic file reads and `launchctl` checks off the SwiftUI main thread, and tail only the recent event log window.
- Treat an explicit `1.0` request multiplier as a valid observed multiplier for cost reconciliation.
- Count deferred recovery candidates in the visible pending queue.

### Fixed

- Correct retry-history cleanup from two hours per configured hour to one hour per configured hour.

### Verified

- 63 unit tests, Swift type checking, app signing checks, and 50 process-level macOS drills.

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
