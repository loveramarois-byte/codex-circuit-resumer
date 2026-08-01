#!/bin/zsh
set -eu

LABEL="com.local.codex-circuit-resumer"
APP_HOME="$HOME/Library/Application Support/CodexCircuitResumer"
LAUNCH_AGENT="$HOME/Library/LaunchAgents/${LABEL}.plist"
SCRIPT_DIR="${0:A:h}"
GUI_DOMAIN="gui/$(id -u)"

# App bundle keeps daemon.py next to this script; repo layout keeps it under ../src.
if [[ -f "$SCRIPT_DIR/daemon.py" ]]; then
  RES_DIR="$SCRIPT_DIR"
  EXAMPLE_CONFIG="$SCRIPT_DIR/config.example.json"
elif [[ -f "$SCRIPT_DIR/../src/daemon.py" ]]; then
  RES_DIR="$SCRIPT_DIR/../src"
  EXAMPLE_CONFIG="$SCRIPT_DIR/../config.example.json"
else
  echo "找不到 daemon.py" >&2
  exit 1
fi

merge_config() {
  /usr/bin/python3 - "$APP_HOME/config.json" "$EXAMPLE_CONFIG" <<'PY'
import json, sys
from pathlib import Path

config_path = Path(sys.argv[1])
example_path = Path(sys.argv[2])
defaults = {}
if example_path.is_file():
    try:
        defaults = json.loads(example_path.read_text(encoding="utf-8"))
    except Exception:
        defaults = {}
current = {}
if config_path.is_file():
    try:
        current = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        current = {}
if not isinstance(current, dict):
    current = {}
if not isinstance(defaults, dict):
    defaults = {}
old_version = int(current.get("config_schema_version") or 1)
# Migrate only former generated defaults; preserve values the user customized.
if old_version < 3:
    migrations = {
        "heartbeat_stale_seconds": (15, 30),
        "provider_snapshot_seconds": (15, 30),
        "balance_probe_seconds": (900, 1800),
    }
    for key, (former, improved) in migrations.items():
        if current.get(key, former) == former:
            current[key] = improved
    current["retry_error_scan_seconds"] = int(current.get("retry_error_scan_seconds") or 10)
    current["state_checkpoint_seconds"] = int(current.get("state_checkpoint_seconds") or 10)
    current["config_schema_version"] = 3
if old_version < 4:
    current["codex_proxy_route_repair_enabled"] = bool(current.get("codex_proxy_route_repair_enabled", True))
    current["codex_proxy_route_check_seconds"] = int(current.get("codex_proxy_route_check_seconds") or 30)
    current["codex_proxy_route_retry_seconds"] = int(current.get("codex_proxy_route_retry_seconds") or 15)
    current["exchange_rate_probe_seconds"] = int(current.get("exchange_rate_probe_seconds") or 21600)
    current["exchange_rate_probes_enabled"] = bool(current.get("exchange_rate_probes_enabled", True))
    current["usd_cny_fallback_rate"] = float(current.get("usd_cny_fallback_rate") or 7.2)
    current["config_schema_version"] = 4
if old_version < 5:
    current["gateway_400_failover_bypass_enabled"] = bool(current.get("gateway_400_failover_bypass_enabled", True))
    current["gateway_400_failover_bypass_seconds"] = int(current.get("gateway_400_failover_bypass_seconds") or 900)
    current["config_schema_version"] = 5
if old_version < 6:
    current["capacity_reasoning_fallback_enabled"] = bool(current.get("capacity_reasoning_fallback_enabled", True))
    current["capacity_reasoning_minimum"] = str(current.get("capacity_reasoning_minimum") or "low")
    current["capacity_reasoning_promote_enabled"] = bool(current.get("capacity_reasoning_promote_enabled", True))
    current["capacity_reasoning_promote_after_seconds"] = int(current.get("capacity_reasoning_promote_after_seconds") or 900)
    current["config_schema_version"] = 6
if old_version < 7:
    legacy_state_db = str(Path.home() / ".codex" / "sqlite" / "state_5.sqlite")
    configured_state_db = str(current.get("codex_state_db") or "")
    if configured_state_db in {legacy_state_db, "~/.codex/sqlite/state_5.sqlite"}:
        current["codex_state_db"] = "~/.codex/state_5.sqlite"
    current["config_schema_version"] = 7
if old_version < 8:
    current["capacity_reasoning_desktop_sync_enabled"] = bool(current.get("capacity_reasoning_desktop_sync_enabled", True))
    current["config_schema_version"] = 8
merged = dict(defaults)
merged.update(current)
config_path.parent.mkdir(parents=True, exist_ok=True)
tmp = config_path.with_suffix(".tmp")
tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.replace(config_path)
config_path.chmod(0o600)
PY
}

install_files() {
  mkdir -p "$APP_HOME/logs/runs" "$HOME/Library/LaunchAgents"
  cp "$RES_DIR/daemon.py" "$APP_HOME/daemon.py"
  chmod 755 "$APP_HOME/daemon.py"
  if [[ -f "$EXAMPLE_CONFIG" ]]; then
    cp "$EXAMPLE_CONFIG" "$APP_HOME/config.example.json"
  fi
  if [[ ! -f "$APP_HOME/config.json" && -f "$EXAMPLE_CONFIG" ]]; then
    cp "$EXAMPLE_CONFIG" "$APP_HOME/config.json"
    chmod 600 "$APP_HOME/config.json"
  fi
  merge_config
  cat > "$LAUNCH_AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-s</string>
    <string>/usr/bin/python3</string>
    <string>${APP_HOME}/daemon.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>${HOME}/.local/bin:${HOME}/.hermes/node/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>ProcessType</key><string>Background</string>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>${APP_HOME}/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>${APP_HOME}/logs/launchd.err.log</string>
</dict>
</plist>
PLIST
  plutil -lint "$LAUNCH_AGENT" >/dev/null
}

start_watcher() {
  install_files
  rm -f "$APP_HOME/PAUSED"
  launchctl bootout "$GUI_DOMAIN" "$LAUNCH_AGENT" >/dev/null 2>&1 || true
  launchctl bootstrap "$GUI_DOMAIN" "$LAUNCH_AGENT"
  launchctl kickstart -k "$GUI_DOMAIN/$LABEL"
  echo "已启动。守望器会自动续接、整夜退避重试、记录心跳，并把登录/审批类故障标为需人工处理。"
}

pause_watcher() {
  mkdir -p "$APP_HOME"
  touch "$APP_HOME/PAUSED"
  echo "已暂停。后台进程保留，但不会扫描或续接任务。"
}

status_watcher() {
  if [[ ! -f "$APP_HOME/daemon.py" ]]; then
    echo "尚未安装。请选择“启动守望器”。"
    return
  fi
  local loaded="未运行"
  if launchctl print "$GUI_DOMAIN/$LABEL" >/dev/null 2>&1; then
    loaded="运行中"
  fi
  local paused="否"
  [[ -f "$APP_HOME/PAUSED" ]] && paused="是"
  local state
  state=$(/usr/bin/python3 "$APP_HOME/daemon.py" --status 2>/dev/null || echo '{}')
  local summary
  summary=$(STATE_JSON="$state" /usr/bin/python3 - <<'PY'
import json, os, time
try:
    s = json.loads(os.environ.get("STATE_JSON", "{}"))
except Exception:
    s = {}
phase_names = {
    "idle": "等待熔断事件",
    "circuit_open": "线路熔断中",
    "recovery_grace": "线路已恢复，等待稳定",
    "resuming": "正在续接任务",
}
stamp = s.get("last_event_at")
when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)) if stamp else "无"
scheduled = s.get("scheduled_retry_count")
if scheduled is None:
    scheduled = len(s.get("scheduled_retries") or [])
heartbeat_age = s.get("heartbeat_age_seconds")
heartbeat = "健康" if s.get("healthy") else "无心跳或需检查"
print("状态：{}\n后台健康：{}{}\n最后事件：{}\n事件时间：{}\n累计自动续接：{}\n正在续接：{}\n待重试任务：{}\n需人工处理：{}".format(
    phase_names.get(s.get("phase"), s.get("phase", "未知")),
    heartbeat,
    "（{} 秒前）".format(heartbeat_age) if heartbeat_age is not None else "",
    s.get("last_event", "无"),
    when,
    s.get("resume_count", 0),
    s.get("inflight_count", len(s.get("inflight") or {})),
    scheduled,
    s.get("blocked_count", len(s.get("blocked") or {})),
))
PY
)
  echo "LaunchAgent：${loaded}\n暂停：${paused}\n${summary}"
}

inspect_watcher() {
  install_files
  /usr/bin/python3 "$APP_HOME/daemon.py" --inspect
}

sleep_check() {
  install_files
  local payload
  payload=$(/usr/bin/python3 "$APP_HOME/daemon.py" --sleep-check)
  SLEEP_CHECK_JSON="$payload" /usr/bin/python3 - <<'PY'
import json, os
try:
    result = json.loads(os.environ.get("SLEEP_CHECK_JSON", "{}"))
except Exception:
    result = {}
ready = bool(result.get("ready"))
print("睡前检查：{}".format("可以守候" if ready else "发现需要处理的项目"))
for item in result.get("checks") or []:
    marker = "✓" if item.get("ok") else "!"
    print("{} {}：{}".format(marker, item.get("name", "检查项"), item.get("detail", "")))
if ready:
    print("保持接电和开盖；关闭本窗口不影响后台守望。")
else:
    print("处理标记为 ! 的项目后，再开始整夜任务。")
PY
}

providers_snapshot() {
  install_files
  local probe_arg=()
  if [[ "${1:-}" == "--probe-balances" ]]; then
    probe_arg=(--probe-balances)
  elif [[ "${1:-}" == "--probe-billing" ]]; then
    probe_arg=(--probe-billing)
  elif [[ "${1:-}" == "--probe-all" ]]; then
    probe_arg=(--probe-balances --probe-billing --probe-exchange)
  elif [[ "${1:-}" == "--probe-exchange" ]]; then
    probe_arg=(--probe-exchange)
  fi
  /usr/bin/python3 "$APP_HOME/daemon.py" --providers "${probe_arg[@]}"
}

open_logs() {
  mkdir -p "$APP_HOME/logs"
  open "$APP_HOME/logs"
  echo "已打开日志目录。"
}

uninstall_watcher() {
  launchctl bootout "$GUI_DOMAIN" "$LAUNCH_AGENT" >/dev/null 2>&1 || true
  rm -f "$LAUNCH_AGENT"
  local trash_target="$HOME/.Trash/CodexCircuitResumer-$(date +%Y%m%d-%H%M%S)"
  if [[ -d "$APP_HOME" ]]; then
    mv "$APP_HOME" "$trash_target"
    echo "已卸载。运行数据已移到废纸篓：${trash_target}"
  else
    echo "已卸载 LaunchAgent；没有发现运行数据。"
  fi
}

case "${1:-status}" in
  start|install) start_watcher ;;
  pause) pause_watcher ;;
  status) status_watcher ;;
  inspect) inspect_watcher ;;
  sleep-check) sleep_check ;;
  providers) providers_snapshot ;;
  probe-balances) providers_snapshot --probe-balances ;;
  probe-billing) providers_snapshot --probe-billing ;;
  probe-exchange) providers_snapshot --probe-exchange ;;
  probe-all) providers_snapshot --probe-all ;;
  logs) open_logs ;;
  uninstall) uninstall_watcher ;;
  *) echo "用法: $0 {start|pause|status|inspect|sleep-check|providers|probe-balances|probe-billing|probe-exchange|probe-all|logs|uninstall}" >&2; exit 2 ;;
esac
