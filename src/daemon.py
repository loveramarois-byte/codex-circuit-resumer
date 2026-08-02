#!/usr/bin/env python3
"""Resume interrupted Codex threads after CC Switch's circuit breaker recovers."""

import argparse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import glob
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import random
import re
import select
import signal
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen


IS_WINDOWS = os.name == "nt"
DEFAULT_APP_HOME = (
    Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "CodexCircuitResumer"
    if IS_WINDOWS
    else Path.home() / "Library" / "Application Support" / "CodexCircuitResumer"
)
APP_HOME = Path(os.environ.get("CODEX_CIRCUIT_RESUMER_HOME", str(DEFAULT_APP_HOME)))
CONFIG_PATH = APP_HOME / "config.json"
STATE_PATH = APP_HOME / "state.json"
PAUSE_PATH = APP_HOME / "PAUSED"
LOG_PATH = APP_HOME / "logs" / "watcher.log"
RUN_LOG_DIR = APP_HOME / "logs" / "runs"
PROVIDERS_PATH = APP_HOME / "providers.json"
EXCHANGE_RATE_CACHE_PATH = APP_HOME / "exchange-rate.json"
APP_VERSION = "2.8.1"
USER_AGENT = "CodexCircuitResumer/{}".format(APP_VERSION)

DEFAULT_RESUME_PROMPT = (
    "继续上一条用户请求中尚未完成的工作，直接动手；"
    "不要讨论熔断续聊软件本身，不要重复已经完成的内容。"
)


@contextmanager
def sqlite_connection(*args, **kwargs):
    connection = sqlite3.connect(*args, **kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()

DEFAULT_CONFIG = {
    "config_schema_version": 10,
    "cc_switch_log": str(Path.home() / ".cc-switch" / "logs" / "cc-switch.log"),
    "cc_switch_db": str(Path.home() / ".cc-switch" / "cc-switch.db"),
    "codex_state_db": str(Path.home() / ".codex" / "state_5.sqlite"),
    "codex_sessions_dir": str(Path.home() / ".codex" / "sessions"),
    "codex_config": str(Path.home() / ".codex" / "config.toml"),
    "codex_proxy_backup": str(Path.home() / ".codex" / "config.toml.before-codex-resumer"),
    "codex_binary": str(Path.home() / ".local" / "bin" / ("codex.exe" if IS_WINDOWS else "codex")),
    "prefer_native_codex": True,
    "extra_path": [
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / ".hermes" / "node" / "bin"),
        str(Path.home() / "AppData" / "Roaming" / "npm"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ],
    "recovery_grace_seconds": 20,
    "active_thread_idle_seconds": 240,
    "poll_seconds": 2,
    "candidate_window_padding_seconds": 8,
    "max_candidates_per_incident": 8,
    "max_parallel_resumes": 2,
    "resume_prompt": DEFAULT_RESUME_PROMPT,
    "notify": True,
    "model_retry_enabled": True,
    "model_retry_until_success": True,
    "model_retry_base_seconds": 60,
    "model_retry_max_seconds": 900,
    "model_retry_jitter_seconds": 15,
    "model_retry_respect_server_hint": True,
    "model_retry_server_hint_max_seconds": 3600,
    "model_retry_max_attempts": 50,
    "model_retry_watch_hours": 24,
    "retry_recovery_probe_seconds": 15,
    "retry_recovery_grace_seconds": 20,
    "capacity_reasoning_fallback_enabled": True,
    "capacity_reasoning_minimum": "low",
    "capacity_reasoning_promote_enabled": True,
    "capacity_reasoning_promote_after_seconds": 900,
    "capacity_reasoning_desktop_sync_enabled": True,
    "retry_error_scan_seconds": 10,
    "resume_exit_grace_seconds": 10,
    "launcher_unknown_failure_limit": 3,
    "circuit_reconcile_seconds": 30,
    "heartbeat_stale_seconds": 30,
    "state_checkpoint_seconds": 10,
    "run_log_retention_days": 7,
    "run_log_max_files": 40,
    "provider_snapshot_seconds": 30,
    "balance_probe_seconds": 1800,
    "balance_probes_enabled": True,
    "billing_probe_seconds": 300,
    "billing_probes_enabled": True,
    "codex_proxy_route_repair_enabled": True,
    "codex_proxy_route_check_seconds": 30,
    "codex_proxy_route_retry_seconds": 15,
    "exchange_rate_probe_seconds": 21600,
    "exchange_rate_probes_enabled": True,
    "usd_cny_fallback_rate": 7.2,
    "gateway_400_failover_bypass_enabled": True,
    "gateway_400_failover_bypass_seconds": 900,
}

_WARNING_THROTTLE = {}

REASONING_EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh")
REASONING_EFFORT_LABELS = {
    "minimal": "极低",
    "low": "轻度",
    "medium": "中",
    "high": "高",
    "xhigh": "极高",
}

OPEN_PATTERNS = (
    re.compile(r"熔断器\s+\w+\s*→\s*Open", re.IGNORECASE),
    re.compile(r"circuit\s+breaker.*(?:to|->|→)\s*open", re.IGNORECASE),
)
CLOSED_PATTERNS = (
    re.compile(r"熔断器\s+\w+\s*→\s*Closed", re.IGNORECASE),
    re.compile(r"熔断器.*恢复正常"),
    re.compile(r"circuit\s+breaker.*(?:to|->|→)\s*closed", re.IGNORECASE),
)
LOG_TIME_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\]\[(\d{2}:\d{2}:\d{2})\]")
THREAD_ID_RE = re.compile(r"^01[0-9a-f]{2}[0-9a-f-]{32}$", re.IGNORECASE)

RETRYABLE_ERROR_WORDS = (
    "server_overloaded",
    "rate_limit",
    "capacity",
    "temporarily unavailable",
    "timeout",
    "timed out",
    "upstream",
    "connection",
    "network",
    "stream disconnected",
    "connection reset",
    "broken pipe",
    "dns",
    "tls",
    "502",
    "503",
    "504",
    "429",
    "insufficient_balance",
    "insufficient balance",
    "insufficient account balance",
    "余额不足",
    "熔断",
    "超时",
)

BLOCKING_ERROR_PATTERNS = (
    ("需要重新登录 Codex", ("not logged in", "login required", "authentication required", "token expired")),
    ("鉴权失败", ("unauthorized", "authentication failed", "invalid token", "401")),
    ("需要人工批准", ("approval required", "requires approval", "permission request", "approval was denied", "cannot prompt for approval")),
    ("权限或沙箱阻止", ("permission denied", "sandbox denied", "operation not permitted")),
    ("请求配置错误", ("invalid request", "bad_request", "unknown option", "unexpected argument", "failed to load configuration")),
    ("模型配置不兼容", ("model not found", "unsupported model", "invalid model")),
    ("MCP 启动失败", ("mcp startup failed", "failed to start mcp", "mcp handshake failed")),
    ("磁盘空间不足", ("no space left on device", "disk quota exceeded")),
)


def ensure_dirs():
    APP_HOME.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging():
    ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[RotatingFileHandler(str(LOG_PATH), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")],
    )


def migrate_user_config(user_config):
    current = dict(user_config)
    try:
        old_version = max(1, int(current.get("config_schema_version") or 1))
    except (TypeError, ValueError, OverflowError):
        old_version = 1
    if old_version < 9:
        if current.get("max_parallel_resumes", 1) == 1:
            current["max_parallel_resumes"] = 2
        current["retry_recovery_probe_seconds"] = nonnegative_int(
            current.get("retry_recovery_probe_seconds"), 15
        ) or 15
        current["retry_recovery_grace_seconds"] = nonnegative_int(
            current.get("retry_recovery_grace_seconds"), 20
        ) or 20
        current["config_schema_version"] = 9
    if old_version < 10:
        old_prompt = str(current.get("resume_prompt") or "").strip()
        legacy_prompt = "继续。上次因中转站熔断或上游临时故障中断，请从中断处继续，不要重复已经完成的工作。"
        if not old_prompt or old_prompt == legacy_prompt:
            current["resume_prompt"] = DEFAULT_RESUME_PROMPT
        current["config_schema_version"] = 10
    return current


def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as handle:
            user_config = json.load(handle)
        if isinstance(user_config, dict):
            migrated = migrate_user_config(user_config)
            config.update(migrated)
            if migrated != user_config:
                ensure_dirs()
                atomic_write_json(CONFIG_PATH, migrated)
                if not IS_WINDOWS:
                    CONFIG_PATH.chmod(0o600)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logging.error("cannot read config %s: %s", CONFIG_PATH, exc)
    return config


def initial_state():
    return {
        "version": 2,
        "phase": "idle",
        "log_inode": None,
        "log_offset": 0,
        "incident": None,
        "queue": [],
        "candidate_backlog": [],
        "resumed_turns": {},
        "last_event": "等待 CC Switch 熔断事件",
        "last_event_at": int(time.time()),
        "resume_count": 0,
        "resume_success_count": 0,
        "manual_recovery_count": 0,
        "legacy_resume_count": 0,
        "resume_metrics_started_at": int(time.time()),
        "recovery_attributions": {},
        "last_success_at": 0,
        "last_success_title": "",
        "last_manual_recovery_at": 0,
        "last_manual_recovery_title": "",
        "scheduled_retries": [],
        "retry_attempts": {},
        "reasoning_efforts": {},
        "reasoning_originals": {},
        "reasoning_last_capacity_at": {},
        "reasoning_restore_pending": {},
        "reasoning_restore_last_scan": {},
        "retry_scan_cursor": None,
        "watch_started_at": int(time.time()),
        "last_retry_scan_at": 0,
        "last_retry_recovery_check_at": 0,
        "last_error_scan_at": 0,
        "last_provider_snapshot_at": 0,
        "last_balance_probe_at": 0,
        "last_billing_probe_at": 0,
        "last_exchange_rate_probe_at": 0,
        "balance_cache": {},
        "billing_cache": {},
        "exchange_rate_cache": {},
        "inflight": {},
        "blocked": {},
        "launcher_failures": {},
        "heartbeat_at": 0,
        "last_tick_at": 0,
        "last_tick_duration_ms": 0,
        "tick_failures_total": 0,
        "last_internal_error": "",
        "last_cleanup_at": 0,
        "state_recovered_at": 0,
        "state_recovery_error": "",
        "proxy_route_status": "unchecked",
        "proxy_route_detail": "尚未检查 Codex 是否经过 CC Switch",
        "proxy_route_checked_at": 0,
        "proxy_route_retry_at": 0,
        "temporary_failover_bypasses": [],
    }


def read_json_dict(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        return None


def state_backup_path():
    return STATE_PATH.with_name("state.backup.json")


def nonnegative_int(value, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def load_state():
    primary_existed = STATE_PATH.exists()
    backup_existed = state_backup_path().exists()
    state = read_json_dict(STATE_PATH)
    recovered = False
    if state is None:
        state = read_json_dict(state_backup_path())
        recovered = state is not None
    base = initial_state()
    if state is not None:
        base.update(state)
        if "resume_metrics_started_at" not in state:
            base["legacy_resume_count"] = nonnegative_int(state.get("resume_count"))
            base["resume_count"] = 0
            base["resume_success_count"] = 0
            base["manual_recovery_count"] = 0
            base["resume_metrics_started_at"] = int(time.time())
    base["version"] = 2
    for key in ("resumed_turns", "retry_attempts", "reasoning_efforts", "reasoning_originals", "reasoning_last_capacity_at", "reasoning_restore_pending", "reasoning_restore_last_scan", "balance_cache", "billing_cache", "exchange_rate_cache", "inflight", "blocked", "launcher_failures", "recovery_attributions"):
        if not isinstance(base.get(key), dict):
            base[key] = {}
    for key in ("queue", "candidate_backlog", "scheduled_retries", "temporary_failover_bypasses"):
        if not isinstance(base.get(key), list):
            base[key] = []
    if base.get("phase") not in {"idle", "circuit_open", "recovery_grace", "resuming"}:
        base["phase"] = "idle"
    if recovered:
        base["last_event"] = "状态文件损坏，已从安全备份恢复"
        base["last_event_at"] = int(time.time())
        base["state_recovered_at"] = int(time.time())
        base["state_recovery_error"] = ""
        logging.warning("recovered state from %s", state_backup_path())
    elif primary_existed and state is None:
        base["last_event"] = "状态文件损坏且没有可用备份，已进入安全空闲状态"
        base["last_event_at"] = int(time.time())
        base["state_recovery_error"] = "主状态与备份均无法解析" if backup_existed else "主状态无法解析且备份不存在"
        logging.error(base["state_recovery_error"])
    return base


def atomic_write_json(path, value):
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temp_path), str(path))


def save_state(state):
    ensure_dirs()
    state["version"] = 2
    atomic_write_json(STATE_PATH, state)
    backup = state_backup_path()
    try:
        if not backup.exists() or time.time() - backup.stat().st_mtime >= 60:
            atomic_write_json(backup, state)
    except OSError as exc:
        logging.warning("cannot refresh state backup: %s", exc)


def tail_text(path, max_bytes=64 * 1024):
    try:
        with Path(path).open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - max_bytes))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def retryable_gateway_error(text):
    """Recognise an upstream gateway's HTML 400 without retrying client JSON 400s."""
    lowered = str(text or "").lower()
    has_400 = bool(re.search(r"\b400\s+bad\s+request\b", lowered))
    has_html = "<html" in lowered or "&lt;html" in lowered
    has_gateway_signature = "openresty" in lowered or "nginx" in lowered
    return has_400 and has_html and has_gateway_signature


def retryable_text(text):
    lowered = (text or "").lower()
    return any(word in lowered for word in RETRYABLE_ERROR_WORDS) or retryable_gateway_error(lowered)


def temporarily_bypass_first_codex_provider(config, state, now=None, reason="HTML 网关 400"):
    """Temporarily remove P1 from CC Switch's queue while preserving its order.

    A durable marker is saved before the database mutation.  If the daemon is
    killed between those operations, startup recovery can safely put P1 back.
    """
    if not config.get("gateway_400_failover_bypass_enabled", True):
        return None
    now = int(now if now is not None else time.time())
    active = state.setdefault("temporary_failover_bypasses", [])
    if active:
        return active[0]
    db_path = config.get("cc_switch_db")
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        with sqlite_connection(str(db_path), timeout=3.0) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(providers)").fetchall()}
            required = {"id", "app_type", "name", "in_failover_queue", "sort_index"}
            if not required.issubset(columns):
                return None
            rows = db.execute(
                """
                SELECT id,name,sort_index FROM providers
                WHERE app_type='codex' AND in_failover_queue=1
                ORDER BY COALESCE(sort_index,999999),id
                """
            ).fetchall()
            if len(rows) < 2:
                return None
            provider_id, provider_name, sort_index = rows[0]
            marker = {
                "provider_id": provider_id,
                "provider_name": provider_name,
                "app_type": "codex",
                "sort_index": sort_index,
                "original_in_failover_queue": True,
                "bypassed_at": now,
                "restore_at": now + max(30, int(config.get("gateway_400_failover_bypass_seconds", 900))),
                "reason": str(reason or "HTML 网关 400")[:180],
            }
            active.append(marker)
            save_state(state)
            cursor = db.execute(
                "UPDATE providers SET in_failover_queue=0 "
                "WHERE id=? AND app_type='codex' AND in_failover_queue=1",
                (provider_id,),
            )
            if cursor.rowcount != 1:
                active.remove(marker)
                save_state(state)
                return None
            db.commit()
            logging.warning("临时避开 CC Switch P1：%s（不改变原排序）", provider_name)
            return marker
    except sqlite3.Error as exc:
        logging.warning("cannot temporarily bypass CC Switch P1: %s", exc)
        # If the marker was persisted but the mutation failed, restoring is safe.
        restore_temporary_failover_bypasses(config, state)
        return None


def restore_temporary_failover_bypasses(config, state, provider_id=None, now=None, expired_only=False):
    entries = state.setdefault("temporary_failover_bypasses", [])
    if not entries:
        return False
    now = int(now if now is not None else time.time())
    targets = [
        item for item in entries
        if (provider_id is None or item.get("provider_id") == provider_id)
        and (not expired_only or int(item.get("restore_at") or 0) <= now)
    ]
    if not targets:
        return False
    db_path = config.get("cc_switch_db")
    if not db_path or not os.path.exists(db_path):
        return False
    try:
        with sqlite_connection(str(db_path), timeout=3.0) as db:
            for item in targets:
                if item.get("original_in_failover_queue"):
                    db.execute(
                        "UPDATE providers SET in_failover_queue=1 WHERE id=? AND app_type=?",
                        (item.get("provider_id"), item.get("app_type") or "codex"),
                    )
            db.commit()
    except sqlite3.Error as exc:
        logging.warning("cannot restore temporary CC Switch bypass: %s", exc)
        return False
    target_ids = {id(item) for item in targets}
    state["temporary_failover_bypasses"] = [item for item in entries if id(item) not in target_ids]
    save_state(state)
    for item in targets:
        logging.info("已恢复 CC Switch 原 P1：%s", item.get("provider_name") or item.get("provider_id"))
    return True


def _toml_string(value):
    return json.dumps(str(value), ensure_ascii=False)


def _toml_value(line, key):
    match = re.match(r"^\s*{}\s*=\s*(\"(?:[^\"\\]|\\.)*\")".format(re.escape(key)), line)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except (ValueError, TypeError):
        return None


def _active_codex_provider(text):
    in_section = False
    for line in str(text or "").splitlines():
        if re.match(r"^\s*\[", line):
            in_section = True
        if not in_section:
            value = _toml_value(line, "model_provider")
            if value:
                return value
    return None


def _codex_provider_section(lines, provider):
    header = re.compile(
        r"^\s*\[\s*model_providers\.(?:\"([^\"]+)\"|([A-Za-z0-9_-]+))\s*\]\s*(?:#.*)?$"
    )
    start = None
    for index, line in enumerate(lines):
        match = header.match(line)
        if match and (match.group(1) or match.group(2)) == provider:
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"^\s*\[", lines[index]):
            end = index
            break
    return start, end


def read_codex_active_route_text(text):
    """Read only non-secret route facts from Codex TOML."""
    provider = _active_codex_provider(text)
    result = {
        "model_provider": provider,
        "base_url": None,
        "wire_api": None,
        "proxy_managed": False,
    }
    if not provider:
        return result
    lines = str(text or "").splitlines()
    bounds = _codex_provider_section(lines, provider)
    if not bounds:
        return result
    start, end = bounds
    for line in lines[start + 1 : end]:
        base_url = _toml_value(line, "base_url")
        wire_api = _toml_value(line, "wire_api")
        token = _toml_value(line, "experimental_bearer_token")
        if base_url is not None:
            result["base_url"] = base_url.rstrip("/")
        if wire_api is not None:
            result["wire_api"] = wire_api
        if token is not None:
            result["proxy_managed"] = token == "PROXY_MANAGED"
    return result


def patch_codex_proxy_route_text(text, proxy_base_url):
    """Mirror CC Switch 3.19's Codex takeover fields in the active provider only."""
    provider = _active_codex_provider(text)
    if not provider:
        raise ValueError("Codex 配置缺少 model_provider")
    had_trailing_newline = str(text).endswith("\n")
    lines = str(text).splitlines()
    if not _codex_provider_section(lines, provider):
        raise ValueError("Codex 当前 model_provider 没有对应配置段")

    for key, value in (
        ("base_url", str(proxy_base_url).rstrip("/")),
        ("wire_api", "responses"),
        ("experimental_bearer_token", "PROXY_MANAGED"),
    ):
        start, end = _codex_provider_section(lines, provider)
        replaced = False
        for index in range(start + 1, end):
            if re.match(r"^\s*{}\s*=".format(re.escape(key)), lines[index]):
                indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
                lines[index] = "{}{} = {}".format(indent, key, _toml_string(value))
                replaced = True
                break
        if not replaced:
            lines.insert(start + 1, "{} = {}".format(key, _toml_string(value)))

    output = "\n".join(lines)
    return output + ("\n" if had_trailing_newline else "")


def cc_switch_codex_proxy_settings(db_path):
    if not db_path or not os.path.exists(db_path):
        return None
    uri = "file:{}?mode=ro".format(db_path)
    try:
        with sqlite_connection(uri, uri=True, timeout=2.0) as db:
            row = db.execute(
                """SELECT proxy_enabled,listen_address,listen_port,enabled,auto_failover_enabled
                   FROM proxy_config WHERE app_type='codex'"""
            ).fetchone()
    except sqlite3.Error as exc:
        if "no such table" not in str(exc).lower():
            logging.warning("cannot read CC Switch Codex proxy settings: %s", exc)
        return None
    if not row:
        return None
    host = str(row[1] or "127.0.0.1")
    if host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    return {
        "proxy_enabled": bool(row[0]),
        "host": host,
        "port": int(row[2] or 0),
        "takeover_enabled": bool(row[3]),
        "auto_failover_enabled": bool(row[4]),
    }


def local_proxy_listening(host, port, timeout=0.6):
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _atomic_write_private_text(path, text, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".codex-resumer.tmp")
    descriptor = os.open(str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(str(temp_path), mode)
        os.replace(str(temp_path), str(path))
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def ensure_codex_proxy_routing(config):
    """Repair a stale Codex live route only when CC Switch takeover + failover are enabled."""
    if not config.get("codex_proxy_route_repair_enabled", True):
        return {"status": "inactive", "detail": "自动接回 CC Switch 已关闭"}
    proxy = cc_switch_codex_proxy_settings(config.get("cc_switch_db"))
    if not proxy:
        return {"status": "inactive", "detail": "未读取到 CC Switch 的 Codex 代理设置"}
    if not (proxy["proxy_enabled"] and proxy["takeover_enabled"] and proxy["auto_failover_enabled"]):
        return {"status": "inactive", "detail": "CC Switch 的 Codex 接管或故障转移未开启"}
    if not local_proxy_listening(proxy["host"], proxy["port"]):
        return {"status": "unavailable", "detail": "CC Switch 本地代理尚未监听"}

    host = proxy["host"]
    url_host = "[{}]".format(host) if ":" in host and not host.startswith("[") else host
    expected = "http://{}:{}/v1".format(url_host, proxy["port"])
    config_path = Path(config.get("codex_config") or DEFAULT_CONFIG["codex_config"])
    try:
        original = config_path.read_text(encoding="utf-8")
        route = read_codex_active_route_text(original)
        if (
            (route.get("base_url") or "").rstrip("/") == expected.rstrip("/")
            and route.get("wire_api") == "responses"
            and route.get("proxy_managed")
        ):
            return {"status": "ready", "detail": "Codex 已经过 CC Switch 故障转移", "base_url": expected}

        patched = patch_codex_proxy_route_text(original, expected)
        backup_path = Path(config.get("codex_proxy_backup") or DEFAULT_CONFIG["codex_proxy_backup"])
        if not backup_path.exists():
            _atomic_write_private_text(backup_path, original, 0o600)
        mode = config_path.stat().st_mode & 0o777
        _atomic_write_private_text(config_path, patched, mode or 0o600)
        verified = read_codex_active_route_text(config_path.read_text(encoding="utf-8"))
        if (
            (verified.get("base_url") or "").rstrip("/") != expected.rstrip("/")
            or verified.get("wire_api") != "responses"
            or not verified.get("proxy_managed")
        ):
            _atomic_write_private_text(config_path, original, mode or 0o600)
            return {"status": "error", "detail": "Codex 路由校验失败，已恢复原配置"}
        return {"status": "repaired", "detail": "Codex 已自动接回 CC Switch 故障转移", "base_url": expected}
    except (OSError, ValueError) as exc:
        return {"status": "error", "detail": "Codex 路由修复失败：{}".format(redact_sensitive_text(exc))[:220]}


def retry_after_seconds(text, maximum=3600):
    """Extract a safe provider retry hint from text without trusting it blindly.

    Different OpenAI-compatible relays expose this as ``retry_after``,
    ``Retry-After`` or prose such as "try again in 2 minutes".  The value is
    only a scheduling hint: malformed and unreasonably large values are ignored.
    """
    value = str(text or "")
    patterns = (
        re.compile(r"(?i)retry[-_ ]?after\s*[:=]?\s*(\d{1,6})\s*(?:s|sec|second|seconds|秒)?"),
        re.compile(r"(?i)(?:try again|retry)\s+(?:in|after)\s*(\d{1,6})\s*(?:s|sec|second|seconds|秒)"),
        re.compile(r"(?i)(?:try again|retry)\s+(?:in|after)\s*(\d{1,4})\s*(?:m|min|minute|minutes|分钟)"),
    )
    for index, pattern in enumerate(patterns):
        match = pattern.search(value)
        if not match:
            continue
        seconds = int(match.group(1)) * (60 if index == 2 else 1)
        if 1 <= seconds <= max(1, int(maximum)):
            return seconds
    return 0


def blocking_reason(text):
    lowered = (text or "").lower()
    for reason, patterns in BLOCKING_ERROR_PATTERNS:
        if any(pattern in lowered for pattern in patterns):
            return reason
    return ""


def normalize_reasoning_effort(value):
    value = str(value or "").strip().lower()
    return value if value in REASONING_EFFORT_ORDER else None


def reasoning_effort_label(value):
    value = normalize_reasoning_effort(value)
    return REASONING_EFFORT_LABELS.get(value, value or "默认")


def lower_reasoning_effort(current, minimum="low"):
    """Return the next lower supported effort without crossing the floor."""
    current = normalize_reasoning_effort(current)
    minimum = normalize_reasoning_effort(minimum) or "low"
    if current is None:
        return None
    floor_index = REASONING_EFFORT_ORDER.index(minimum)
    current_index = REASONING_EFFORT_ORDER.index(current)
    return REASONING_EFFORT_ORDER[max(floor_index, current_index - 1)]


def is_model_capacity_error(text):
    lowered = str(text or "").lower()
    return (
        "selected model is at capacity" in lowered
        or "model is at capacity" in lowered
        or "server_overloaded" in lowered
        or "model overloaded" in lowered
        # Codex Desktop can hide the upstream capacity response behind this
        # stream error. Treat the exact wrapper as effort-sensitive so the
        # unattended resume tries the next lower reasoning tier.
        or "stream disconnected before completion: upstream request failed" in lowered
    )


def configured_reasoning_effort(config):
    """Read only the top-level effort from Codex config, never print secrets."""
    path = Path(config.get("codex_config") or DEFAULT_CONFIG["codex_config"])
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = _toml_value(line, "model_reasoning_effort")
            if value:
                return normalize_reasoning_effort(value)
    except OSError:
        pass
    return None


def reasoning_effort_is_lower(current, target):
    current = normalize_reasoning_effort(current)
    target = normalize_reasoning_effort(target)
    if not current or not target:
        return False
    return REASONING_EFFORT_ORDER.index(current) < REASONING_EFFORT_ORDER.index(target)


def event_epoch(event, fallback=0):
    payload = event.get("payload") or {}
    for key in ("completed_at", "started_at"):
        value = as_epoch(payload.get(key), None)
        if value is not None:
            return value
    timestamp = event.get("timestamp")
    if timestamp:
        try:
            return int(dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            pass
    return int(fallback or 0)


def reasoning_restore_hint(path, target_effort, now=None, max_age_seconds=86400):
    """Find a recent capacity error followed by a lower Desktop thread setting."""
    default_target = normalize_reasoning_effort(target_effort)
    now = int(now if now is not None else time.time())
    selected_effort = None
    restore_target = None
    last_capacity_at = 0
    fallback_effort = None
    fallback_at = 0
    for event in iter_tail_json_objects(Path(path)):
        if event.get("type") != "event_msg":
            continue
        payload = event.get("payload") or {}
        payload_type = payload.get("type")
        if payload_type == "task_complete":
            error = payload.get("error")
            error_text = json.dumps(error, ensure_ascii=False) if error else ""
            if is_model_capacity_error(error_text):
                capacity_target = selected_effort or default_target
                if not capacity_target:
                    continue
                if not restore_target or not reasoning_effort_is_lower(capacity_target, restore_target):
                    restore_target = capacity_target
                last_capacity_at = event_epoch(event, now)
                fallback_effort = None
                fallback_at = 0
        elif payload_type == "thread_settings_applied":
            effort = normalize_reasoning_effort(
                (payload.get("thread_settings") or {}).get("reasoning_effort")
            )
            if not effort:
                continue
            selected_effort = effort
            applied_at = event_epoch(event, last_capacity_at or now)
            # The database value checked by the caller is authoritative. A
            # target-tier event can be written while an older lower-tier turn
            # is still active; that turn may write its lower setting back to
            # the database without another rollout settings event. Keep the
            # fallback hint until the database itself reaches the target.
            if last_capacity_at and applied_at >= last_capacity_at and reasoning_effort_is_lower(
                effort, restore_target or default_target
            ):
                fallback_effort = effort
                fallback_at = applied_at
    if not last_capacity_at or not fallback_effort or not restore_target:
        return None
    if now - last_capacity_at > max(60, int(max_age_seconds)):
        return None
    return {
        "target_effort": restore_target,
        "fallback_effort": fallback_effort,
        "last_capacity_at": last_capacity_at,
        "fallback_at": fallback_at or last_capacity_at,
    }


def _app_server_rpc(process, request_id, method, params, timeout=15):
    message = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }
    process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    process.stdin.flush()
    deadline = time.monotonic() + max(1, timeout)
    if IS_WINDOWS:
        result = {"response": None}

        def read_response():
            while True:
                line = process.stdout.readline()
                if not line:
                    return
                try:
                    response = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if response.get("id") == request_id:
                    result["response"] = response
                    return

        reader = threading.Thread(target=read_response, daemon=True)
        reader.start()
        reader.join(max(1, timeout))
        response = result["response"]
        if response is None:
            raise TimeoutError("Codex app-server did not answer {}".format(method))
        error = response.get("error")
        if error:
            raise RuntimeError(str((error or {}).get("message") or error))
        return response.get("result") or {}
    else:
        lines = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if lines is None:
            ready, _, _ = select.select([process.stdout], [], [], min(0.5, max(0, deadline - time.monotonic())))
            if not ready:
                continue
            line = process.stdout.readline()
        else:
            line = lines.pop(0) if lines else ""
        if not line:
            break
        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            continue
        if response.get("id") != request_id:
            continue
        error = response.get("error")
        if error:
            raise RuntimeError(str((error or {}).get("message") or error))
        return response.get("result") or {}
    raise TimeoutError("Codex app-server did not answer {}".format(method))


def update_thread_reasoning_effort(config, thread_id, effort):
    """Persist an idle Codex Desktop thread effort through its official app-server API."""
    effort = normalize_reasoning_effort(effort)
    if not valid_thread_id(thread_id) or not effort:
        return {"ok": False, "detail": "线程或档位无效"}
    binary = resolve_codex_binary(config)
    if not binary or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        return {"ok": False, "detail": "找不到支持对话设置同步的 Codex CLI"}
    process = None
    try:
        process = subprocess.Popen(
            codex_command(binary, "app-server", "--stdio"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        _app_server_rpc(
            process,
            1,
            "initialize",
            {
                "clientInfo": {"name": "codex-circuit-resumer", "version": APP_VERSION},
                "capabilities": {"experimentalApi": True},
            },
        )
        _app_server_rpc(
            process,
            2,
            "thread/resume",
            {"threadId": thread_id, "excludeTurns": True},
        )
        _app_server_rpc(
            process,
            3,
            "thread/settings/update",
            {"threadId": thread_id, "effort": effort},
        )
    except (OSError, subprocess.SubprocessError, RuntimeError, TimeoutError) as exc:
        return {"ok": False, "detail": redact_sensitive_text(exc)[:180]}
    finally:
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except (OSError, subprocess.SubprocessError):
                try:
                    process.kill()
                except OSError:
                    pass
    record = thread_paths_from_db(config, {thread_id}).get(thread_id, {})
    actual = normalize_reasoning_effort(record.get("reasoning_effort"))
    if actual != effort:
        return {"ok": False, "detail": "Codex 未确认保存目标档位"}
    return {"ok": True, "detail": "已通过 Codex 对话设置接口同步"}


def redact_sensitive_text(text):
    value = str(text or "")
    patterns = (
        (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]+"), "Bearer [REDACTED]"),
        (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "sk-[REDACTED]"),
        (re.compile(r'(?i)(["\']?(?:api[_-]?key|access[_-]?token|bearer[_-]?token)["\']?\s*[:=]\s*["\']?)[^\s,"\']+'), r"\1[REDACTED]"),
    )
    for pattern, replacement in patterns:
        value = pattern.sub(replacement, value)
    return value


def process_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def process_matches_resume(pid, thread_id):
    if not process_alive(pid):
        return False
    try:
        command = (
            ["powershell.exe", "-NoProfile", "-Command", "(Get-CimInstance Win32_Process -Filter \"ProcessId={}\").CommandLine".format(int(pid))]
            if IS_WINDOWS else ["/bin/ps", "-p", str(int(pid)), "-o", "command="]
        )
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return False
    command = result.stdout.strip()
    return bool(thread_id and thread_id in command and "exec" in command and "resume" in command)


def find_resume_pid(thread_id):
    try:
        command = (
            ["powershell.exe", "-NoProfile", "-Command", "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }"]
            if IS_WINDOWS else ["/bin/ps", "-axo", "pid=,command="]
        )
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        if thread_id not in line or "exec" not in line or "resume" not in line:
            continue
        fields = line.strip().split(None, 1)
        if fields and fields[0].isdigit():
            return int(fields[0])
    return None


def resolve_codex_binary(config):
    native_candidates = ([] if IS_WINDOWS else [
        "/Applications/ChatGPT.app/Contents/Resources/codex",
        "/Applications/Codex.app/Contents/Resources/codex",
        str(Path.home() / "Applications" / "ChatGPT.app" / "Contents" / "Resources" / "codex"),
        str(Path.home() / "Applications" / "Codex.app" / "Contents" / "Resources" / "codex"),
    ])
    configured = str(config.get("codex_binary") or "")
    candidates = native_candidates + [configured] if config.get("prefer_native_codex", True) else [configured] + native_candidates
    discovered = shutil.which("codex")
    if discovered:
        candidates.append(discovered)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return configured


def codex_command(binary, *arguments):
    """Build a directly executable command for native and npm Windows shims."""
    binary = str(binary or "")
    if IS_WINDOWS and Path(binary).suffix.lower() in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", binary, *arguments]
    return [binary, *arguments]


def cleanup_run_logs(config, now=None):
    now = int(now if now is not None else time.time())
    try:
        files = sorted((path for path in RUN_LOG_DIR.iterdir() if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return
    keep = max(5, int(config.get("run_log_max_files", 40)))
    cutoff = now - max(1, int(config.get("run_log_retention_days", 7))) * 86400
    for index, path in enumerate(files):
        try:
            if index >= keep or path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def event_kind(line):
    if any(pattern.search(line) for pattern in OPEN_PATTERNS):
        return "open"
    if any(pattern.search(line) for pattern in CLOSED_PATTERNS):
        return "closed"
    return None


def line_epoch(line, fallback=None):
    match = LOG_TIME_RE.search(line)
    if match:
        try:
            parsed = dt.datetime.strptime(" ".join(match.groups()), "%Y-%m-%d %H:%M:%S")
            return int(parsed.timestamp())
        except ValueError:
            pass
    return int(fallback if fallback is not None else time.time())


def tail_json_objects(path, max_bytes=8 * 1024 * 1024):
    objects = []
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            start = max(0, size - max_bytes)
            handle.seek(start)
            if start:
                handle.readline()
            for raw_line in handle:
                try:
                    objects.append(json.loads(raw_line.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
    except OSError:
        return []
    return objects


def iter_tail_json_objects(path, max_bytes=64 * 1024 * 1024):
    """Stream a bounded rollout tail without retaining the whole file in RAM."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            start = max(0, size - max_bytes)
            handle.seek(start)
            if start:
                handle.readline()
            for raw_line in handle:
                try:
                    yield json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
    except OSError:
        return


def as_epoch(value, fallback=None):
    try:
        if value is None:
            return fallback
        return int(value)
    except (TypeError, ValueError):
        return fallback


def latest_turn_status(path):
    """Return the current turn and whether it looks interrupted or active."""
    events = tail_json_objects(path)
    start_index = None
    started = None
    for index, event in enumerate(events):
        payload = event.get("payload") or {}
        if event.get("type") == "event_msg" and payload.get("type") == "task_started":
            start_index = index
            started = payload
    if start_index is None:
        return {
            "state": "unknown",
            "turn_id": None,
            "error": None,
            "started_at": None,
            "completed_at": None,
            "user_message": None,
            "user_messages": [],
        }

    complete = None
    user_message = None
    user_messages = []
    for event in events[start_index + 1 :]:
        payload = event.get("payload") or {}
        if event.get("type") == "event_msg" and payload.get("type") == "user_message":
            message = payload.get("message")
            if isinstance(message, str):
                user_message = message
                user_messages.append(message)
        elif (
            event.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        ):
            parts = []
            for content in payload.get("content") or []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    parts.append(content["text"])
            if parts:
                user_message = "".join(parts)
                user_messages.append(user_message)
        if event.get("type") == "event_msg" and payload.get("type") == "task_complete":
            complete = payload

    file_mtime = None
    try:
        file_mtime = int(path.stat().st_mtime)
    except OSError:
        pass

    turn_id = (complete or started or {}).get("turn_id")
    if complete is None:
        return {
            "state": "active",
            "turn_id": turn_id,
            "error": None,
            "started_at": as_epoch((started or {}).get("started_at"), file_mtime),
            "completed_at": None,
            "user_message": user_message,
            "user_messages": user_messages,
        }

    final_message = complete.get("last_agent_message")
    error = complete.get("error")
    started_at = as_epoch(complete.get("started_at") or (started or {}).get("started_at"), file_mtime)
    completed_at = as_epoch(complete.get("completed_at"), file_mtime)
    if final_message:
        return {
            "state": "complete",
            "turn_id": turn_id,
            "error": None,
            "started_at": started_at,
            "completed_at": completed_at,
            "user_message": user_message,
            "user_messages": user_messages,
        }
    if error:
        error_text = json.dumps(error, ensure_ascii=False).lower()
        retryable = retryable_text(error_text)
        return {
            "state": "interrupted" if retryable else "failed_nonretryable",
            "turn_id": turn_id,
            "error": error,
            "started_at": started_at,
            "completed_at": completed_at,
            "user_message": user_message,
            "user_messages": user_messages,
        }
    return {
        "state": "interrupted",
        "turn_id": turn_id,
        "error": None,
        "started_at": started_at,
        "completed_at": completed_at,
        "user_message": user_message,
        "user_messages": user_messages,
    }


def extract_provider_details(settings_config):
    details = {"base_url": None, "api_key": None, "models": []}
    try:
        decoded = json.loads(settings_config)
    except Exception:
        decoded = settings_config

    def visit(value, key_name=""):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, str(key).lower())
        elif isinstance(value, list):
            for item in value:
                visit(item, key_name)
        elif isinstance(value, str):
            lowered = key_name.lower()
            if lowered in {"baseurl", "base_url", "openai_base_url", "anthropic_base_url"} and value.startswith("http"):
                details["base_url"] = value.rstrip("/")
            elif any(word in lowered for word in ("api_key", "apikey", "bearer_token", "auth_token")) and len(value) >= 8:
                details["api_key"] = value
            elif lowered in {"model", "model_id"} and value not in details["models"]:
                details["models"].append(value)
            if "=" in value or "base_url" in value:
                match = re.search(r'(?im)\bbase_url\s*=\s*["\']([^"\']+)', value)
                if match:
                    details["base_url"] = match.group(1).rstrip("/")
                key_match = re.search(r'(?im)\b(?:experimental_bearer_token|api_key)\s*=\s*["\']([^"\']+)', value)
                if key_match:
                    details["api_key"] = key_match.group(1)
                model_match = re.search(r'(?im)^\s*model\s*=\s*["\']([^"\']+)', value)
                if model_match and model_match.group(1) not in details["models"]:
                    details["models"].append(model_match.group(1))

    visit(decoded)
    return details


def inferred_multiplier(name, configured):
    configured_text = str(configured or "1.0")
    if configured_text not in {"1", "1.0", "1.00"}:
        return configured_text, "配置"
    match = re.search(r"(?:-|x)(0(?:\.\d+)?|1(?:\.0+)?)$", name, re.IGNORECASE)
    if match:
        return match.group(1), "名称"
    return configured_text, "配置"


def classify_provider_error(error):
    text = (error or "").lower()
    if not text:
        return ""
    if "insufficient" in text and "balance" in text:
        return "余额不足"
    if (
        "group_deleted" in text
        or "group unavailable" in text
        or "group is unavailable" in text
        or "group not found" in text
        or "group disabled" in text
        or "not assigned to a group" in text
        or any(word in text for word in ("分组已删除", "分组不存在", "分组不可用", "分组已禁用", "未分配分组"))
    ):
        return "Key 分组失效"
    if "capacity" in text or "no available upstream" in text:
        return "上游账号池满"
    if "authentication" in text or "unauthorized" in text or " 401" in text:
        return "鉴权失败"
    if any(word in text for word in ("invalid api key", "api_key_disabled", "api key disabled", "api key not found")):
        return "鉴权失败"
    if "429" in text or "rate limit" in text:
        return "请求限流"
    if "timeout" in text or "超时" in text:
        return "响应超时"
    if any(code in text for code in ("502", "503", "504", "temporarily unavailable")):
        return "上游临时故障"
    if "model" in text and any(word in text for word in ("not found", "unsupported", "invalid")):
        return "模型不兼容"
    return "请求失败"


def valid_usd_cny_rate(value):
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return False
    return 5.0 <= rate <= 10.0


def query_usd_cny_rate(timeout=5):
    endpoints = (
        ("https://api.frankfurter.app/latest?from=USD&to=CNY", lambda payload: payload.get("rates", {}).get("CNY")),
        ("https://open.er-api.com/v6/latest/USD", lambda payload: payload.get("rates", {}).get("CNY")),
    )
    for endpoint, extract in endpoints:
        try:
            request = Request(endpoint, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            rate = extract(payload)
            if valid_usd_cny_rate(rate):
                return float(rate)
        except Exception:
            continue
    return None


def resolve_usd_cny_rate(config, state, probe=False, now=None):
    now = int(now if now is not None else time.time())
    state_cache = state.get("exchange_rate_cache") if isinstance(state.get("exchange_rate_cache"), dict) else {}
    disk_cache = read_json_dict(EXCHANGE_RATE_CACHE_PATH) or {}
    cached = state_cache if valid_usd_cny_rate(state_cache.get("rate")) else disk_cache

    if probe and config.get("exchange_rate_probes_enabled", True):
        live_rate = query_usd_cny_rate()
        if valid_usd_cny_rate(live_rate):
            result = {"rate": round(float(live_rate), 6), "updated_at": now, "source": "live"}
            state["exchange_rate_cache"] = dict(result)
            ensure_dirs()
            atomic_write_json(EXCHANGE_RATE_CACHE_PATH, result)
            return result

    if valid_usd_cny_rate(cached.get("rate")):
        result = {
            "rate": round(float(cached["rate"]), 6),
            "updated_at": int(cached.get("updated_at") or 0),
            "source": "cache",
        }
        state["exchange_rate_cache"] = dict(result)
        return result

    fallback = config.get("usd_cny_fallback_rate", 7.2)
    if not valid_usd_cny_rate(fallback):
        fallback = 7.2
    result = {"rate": float(fallback), "updated_at": 0, "source": "fallback"}
    state["exchange_rate_cache"] = dict(result)
    return result


def usd_to_cny(amount, rate):
    try:
        return round(float(amount or 0) * float(rate), 8)
    except (TypeError, ValueError):
        return 0.0


def balance_in_cny(balance, rate):
    if not isinstance(balance, dict) or balance.get("status") != "ok":
        return None
    try:
        amount = float(balance.get("amount"))
    except (TypeError, ValueError):
        return None
    currency = str(balance.get("currency") or "USD").upper()
    if currency == "CNY":
        return round(amount, 8)
    if currency == "USD":
        return usd_to_cny(amount, rate)
    return None


def query_known_balance(base_url, api_key, timeout=6):
    if not base_url or not api_key:
        return None
    host = (urlsplit(base_url).hostname or "").lower()
    endpoint = None
    kind = None
    if "deepseek" in host:
        endpoint, kind = "https://api.deepseek.com/user/balance", "deepseek"
    elif "openrouter" in host:
        endpoint, kind = "https://openrouter.ai/api/v1/credits", "openrouter"
    elif "novita" in host:
        endpoint, kind = "https://api.novita.ai/v3/user/balance", "novita"
    elif "siliconflow" in host:
        endpoint, kind = base_url.rstrip("/") + "/user/info", "siliconflow"
    if not endpoint:
        return None
    request = Request(endpoint, headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"status": "error", "detail": redact_sensitive_text(exc)[:160], "checked_at": int(time.time())}
    amount = None
    currency = "USD"
    if kind == "deepseek":
        infos = payload.get("balance_infos") or []
        if infos:
            amount = infos[0].get("total_balance")
            currency = infos[0].get("currency") or "CNY"
    elif kind == "openrouter":
        data = payload.get("data") or payload
        total = data.get("total_credits")
        usage = data.get("total_usage")
        if total is not None and usage is not None:
            amount = float(total) - float(usage)
    elif kind == "novita":
        amount = payload.get("availableBalance") or payload.get("balance")
    elif kind == "siliconflow":
        data = payload.get("data") or payload
        amount = data.get("balance") or data.get("totalBalance")
    if amount is None:
        return {"status": "unsupported", "detail": "返回内容没有余额字段", "checked_at": int(time.time())}
    return {"status": "ok", "amount": str(amount), "currency": currency, "checked_at": int(time.time())}


def query_sub2api_billing(base_url, api_key, timeout=8):
    """Read Sub2API's key-scoped effective multiplier without spending quota."""
    if not base_url or not api_key:
        return None
    root = base_url.rstrip("/")
    endpoint = root + "/sub2api/billing" if root.endswith("/v1") else root + "/v1/sub2api/billing"
    request = Request(
        endpoint,
        headers={
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return {"status": "unsupported", "detail": "站点未提供倍率自省接口", "checked_at": int(time.time())}
        detail = "HTTP {}".format(exc.code)
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            error = payload.get("error") if isinstance(payload, dict) else None
            message = error.get("message") if isinstance(error, dict) else payload.get("message")
            if message:
                detail = str(message)
        except Exception:
            pass
        return {"status": "error", "detail": redact_sensitive_text(detail)[:180], "checked_at": int(time.time())}
    except Exception as exc:
        return {"status": "error", "detail": redact_sensitive_text(exc)[:180], "checked_at": int(time.time())}

    if not isinstance(payload, dict) or payload.get("object") != "sub2api.key_billing":
        return {"status": "unsupported", "detail": "站点未提供倍率自省接口", "checked_at": int(time.time())}
    effective = payload.get("effective_rate_multiplier")
    resolved = payload.get("resolved_rate_multiplier")
    group = payload.get("group_rate_multiplier")
    try:
        effective = float(effective)
        resolved = float(resolved)
        group = float(group)
    except (TypeError, ValueError):
        return {"status": "error", "detail": "倍率接口返回内容不完整", "checked_at": int(time.time())}
    return {
        "status": "ok",
        "group_multiplier": group,
        "resolved_multiplier": resolved,
        "effective_multiplier": effective,
        "peak_rate_enabled": bool(payload.get("peak_rate_enabled")),
        "observed_at": payload.get("observed_at"),
        "checked_at": int(time.time()),
    }


def numeric_multiplier(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def sqlite_columns(db, table):
    """Read a table's columns without assuming one CC Switch schema version."""
    try:
        return {row[1] for row in db.execute("PRAGMA table_info({})".format(table)).fetchall()}
    except sqlite3.Error:
        return set()


def optional_sql_column(alias, columns, name, fallback):
    return "{}.{}".format(alias, name) if name in columns else fallback


def local_day_start(timestamp):
    local = time.localtime(timestamp)
    return int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, -1, -1, -1)))


def provider_failure_reason(item, error_class, last_status, billing_result):
    billing_detail = str((billing_result or {}).get("detail") or "")
    billing_class = classify_provider_error(billing_detail)
    if billing_class in {"Key 分组失效", "鉴权失败", "余额不足"}:
        return billing_class
    failures = int(item.get("consecutive_failures") or 0)
    if item.get("is_healthy") == 0:
        reason = error_class or "线路连续失败"
        return "已熔断（连续 {} 次失败：{}）".format(failures, reason) if failures else "已熔断（{}）".format(reason)
    if failures > 0:
        reason = error_class or "最近请求失败"
        return "降级（连续 {} 次失败：{}）".format(failures, reason)
    if error_class:
        return error_class
    if last_status and not 200 <= last_status < 400:
        return "请求失败（HTTP {}）".format(last_status)
    return "正常" if last_status else "尚无调用记录"


def build_provider_snapshot(
    config, state, probe_balances=False, probe_billing=False, probe_exchange=False, now=None
):
    now = int(now if now is not None else time.time())
    exchange = resolve_usd_cny_rate(config, state, probe=probe_exchange, now=now)
    usd_cny_rate = float(exchange["rate"])
    day_start = local_day_start(now)
    db_path = config["cc_switch_db"]
    if not os.path.exists(db_path):
        return {
            "generated_at": now,
            "providers": [],
            "error": "CC Switch 数据库不存在",
            "usd_cny_rate": usd_cny_rate,
            "exchange_rate_updated_at": exchange["updated_at"],
            "exchange_rate_source": exchange["source"],
        }
    uri = "file:{}?mode=ro".format(db_path)
    try:
        with sqlite_connection(uri, uri=True, timeout=3.0) as db:
            db.row_factory = sqlite3.Row
            log_columns = sqlite_columns(db, "proxy_request_logs")
            provider_columns = sqlite_columns(db, "providers")
            health_columns = sqlite_columns(db, "provider_health")
            if not {"id", "name", "app_type"}.issubset(provider_columns):
                return {
                    "generated_at": now,
                    "providers": [],
                    "error": "CC Switch providers 表缺少 id、name 或 app_type 字段",
                    "usd_cny_rate": usd_cny_rate,
                    "exchange_rate_updated_at": exchange["updated_at"],
                    "exchange_rate_source": exchange["source"],
                }
            provider_created_expr = optional_sql_column("p", provider_columns, "created_at", "p.name")
            provider_settings_expr = optional_sql_column("p", provider_columns, "settings_config", "''")
            provider_website_expr = optional_sql_column("p", provider_columns, "website_url", "''")
            provider_current_expr = optional_sql_column("p", provider_columns, "is_current", "0")
            provider_queue_expr = optional_sql_column("p", provider_columns, "in_failover_queue", "0")
            provider_sort_expr = optional_sql_column("p", provider_columns, "sort_index", "NULL")
            provider_multiplier_expr = optional_sql_column("p", provider_columns, "cost_multiplier", "'1.0'")
            provider_type_expr = optional_sql_column("p", provider_columns, "provider_type", "''")
            health_join = ""
            health_expr = {name: "NULL" for name in ("is_healthy", "consecutive_failures", "last_success_at", "last_failure_at", "last_error")}
            if {"provider_id", "app_type"}.issubset(health_columns):
                health_join = "LEFT JOIN provider_health h ON h.provider_id=p.id AND h.app_type=p.app_type"
                health_expr = {name: optional_sql_column("h", health_columns, name, "NULL") for name in health_expr}
            row_cost_expr = "CAST(total_cost_usd AS REAL)" if "total_cost_usd" in log_columns else "0"
            cost_24h_expr = "ROUND(SUM({}), 8)".format(row_cost_expr)
            cost_today_expr = "ROUND(SUM(CASE WHEN created_at>=? THEN {} ELSE 0 END), 8)".format(row_cost_expr)
            latest_multiplier_expr = "l.cost_multiplier" if "cost_multiplier" in log_columns else "NULL"
            relay_predicate = "COALESCE(data_source,'proxy')='proxy'" if "data_source" in log_columns else "1=1"
            providers = db.execute(
                """
                SELECT p.id,p.app_type,p.name,{settings},{website},{current},{queue},{sort},
                       {multiplier},{provider_type},
                       {is_healthy},{consecutive_failures},{last_success_at},{last_failure_at},{last_error}
                FROM providers p
                {health_join}
                WHERE p.app_type IN ('codex','claude-desktop')
                ORDER BY CASE p.app_type WHEN 'codex' THEN 0 ELSE 1 END,
                         CASE WHEN {sort} IS NULL THEN 1 ELSE 0 END,
                         {sort} ASC,{created} ASC,p.name ASC
                """.format(
                    settings=provider_settings_expr,
                    website=provider_website_expr,
                    current=provider_current_expr,
                    queue=provider_queue_expr,
                    sort=provider_sort_expr,
                    multiplier=provider_multiplier_expr,
                    provider_type=provider_type_expr,
                    is_healthy=health_expr["is_healthy"],
                    consecutive_failures=health_expr["consecutive_failures"],
                    last_success_at=health_expr["last_success_at"],
                    last_failure_at=health_expr["last_failure_at"],
                    last_error=health_expr["last_error"],
                    health_join=health_join,
                    created=provider_created_expr,
                )
            ).fetchall()
            log_usable = {
                "app_type", "provider_id", "status_code", "created_at",
                "latency_ms", "error_message", "model", "request_model",
            }.issubset(log_columns)
            if log_usable:
                stats_rows = db.execute(
                    """
                    SELECT app_type,provider_id,COUNT(*) requests,
                           SUM(CASE WHEN status_code BETWEEN 200 AND 399 THEN 1 ELSE 0 END) successes,
                           ROUND(AVG(CASE WHEN status_code BETWEEN 200 AND 399 THEN latency_ms END)) avg_latency,
                           MAX(created_at) last_request_at,
                           {} standard_cost_today_usd,
                           {} standard_cost_24h_usd
                    FROM proxy_request_logs
                    WHERE app_type IN ('codex','claude-desktop') AND created_at>=?
                    GROUP BY app_type,provider_id
                    """.format(cost_today_expr, cost_24h_expr),
                    (day_start, now - 86400,),
                ).fetchall()
            else:
                stats_rows = []
            if "total_cost_usd" in log_columns:
                total_rows = db.execute(
                    """
                    SELECT app_type,provider_id,ROUND(SUM(CAST(total_cost_usd AS REAL)), 8) standard_cost_all_usd
                    FROM proxy_request_logs
                    WHERE app_type IN ('codex','claude-desktop')
                    GROUP BY app_type,provider_id
                    """
                ).fetchall() if log_usable else []
            else:
                total_rows = []
            latest_rows = db.execute(
                """
                SELECT l.app_type,l.provider_id,l.status_code,l.error_message,l.model,l.request_model,l.created_at,
                       {} logged_multiplier
                FROM proxy_request_logs l
                JOIN (
                    SELECT app_type,provider_id,MAX(created_at) created_at FROM proxy_request_logs
                    WHERE app_type IN ('codex','claude-desktop') GROUP BY app_type,provider_id
                ) newest ON newest.app_type=l.app_type AND newest.provider_id=l.provider_id AND newest.created_at=l.created_at
                WHERE l.app_type IN ('codex','claude-desktop')
                """.format(latest_multiplier_expr)
            ).fetchall() if log_usable else []
            usage_rows = db.execute(
                """
                SELECT app_type,
                       ROUND(SUM(CASE WHEN created_at>=? THEN {cost} ELSE 0 END), 8) cc_switch_standard_cost_today_usd,
                       ROUND(SUM({cost}), 8) cc_switch_standard_cost_all_usd,
                       ROUND(SUM(CASE WHEN created_at>=? AND {relay} THEN {cost} ELSE 0 END), 8) relay_standard_cost_today_usd,
                       ROUND(SUM(CASE WHEN {relay} THEN {cost} ELSE 0 END), 8) relay_standard_cost_all_usd
                FROM proxy_request_logs
                WHERE app_type IN ('codex','claude-desktop')
                GROUP BY app_type
                """.format(cost=row_cost_expr, relay=relay_predicate),
                (day_start, day_start),
            ).fetchall() if log_usable else []
            snapshot_warning = None if log_usable else "CC Switch 请求日志表缺少可识别字段，费用与健康记录暂不可用"
    except sqlite3.Error as exc:
        return {"generated_at": now, "providers": [], "error": str(exc)}

    stats = {(row["app_type"], row["provider_id"]): dict(row) for row in stats_rows}
    totals = {(row["app_type"], row["provider_id"]): dict(row) for row in total_rows}
    latest = {(row["app_type"], row["provider_id"]): dict(row) for row in latest_rows}
    usage_by_app = {row["app_type"]: dict(row) for row in usage_rows}
    balance_cache = state.setdefault("balance_cache", {})
    billing_cache = state.setdefault("billing_cache", {})
    provider_items = [dict(row) for row in providers]
    provider_details = {
        (item["app_type"], item["id"]): extract_provider_details(item.get("settings_config") or "")
        for item in provider_items
    }

    if probe_billing and config.get("billing_probes_enabled", True):
        jobs = {}
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(provider_items)))) as pool:
            for item in provider_items:
                cache_key = "{}:{}".format(item["app_type"], item["id"])
                details = provider_details[(item["app_type"], item["id"])]
                if details.get("base_url") and details.get("api_key"):
                    future = pool.submit(query_sub2api_billing, details["base_url"], details["api_key"])
                    jobs[future] = cache_key
            for future in as_completed(jobs):
                try:
                    result = future.result()
                except Exception as exc:
                    result = {"status": "error", "detail": redact_sensitive_text(exc)[:180], "checked_at": now}
                if result is not None:
                    billing_cache[jobs[future]] = result

    output = []
    total_standard_24h = 0.0
    total_standard_all = 0.0
    total_estimated_24h = 0.0
    total_estimated_all = 0.0
    estimated_provider_count = 0
    app_summaries = {}
    app_display_orders = {}
    for item in provider_items:
        app_type = item["app_type"]
        raw_id = item["id"]
        cache_key = "{}:{}".format(app_type, raw_id)
        details = provider_details[(app_type, raw_id)]
        provider_stats = stats.get((app_type, raw_id), {})
        provider_totals = totals.get((app_type, raw_id), {})
        last = latest.get((app_type, raw_id), {})
        requests = int(provider_stats.get("requests") or 0)
        successes = int(provider_stats.get("successes") or 0)
        success_rate = round(successes * 100.0 / requests, 1) if requests else None
        last_error = last.get("error_message") or item.get("last_error") or ""
        error_class = classify_provider_error(last_error)
        last_status = int(last.get("status_code") or 0)
        billing = billing_cache.get(cache_key) or (billing_cache.get(raw_id) if app_type == "codex" else None)
        billing_class = classify_provider_error(str((billing or {}).get("detail") or ""))
        failures = int(item.get("consecutive_failures") or 0)
        if billing_class in {"余额不足", "Key 分组失效", "鉴权失败"}:
            usability = "unavailable"
        elif item.get("is_healthy") == 0:
            usability = "unavailable"
        elif failures > 0:
            # Match CC Switch exactly: healthy + one or more consecutive
            # failures is shown as degraded, not operational.
            usability = "degraded"
        elif requests and successes and (last_status == 0 or 200 <= last_status < 400):
            usability = "usable"
        elif error_class in {"余额不足", "Key 分组失效", "鉴权失败", "模型不兼容"}:
            usability = "unavailable"
        elif requests:
            usability = "degraded"
        else:
            usability = "untested"

        if probe_balances and config.get("balance_probes_enabled", True):
            balance_result = query_known_balance(details.get("base_url"), details.get("api_key"))
            if balance_result is not None:
                balance_cache[cache_key] = balance_result
        balance = balance_cache.get(cache_key) or (balance_cache.get(raw_id) if app_type == "codex" else None)
        if error_class == "余额不足":
            balance_display = "余额不足"
            balance_state = "empty"
        elif balance and balance.get("status") == "ok":
            balance_cny = balance_in_cny(balance, usd_cny_rate)
            if balance_cny is None:
                balance_display = "{} {}（无法换算）".format(
                    balance.get("currency", "未知币种"), balance.get("amount", "—")
                )
                balance_state = "error"
            else:
                balance_display = "¥{:.2f}".format(balance_cny)
                balance_state = "known"
        elif balance and balance.get("status") == "error":
            balance_display = "查询失败"
            balance_state = "error"
        else:
            balance_display = "渠道未提供查询接口"
            balance_state = "unsupported"

        remarked_multiplier, remarked_multiplier_source = inferred_multiplier(item["name"], item.get("cost_multiplier"))
        actual_multiplier = None
        actual_multiplier_source = "无法验证"
        actual_multiplier_state = "unknown"
        if billing and billing.get("status") == "ok":
            actual_multiplier = numeric_multiplier(billing.get("effective_multiplier"))
            if actual_multiplier is not None:
                actual_multiplier_source = "网站实测"
                actual_multiplier_state = "verified"
        if actual_multiplier is None:
            logged_multiplier = numeric_multiplier(last.get("logged_multiplier"))
            if logged_multiplier is not None:
                actual_multiplier = logged_multiplier
                actual_multiplier_source = "请求记录"
                actual_multiplier_state = "observed"
            elif billing and billing.get("status") == "error":
                actual_multiplier_source = "查询失败"
                actual_multiplier_state = "error"
        remarked_number = numeric_multiplier(remarked_multiplier)
        multiplier_changed = (
            actual_multiplier is not None
            and remarked_number is not None
            and abs(actual_multiplier - remarked_number) > 0.000001
        )

        standard_cost_24h = float(provider_stats.get("standard_cost_24h_usd") or 0)
        standard_cost_today = float(provider_stats.get("standard_cost_today_usd") or 0)
        standard_cost_all = float(provider_totals.get("standard_cost_all_usd") or 0)
        estimated_cost_24h = round(standard_cost_24h * actual_multiplier, 8) if actual_multiplier is not None else None
        estimated_cost_today = round(standard_cost_today * actual_multiplier, 8) if actual_multiplier is not None else None
        estimated_cost_all = round(standard_cost_all * actual_multiplier, 8) if actual_multiplier is not None else None
        total_standard_24h += standard_cost_24h
        total_standard_all += standard_cost_all
        if estimated_cost_24h is not None:
            total_estimated_24h += estimated_cost_24h
            total_estimated_all += estimated_cost_all or 0
            estimated_provider_count += 1

        app_usage = usage_by_app.get(app_type, {})
        summary = app_summaries.setdefault(app_type, {
            "provider_count": 0,
            "estimated_provider_count": 0,
            "total_standard_cost_24h_usd": 0.0,
            "total_standard_cost_all_usd": 0.0,
            "total_estimated_actual_cost_24h_usd": 0.0,
            "total_estimated_actual_cost_all_usd": 0.0,
            "cc_switch_standard_cost_today_usd": float(app_usage.get("cc_switch_standard_cost_today_usd") or 0),
            "cc_switch_standard_cost_all_usd": float(app_usage.get("cc_switch_standard_cost_all_usd") or 0),
            "relay_standard_cost_today_usd": float(app_usage.get("relay_standard_cost_today_usd") or 0),
            "relay_standard_cost_all_usd": float(app_usage.get("relay_standard_cost_all_usd") or 0),
            "verified_relay_standard_cost_today_usd": 0.0,
            "estimated_relay_actual_cost_today_usd": 0.0,
            "unpriced_relay_standard_cost_today_usd": 0.0,
        })
        summary["provider_count"] += 1
        summary["total_standard_cost_24h_usd"] += standard_cost_24h
        summary["total_standard_cost_all_usd"] += standard_cost_all
        if estimated_cost_24h is not None:
            summary["estimated_provider_count"] += 1
            summary["total_estimated_actual_cost_24h_usd"] += estimated_cost_24h
            summary["total_estimated_actual_cost_all_usd"] += estimated_cost_all or 0
            summary["verified_relay_standard_cost_today_usd"] += standard_cost_today
            summary["estimated_relay_actual_cost_today_usd"] += estimated_cost_today or 0

        display_order = app_display_orders.get(app_type, 0)
        app_display_orders[app_type] = display_order + 1

        failure_reason = provider_failure_reason(item, error_class or billing_class, last_status, billing)
        output.append(
            {
                "id": cache_key,
                "provider_id": raw_id,
                "app_type": app_type,
                "display_order": display_order,
                "sort_index": int(item["sort_index"]) if item.get("sort_index") is not None else None,
                "name": item["name"],
                "host": urlsplit(details.get("base_url") or item.get("website_url") or "").hostname or "未知地址",
                "is_current": bool(item.get("is_current")),
                "in_failover_queue": bool(item.get("in_failover_queue")),
                "multiplier": remarked_multiplier,
                "multiplier_source": remarked_multiplier_source,
                "remarked_multiplier": remarked_multiplier,
                "remarked_multiplier_source": remarked_multiplier_source,
                "actual_multiplier": actual_multiplier,
                "actual_multiplier_source": actual_multiplier_source,
                "actual_multiplier_state": actual_multiplier_state,
                "multiplier_changed": multiplier_changed,
                "group_multiplier": (billing or {}).get("group_multiplier") if (billing or {}).get("status") == "ok" else None,
                "resolved_multiplier": (billing or {}).get("resolved_multiplier") if (billing or {}).get("status") == "ok" else None,
                "billing_checked_at": (billing or {}).get("checked_at"),
                "billing_error": redact_sensitive_text((billing or {}).get("detail") or "")[:180],
                "usability": usability,
                "failure_reason": failure_reason,
                "circuit_state": "open" if item.get("is_healthy") == 0 else ("closed" if item.get("is_healthy") == 1 else "unknown"),
                "consecutive_failures": int(item.get("consecutive_failures") or 0),
                "temporarily_bypassed": any(
                    bypass.get("provider_id") == raw_id and bypass.get("app_type") == app_type
                    for bypass in state.get("temporary_failover_bypasses", [])
                ),
                "requests_24h": requests,
                "success_rate_24h": success_rate,
                "avg_latency_ms": provider_stats.get("avg_latency"),
                "last_request_at": provider_stats.get("last_request_at"),
                "last_status": last_status or None,
                "last_model": last.get("request_model") or last.get("model") or (details.get("models") or [None])[0],
                "error_class": error_class,
                "last_error": redact_sensitive_text(last_error)[:240],
                "balance": balance_display,
                "balance_state": balance_state,
                "balance_cny": balance_in_cny(balance, usd_cny_rate),
                "balance_original_currency": (balance or {}).get("currency"),
                "balance_original_amount": (balance or {}).get("amount"),
                "standard_cost_24h_usd": round(standard_cost_24h, 8),
                "standard_cost_all_usd": round(standard_cost_all, 8),
                "estimated_actual_cost_24h_usd": estimated_cost_24h,
                "estimated_actual_cost_all_usd": estimated_cost_all,
                "standard_cost_24h_cny": usd_to_cny(standard_cost_24h, usd_cny_rate),
                "standard_cost_all_cny": usd_to_cny(standard_cost_all, usd_cny_rate),
                "estimated_actual_cost_24h_cny": (
                    usd_to_cny(estimated_cost_24h, usd_cny_rate) if estimated_cost_24h is not None else None
                ),
                "estimated_actual_cost_all_cny": (
                    usd_to_cny(estimated_cost_all, usd_cny_rate) if estimated_cost_all is not None else None
                ),
            }
        )
    for summary in app_summaries.values():
        summary["unpriced_relay_standard_cost_today_usd"] = max(
            0.0,
            summary["relay_standard_cost_today_usd"] - summary["verified_relay_standard_cost_today_usd"],
        )
        for key in (
            "total_standard_cost_24h_usd",
            "total_standard_cost_all_usd",
            "total_estimated_actual_cost_24h_usd",
            "total_estimated_actual_cost_all_usd",
            "cc_switch_standard_cost_today_usd",
            "cc_switch_standard_cost_all_usd",
            "relay_standard_cost_today_usd",
            "relay_standard_cost_all_usd",
            "verified_relay_standard_cost_today_usd",
            "estimated_relay_actual_cost_today_usd",
            "unpriced_relay_standard_cost_today_usd",
        ):
            summary[key] = round(summary[key], 8)
            summary[key.replace("_usd", "_cny")] = usd_to_cny(summary[key], usd_cny_rate)
    return {
        "generated_at": now,
        "providers": output,
        "error": snapshot_warning,
        "total_standard_cost_24h_usd": round(total_standard_24h, 8),
        "total_standard_cost_all_usd": round(total_standard_all, 8),
        "total_estimated_actual_cost_24h_usd": round(total_estimated_24h, 8),
        "total_estimated_actual_cost_all_usd": round(total_estimated_all, 8),
        "estimated_provider_count": estimated_provider_count,
        "app_summaries": app_summaries,
        "total_standard_cost_24h_cny": usd_to_cny(total_standard_24h, usd_cny_rate),
        "total_standard_cost_all_cny": usd_to_cny(total_standard_all, usd_cny_rate),
        "total_estimated_actual_cost_24h_cny": usd_to_cny(total_estimated_24h, usd_cny_rate),
        "total_estimated_actual_cost_all_cny": usd_to_cny(total_estimated_all, usd_cny_rate),
        "usd_cny_rate": usd_cny_rate,
        "exchange_rate_updated_at": exchange["updated_at"],
        "exchange_rate_source": exchange["source"],
    }


def write_provider_snapshot(
    config, state, probe_balances=False, probe_billing=False, probe_exchange=False, now=None
):
    snapshot = build_provider_snapshot(
        config,
        state,
        probe_balances=probe_balances,
        probe_billing=probe_billing,
        probe_exchange=probe_exchange,
        now=now,
    )
    temp_path = PROVIDERS_PATH.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(str(temp_path), str(PROVIDERS_PATH))
    return snapshot


def valid_thread_id(value):
    return bool(value and THREAD_ID_RE.match(str(value)))


def codex_state_db_candidates(config, home=None):
    """Return state DB candidates without overriding an explicit custom path.

    Codex Desktop moved ``state_5.sqlite`` from ``~/.codex/sqlite`` to
    ``~/.codex``.  Existing installations can therefore still carry the old
    generated default even though the live thread index is now at the new
    location.  Only those two known defaults are auto-resolved; any other path
    is treated as a deliberate user customization.
    """
    home = Path(home) if home is not None else Path.home()
    modern = home / ".codex" / "state_5.sqlite"
    legacy = home / ".codex" / "sqlite" / "state_5.sqlite"
    configured_text = str(config.get("codex_state_db") or modern)
    configured = Path(configured_text).expanduser()
    if configured not in {modern, legacy}:
        return [configured]
    return [modern, legacy]


def resolve_codex_state_db(config, home=None):
    candidates = codex_state_db_candidates(config, home=home)
    return next((path for path in candidates if path.is_file()), candidates[0])


def warning_throttled(key, message, *args, interval=300):
    """Log a recurring warning at most once per interval."""
    now = time.monotonic()
    last = _WARNING_THROTTLE.get(key)
    if last is not None and now - last < interval:
        return False
    _WARNING_THROTTLE[key] = now
    logging.warning(message, *args)
    return True


def thread_paths_from_db(config, thread_ids):
    if not thread_ids:
        return {}
    placeholders = ",".join("?" for _ in thread_ids)
    errors = []
    for db_path in codex_state_db_candidates(config):
        if not db_path.is_file():
            continue
        result = {}
        try:
            uri = db_path.resolve().as_uri() + "?mode=ro"
            with sqlite_connection(uri, uri=True, timeout=2.0) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(threads)").fetchall()}
                effort_column = ", reasoning_effort" if "reasoning_effort" in columns else ""
                rows = db.execute(
                    "SELECT id, rollout_path, cwd, title{} FROM threads WHERE id IN ({})".format(
                        effort_column, placeholders
                    ),
                    list(thread_ids),
                ).fetchall()
            for row in rows:
                thread_id, rollout_path, cwd, title = row[:4]
                reasoning_effort = row[4] if len(row) > 4 else None
                result[thread_id] = {
                    "path": rollout_path,
                    "cwd": cwd,
                    "title": title or thread_id,
                }
                if normalize_reasoning_effort(reasoning_effort):
                    result[thread_id]["reasoning_effort"] = normalize_reasoning_effort(reasoning_effort)
            _WARNING_THROTTLE.pop("codex-state-db", None)
            return result
        except sqlite3.Error as exc:
            errors.append("{}: {}".format(db_path, exc))
    if errors:
        warning_throttled(
            "codex-state-db",
            "cannot query Codex state DB: %s",
            "; ".join(errors),
        )
    return {}


def fallback_thread_path(config, thread_id):
    pattern = os.path.join(config["codex_sessions_dir"], "**", "*{}*.jsonl".format(thread_id))
    matches = glob.glob(pattern, recursive=True)
    return max(matches, key=os.path.getmtime) if matches else None


def failed_thread_ids(config, opened_at, recovered_at):
    db_path = config["cc_switch_db"]
    padding = int(config["candidate_window_padding_seconds"])
    start = int(opened_at) - padding
    end = int(recovered_at) + padding
    result = set()
    if not os.path.exists(db_path):
        return result
    uri = "file:{}?mode=ro".format(db_path)
    try:
        with sqlite_connection(uri, uri=True, timeout=2.0) as db:
            rows = db.execute(
                """
                SELECT DISTINCT session_id
                FROM proxy_request_logs
                WHERE app_type = 'codex'
                  AND created_at BETWEEN ? AND ?
                  AND status_code >= 400
                  AND session_id IS NOT NULL
                """,
                (start, end),
            ).fetchall()
        result.update(str(row[0]) for row in rows if valid_thread_id(row[0]))
    except sqlite3.Error as exc:
        logging.warning("cannot query CC Switch request logs: %s", exc)
    return result


def recently_touched_thread_ids(config, opened_at, recovered_at):
    result = set()
    sessions_dir = config["codex_sessions_dir"]
    padding = int(config["candidate_window_padding_seconds"])
    start = int(opened_at) - padding
    end = int(recovered_at) + padding + int(config["recovery_grace_seconds"])
    pattern = os.path.join(sessions_dir, "**", "rollout-*.jsonl")
    for path in glob.iglob(pattern, recursive=True):
        try:
            modified = os.path.getmtime(path)
        except OSError:
            continue
        if start <= modified <= end:
            match = re.search(r"(01[0-9a-f]{2}[0-9a-f-]{32})\.jsonl$", path, re.IGNORECASE)
            if match:
                result.add(match.group(1))
    return result


def latest_success_after(config, started_at):
    db_path = config.get("cc_switch_db")
    if not db_path or not os.path.exists(db_path):
        return None
    uri = "file:{}?mode=ro".format(db_path)
    try:
        with sqlite_connection(uri, uri=True, timeout=3.0) as db:
            row = db.execute(
                """
                SELECT MAX(created_at) FROM proxy_request_logs
                WHERE app_type='codex' AND status_code BETWEEN 200 AND 399 AND created_at>=?
                """,
                (int(started_at),),
            ).fetchone()
        return int(row[0]) if row and row[0] else None
    except (sqlite3.Error, TypeError, ValueError):
        return None


def build_candidates(config, incident, state, now=None, include_pending=False):
    now = int(now if now is not None else time.time())
    opened_at = int(incident["opened_at"])
    recovered_at = int(incident["recovered_at"])
    ids = failed_thread_ids(config, opened_at, recovered_at)
    ids.update(recently_touched_thread_ids(config, opened_at, recovered_at))
    records = thread_paths_from_db(config, ids)
    candidates = []
    pending = []

    for thread_id in sorted(ids):
        record = records.get(thread_id, {})
        path_text = record.get("path") or fallback_thread_path(config, thread_id)
        if not path_text:
            continue
        path = Path(path_text)
        status = latest_turn_status(path)
        turn_id = status.get("turn_id")
        if not turn_id or turn_id in state.get("resumed_turns", {}):
            continue
        if status["state"] == "active":
            try:
                if now - int(path.stat().st_mtime) < int(config["active_thread_idle_seconds"]):
                    pending.append(
                        {
                            "thread_id": thread_id,
                            "turn_id": turn_id,
                            "title": record.get("title") or thread_id,
                            "rollout_path": str(path),
                        }
                    )
                    continue
            except OSError:
                continue
            status["state"] = "interrupted"
        if status["state"] != "interrupted":
            continue
        candidates.append(
            {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "title": record.get("title") or thread_id,
                "cwd": record.get("cwd") or str(Path.home()),
                "rollout_path": str(path),
                "detected_at": now,
            }
        )
        remembered_effort = state.get("reasoning_efforts", {}).get(thread_id)
        if normalize_reasoning_effort(remembered_effort):
            candidates[-1]["reasoning_effort"] = normalize_reasoning_effort(remembered_effort)
    # The configured limit is a launch batch size, not a hard incident cap.
    # Returning every candidate lets the watcher persist overflow work and
    # process it after the first batch drains instead of silently dropping it.
    return (candidates, pending) if include_pending else candidates


def notify(title, body):
    if IS_WINDOWS:
        return
    script = 'display notification {} with title {}'.format(json.dumps(body), json.dumps(title))
    try:
        subprocess.Popen(
            ["/usr/bin/osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


class Watcher:
    def __init__(self, config, dry_run=False):
        self.config = config
        self.state = load_state()
        # A previous process may have died after removing P1 from the queue.
        # Always restore first; a due gateway retry will create a fresh,
        # bounded bypass immediately before it launches.
        restore_temporary_failover_bypasses(self.config, self.state)
        now = int(time.time())
        if not self.state.get("retry_scan_cursor"):
            self.state["retry_scan_cursor"] = now
        if not self.state.get("watch_started_at"):
            self.state["watch_started_at"] = now
        self.dry_run = dry_run
        self.running = True
        self.processes = {}
        self.last_checkpoint_at = 0

    def stop(self, *_args):
        self.running = False

    def set_event(self, message, when=None):
        self.state["last_event"] = message
        self.state["last_event_at"] = int(when if when is not None else time.time())
        logging.info(message)

    def checkpoint(self, now=None, force=False):
        now = int(now if now is not None else time.time())
        interval = max(1, int(self.config.get("state_checkpoint_seconds", 10)))
        if force or now - self.last_checkpoint_at >= interval:
            save_state(self.state)
            self.last_checkpoint_at = now

    def is_inflight(self, thread_id=None, turn_id=None):
        for item in self.state.setdefault("inflight", {}).values():
            if thread_id and item.get("thread_id") == thread_id:
                return True
            if turn_id and item.get("turn_id") == turn_id:
                return True
        return False

    def clear_reasoning_state(self, thread_id, include_restore=False):
        for key in ("reasoning_efforts", "reasoning_originals", "reasoning_last_capacity_at"):
            self.state.setdefault(key, {}).pop(thread_id, None)
        if include_restore:
            self.state.setdefault("reasoning_restore_pending", {}).pop(thread_id, None)

    def remember_reasoning_restore(self, thread_id, record, target_effort, fallback_effort, now):
        target_effort = normalize_reasoning_effort(target_effort)
        fallback_effort = normalize_reasoning_effort(fallback_effort)
        if not target_effort or not fallback_effort or not reasoning_effort_is_lower(fallback_effort, target_effort):
            return False
        cooldown = max(60, int(self.config.get("capacity_reasoning_promote_after_seconds", 900)))
        self.state.setdefault("reasoning_restore_pending", {})[thread_id] = {
            "target_effort": target_effort,
            "fallback_effort": fallback_effort,
            "not_before": now + cooldown,
            "last_capacity_at": now,
            "title": (record or {}).get("title") or thread_id,
            "rollout_path": str((record or {}).get("path") or ""),
        }
        return True

    def discover_reasoning_restore_candidate(self, thread_id, record, now):
        if not self.config.get("capacity_reasoning_promote_enabled", True):
            return False
        target = configured_reasoning_effort(self.config)
        current = normalize_reasoning_effort((record or {}).get("reasoning_effort"))
        path_text = str((record or {}).get("path") or "")
        if not current or not path_text:
            return False
        pending = self.state.setdefault("reasoning_restore_pending", {})
        existing = pending.get(thread_id) or {}
        scans = self.state.setdefault("reasoning_restore_last_scan", {})
        last_scan = int(scans.get(thread_id) or 0)
        if last_scan and now - last_scan < 3600:
            return False
        scans[thread_id] = now
        max_age = max(3600, int(float(self.config.get("model_retry_watch_hours", 24)) * 3600))
        hint = reasoning_restore_hint(Path(path_text), target, now=now, max_age_seconds=max_age)
        if not hint:
            return False
        detected_target = normalize_reasoning_effort(hint.get("target_effort"))
        if not detected_target or not reasoning_effort_is_lower(current, detected_target):
            return False
        existing_target = normalize_reasoning_effort(existing.get("target_effort"))
        if existing_target and not reasoning_effort_is_lower(existing_target, detected_target):
            return False
        cooldown = max(60, int(self.config.get("capacity_reasoning_promote_after_seconds", 900)))
        pending[thread_id] = {
            **existing,
            **hint,
            "not_before": max(now, int(hint.get("last_capacity_at") or now) + cooldown),
            "title": (record or {}).get("title") or thread_id,
            "rollout_path": path_text,
        }
        return True

    def restore_due_desktop_reasoning(self, now):
        if (
            self.dry_run
            or not self.config.get("capacity_reasoning_promote_enabled", True)
            or not self.config.get("capacity_reasoning_desktop_sync_enabled", True)
        ):
            return False
        pending = self.state.setdefault("reasoning_restore_pending", {})
        due = sorted(
            (
                (thread_id, item)
                for thread_id, item in pending.items()
                if int((item or {}).get("not_before") or 0) <= now
            ),
            key=lambda pair: int((pair[1] or {}).get("not_before") or 0),
        )
        if not due:
            return False
        thread_id, item = due[0]
        if self.is_inflight(thread_id=thread_id) or any(
            queued.get("thread_id") == thread_id
            for queued in (self.state.get("scheduled_retries") or []) + (self.state.get("queue") or [])
        ):
            item["not_before"] = now + 60
            return True
        record = thread_paths_from_db(self.config, {thread_id}).get(thread_id, {})
        path_text = record.get("path") or item.get("rollout_path") or fallback_thread_path(self.config, thread_id)
        if not path_text or not Path(path_text).is_file():
            item["not_before"] = now + 300
            item["last_error"] = "找不到 Codex 对话记录"
            return True
        status = latest_turn_status(Path(path_text))
        if status.get("state") == "active":
            item["not_before"] = now + 60
            return True
        target = normalize_reasoning_effort(item.get("target_effort"))
        current = normalize_reasoning_effort(record.get("reasoning_effort"))
        if not target:
            pending.pop(thread_id, None)
            return True
        if current == target or (current and not reasoning_effort_is_lower(current, target)):
            pending.pop(thread_id, None)
            self.set_event(
                "对话已经是“{}”档：{}".format(reasoning_effort_label(current or target), item.get("title") or thread_id),
                now,
            )
            return True
        result = update_thread_reasoning_effort(self.config, thread_id, target)
        if result.get("ok"):
            pending.pop(thread_id, None)
            self.state["last_reasoning_restore_at"] = now
            self.state["last_reasoning_restore_title"] = item.get("title") or thread_id
            self.state["last_reasoning_restore_error"] = ""
            self.set_event(
                "Codex 对话已自动恢复为“{}”：{}".format(reasoning_effort_label(target), item.get("title") or thread_id),
                now,
            )
            return True
        item["not_before"] = now + 60
        item["last_error"] = str(result.get("detail") or "Codex 对话档位同步失败")[:180]
        self.state["last_reasoning_restore_error"] = item["last_error"]
        warning_throttled(
            "reasoning-restore-{}".format(thread_id),
            "cannot restore Codex thread reasoning effort: %s",
            item["last_error"],
            interval=300,
        )
        return True

    def mark_blocked(self, item, reason, now):
        thread_id = item.get("thread_id")
        existing = self.state.setdefault("blocked", {}).get(thread_id)
        if existing and existing.get("turn_id") == item.get("turn_id"):
            return False
        self.clear_reasoning_state(thread_id, include_restore=True)
        self.state.setdefault("blocked", {})[thread_id] = {
            "thread_id": thread_id,
            "turn_id": item.get("turn_id"),
            "title": item.get("title") or thread_id,
            "reason": reason,
            "blocked_at": now,
        }
        self.set_event("需要人工处理：{}（{}）".format(item.get("title") or thread_id, reason), now)
        if self.config.get("notify"):
            notify("Codex 熔断续聊", "{}：{}".format(reason, item.get("title") or thread_id))
        return True

    def record_completed_recovery(self, item, current, now):
        if current.get("state") != "complete":
            return None
        completed_turn_id = current.get("turn_id")
        if not completed_turn_id:
            return None
        attributed = self.state.setdefault("recovery_attributions", {})
        existing = attributed.get(completed_turn_id)
        if existing:
            return existing.get("kind")
        marker = str(item.get("resume_marker") or "")
        marker_text = "[Codex熔断续聊:{}]".format(marker) if marker else ""
        messages = current.get("user_messages")
        if not isinstance(messages, list):
            messages = [current.get("user_message")]
        if marker_text and any(marker_text in str(message or "") for message in messages):
            kind = "auto"
        elif completed_turn_id != item.get("turn_id"):
            kind = "manual"
        else:
            kind = "self"
        attributed[completed_turn_id] = {
            "kind": kind,
            "thread_id": item.get("thread_id"),
            "recorded_at": now,
        }
        title = item.get("title") or item.get("thread_id")
        if kind == "auto":
            self.state["resume_success_count"] = int(self.state.get("resume_success_count", 0)) + 1
            self.state["last_success_at"] = now
            self.state["last_success_title"] = title
            self.set_event("自动续接成功：{} ({})".format(title, item.get("thread_id")), now)
        elif kind == "manual":
            self.state["manual_recovery_count"] = int(self.state.get("manual_recovery_count", 0)) + 1
            self.state["last_manual_recovery_at"] = now
            self.state["last_manual_recovery_title"] = title
            self.set_event("检测到你手动继续后完成：{} ({})".format(title, item.get("thread_id")), now)
        else:
            self.set_event("任务已自行完成，取消无人值守重试：{}".format(title), now)
        return kind

    def finish_inflight(self, item, result, now):
        thread_id = item.get("thread_id")
        turn_id = item.get("turn_id")
        self.state.setdefault("inflight", {}).pop(thread_id, None)
        bypass_provider_id = item.get("temporary_bypass_provider_id")
        if bypass_provider_id:
            restore_temporary_failover_bypasses(
                self.config, self.state, provider_id=bypass_provider_id, now=now
            )
        rollout = Path(item.get("rollout_path") or "")
        current = latest_turn_status(rollout) if rollout.is_file() else {
            "state": "unknown", "turn_id": turn_id, "error": None
        }

        if current.get("state") == "complete":
            self.state.setdefault("retry_attempts", {}).pop(thread_id, None)
            self.state.setdefault("launcher_failures", {}).pop(thread_id, None)
            self.state.setdefault("blocked", {}).pop(thread_id, None)
            self.clear_reasoning_state(thread_id)
            self.record_completed_recovery(item, current, now)
            return

        if current.get("turn_id") != turn_id:
            if current.get("state") == "interrupted":
                record = {
                    "title": item.get("title"),
                    "cwd": item.get("cwd"),
                    "path": item.get("rollout_path"),
                    "reasoning_effort": item.get("reasoning_effort"),
                }
                self.schedule_retry(thread_id, current, record, now, "resume_failed_turn")
                self.set_event("续接后的新 turn 再次中断，已继续守候：{}".format(item.get("title")), now)
            elif current.get("state") == "failed_nonretryable":
                self.clear_reasoning_state(thread_id)
                error_text = json.dumps(current.get("error") or {}, ensure_ascii=False)
                self.mark_blocked(item, blocking_reason(error_text) or "Codex 返回不可重试错误", now)
            else:
                self.clear_reasoning_state(thread_id)
                self.set_event("检测到新的 turn，等待其自行完成：{}".format(item.get("title")), now)
            return

        run_output = tail_text(item.get("run_log") or "")
        reason = blocking_reason(run_output)
        failures = self.state.setdefault("launcher_failures", {})
        failures[thread_id] = int(failures.get(thread_id, 0)) + 1
        if reason:
            self.mark_blocked(item, reason, now)
            return
        unknown_limit = int(self.config.get("launcher_unknown_failure_limit", 3))
        if not retryable_text(run_output) and failures[thread_id] >= unknown_limit:
            self.mark_blocked(item, "续接进程连续异常退出", now)
            return

        self.state.setdefault("resumed_turns", {}).pop(turn_id, None)
        retry_status = dict(current)
        retry_status["error"] = {
            "message": "resume process exited with code {}".format(result if result is not None else "unknown")
        }
        record = {
            "title": item.get("title"),
            "cwd": item.get("cwd"),
            "path": item.get("rollout_path"),
            "reasoning_effort": item.get("reasoning_effort"),
        }
        if self.schedule_retry(thread_id, retry_status, record, now, "resume_process_exit"):
            self.set_event("续接进程未完成，已重新排队：{}".format(item.get("title")), now)

    def reconcile_inflight(self, now):
        inflight = self.state.setdefault("inflight", {})
        grace = max(1, int(self.config.get("resume_exit_grace_seconds", 10)))
        changed = False
        for thread_id, item in list(inflight.items()):
            pid = item.get("pid")
            if not pid:
                pid = find_resume_pid(thread_id)
                if pid:
                    item["pid"] = pid
                    changed = True
            if pid and process_matches_resume(pid, thread_id):
                continue
            if pid:
                item["pid"] = None
                changed = True
            if not item.get("exited_at"):
                item["exited_at"] = now
                changed = True
                continue
            if now - int(item.get("exited_at") or now) < grace:
                continue
            self.finish_inflight(item, item.get("exit_code"), now)
            changed = True
        if changed:
            save_state(self.state)

    def reconcile_blocked_tasks(self, now):
        """Drop manual-action warnings once the blocked turn is no longer current.

        A login or approval failure is deliberately sticky while that exact turn
        remains unresolved.  Without this reconciliation, however, a user who
        later continues the same thread successfully keeps seeing a red badge
        for an obsolete failure forever.
        """
        blocked = self.state.setdefault("blocked", {})
        if not blocked:
            return False
        records = thread_paths_from_db(self.config, set(blocked))
        cleared = 0
        for thread_id, item in list(blocked.items()):
            record = records.get(thread_id, {})
            path_text = record.get("path") or fallback_thread_path(self.config, thread_id)
            if not path_text:
                continue
            status = latest_turn_status(Path(path_text))
            blocked_turn_id = item.get("turn_id")
            if not blocked_turn_id or status.get("turn_id") == blocked_turn_id:
                continue
            blocked.pop(thread_id, None)
            self.state.setdefault("retry_attempts", {}).pop(thread_id, None)
            self.state.setdefault("launcher_failures", {}).pop(thread_id, None)
            cleared += 1
        if cleared:
            self.set_event("已清除 {} 条过期的人工处理提示".format(cleared), now)
        return bool(cleared)

    def reconcile_circuit(self, now):
        if self.state.get("phase") != "circuit_open":
            return
        incident = self.state.get("incident") or {}
        opened_at = int(incident.get("opened_at") or 0)
        wait = max(10, int(self.config.get("circuit_reconcile_seconds", 30)))
        if not opened_at or now - opened_at < wait:
            return
        success_at = latest_success_after(self.config, opened_at)
        if success_at:
            self.handle_closed(success_at, "由 CC Switch 成功请求确认线路恢复")

    def expedite_retries_after_recovery(self, now):
        scheduled = self.state.get("scheduled_retries") or []
        if not scheduled:
            return False
        interval = max(5, int(self.config.get("retry_recovery_probe_seconds", 15)))
        last_check = int(self.state.get("last_retry_recovery_check_at") or 0)
        if now - last_check < interval:
            return False
        self.state["last_retry_recovery_check_at"] = now
        detected = [int(item.get("detected_at") or 0) for item in scheduled if item.get("detected_at")]
        if not detected:
            return False
        success_at = latest_success_after(self.config, min(detected))
        if not success_at:
            return False
        grace = max(5, int(self.config.get("retry_recovery_grace_seconds", 20)))
        wake_at = now + grace
        changed = False
        for item in scheduled:
            if item.get("server_hint_seconds"):
                continue
            if success_at < int(item.get("detected_at") or 0):
                continue
            if int(item.get("not_before") or 0) > wake_at:
                item["not_before"] = wake_at
                item["recovery_detected_at"] = success_at
                changed = True
        if changed:
            self.set_event("检测到线路已恢复，待重试任务将在 {} 秒保护期后提前继续".format(grace), now)
        return changed

    def maintenance(self, now):
        last_cleanup = int(self.state.get("last_cleanup_at") or 0)
        if now - last_cleanup < 3600:
            return
        self.state["last_cleanup_at"] = now
        cutoff = now - max(86400, int(float(self.config.get("model_retry_watch_hours", 24)) * 3600))
        resumed = self.state.setdefault("resumed_turns", {})
        for turn_id, item in list(resumed.items()):
            if int(item.get("resumed_at") or 0) < cutoff:
                resumed.pop(turn_id, None)
        blocked = self.state.setdefault("blocked", {})
        for thread_id, item in list(blocked.items()):
            if int(item.get("blocked_at") or 0) < cutoff:
                blocked.pop(thread_id, None)
        attributed = self.state.setdefault("recovery_attributions", {})
        for turn_id, item in list(attributed.items()):
            if int(item.get("recorded_at") or 0) < cutoff:
                attributed.pop(turn_id, None)
        cleanup_run_logs(self.config, now)

    def handle_open(self, when, line=""):
        if self.state.get("phase") == "circuit_open":
            return
        self.state["phase"] = "circuit_open"
        self.state["incident"] = {
            "opened_at": int(when),
            "recovered_at": None,
            "source_line": line.strip()[-500:],
        }
        self.set_event("检测到 CC Switch 熔断，开始记录受影响任务", when)
        save_state(self.state)

    def handle_closed(self, when, line=""):
        incident = self.state.get("incident")
        if not incident or self.state.get("phase") != "circuit_open":
            return
        incident["recovered_at"] = int(when)
        incident["recovery_line"] = line.strip()[-500:]
        self.state["phase"] = "recovery_grace"
        self.set_event("CC Switch 已恢复，等待任务自身重试结束", when)
        save_state(self.state)

    def process_log_line(self, line):
        kind = event_kind(line)
        if kind == "open":
            self.handle_open(line_epoch(line), line)
        elif kind == "closed":
            self.handle_closed(line_epoch(line), line)

    def initialize_log_cursor(self):
        path = Path(self.config["cc_switch_log"])
        try:
            stat = path.stat()
        except OSError as exc:
            self.set_event("等待 CC Switch 日志: {}".format(exc))
            save_state(self.state)
            return

        if self.state.get("log_inode") == stat.st_ino and self.state.get("log_offset", 0) <= stat.st_size:
            return

        self.state["log_inode"] = stat.st_ino
        self.state["log_offset"] = stat.st_size
        # Reconstruct only the current breaker state; do not replay old incidents.
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, stat.st_size - 512 * 1024))
                if handle.tell():
                    handle.readline()
                last_transition = None
                for raw in handle:
                    line = raw.decode("utf-8", "replace")
                    kind = event_kind(line)
                    if kind:
                        last_transition = (kind, line)
            if last_transition and last_transition[0] == "open" and self.state.get("phase") == "idle":
                self.handle_open(line_epoch(last_transition[1]), last_transition[1])
        except OSError:
            pass
        save_state(self.state)

    def read_new_log_lines(self):
        path = Path(self.config["cc_switch_log"])
        try:
            stat = path.stat()
        except OSError:
            return
        if self.state.get("log_inode") != stat.st_ino or self.state.get("log_offset", 0) > stat.st_size:
            self.state["log_inode"] = stat.st_ino
            self.state["log_offset"] = 0
        try:
            with path.open("rb") as handle:
                handle.seek(int(self.state.get("log_offset", 0)))
                for raw in handle:
                    self.process_log_line(raw.decode("utf-8", "replace"))
                self.state["log_offset"] = handle.tell()
        except OSError as exc:
            logging.warning("cannot read CC Switch log: %s", exc)

    def prepare_queue_if_ready(self, now):
        if self.state.get("phase") != "recovery_grace":
            return
        incident = self.state.get("incident") or {}
        recovered_at = incident.get("recovered_at")
        if not recovered_at or now < recovered_at + int(self.config["recovery_grace_seconds"]):
            return
        candidates, pending = build_candidates(
            self.config, incident, self.state, now, include_pending=True
        )
        carryover = list(self.state.get("queue") or []) + list(
            self.state.get("candidate_backlog") or []
        )
        merged_candidates = []
        seen = set()
        for item in carryover + candidates:
            key = (item.get("thread_id"), item.get("turn_id"))
            if key in seen:
                continue
            seen.add(key)
            merged_candidates.append(item)
        candidates = merged_candidates
        batch_size = max(1, int(self.config.get("max_candidates_per_incident", 8)))
        self.state["queue"] = candidates[:batch_size]
        self.state["candidate_backlog"] = candidates[batch_size:]
        if self.state["queue"]:
            self.state["phase"] = "resuming"
            total = len(candidates)
            queued = len(self.state["queue"])
            remaining = len(self.state["candidate_backlog"])
            message = "找到 {} 条中断任务，开始安全续接".format(total)
            if remaining:
                message = "找到 {} 条中断任务，先续接 {} 条，另有 {} 条已安全排队".format(
                    total, queued, remaining
                )
            self.set_event(message, now)
        elif pending:
            self.set_event(
                "线路已恢复，继续观察 {} 条尚未结束的任务".format(len(pending)), now
            )
        else:
            self.state["phase"] = "idle"
            self.state["incident"] = None
            self.set_event("线路已恢复，没有需要续接的中断任务", now)
        save_state(self.state)

    def reap_processes(self):
        for pid, item in list(self.processes.items()):
            process = item["process"]
            result = process.poll()
            if result is None:
                continue
            item["log_handle"].close()
            del self.processes[pid]
            persisted = self.state.setdefault("inflight", {}).get(item["thread_id"], item)
            persisted["exit_code"] = result
            persisted["exited_at"] = int(time.time())
            self.set_event(
                "续接进程已退出，核对 turn 结果：{} ({})，退出码 {}".format(
                    item["title"], item["thread_id"], result
                )
            )
            save_state(self.state)

    def retry_delay(self, thread_id, error_text=""):
        """Return a bounded exponential delay with a small, persisted jitter.

        This is deliberately the equivalent of Tenacity's
        ``wait_exponential + wait_random`` pattern rather than a fixed retry
        cadence.  Multiple interrupted Codex sessions therefore do not hammer
        the same low-rate relay at exactly the same second.
        """
        attempts = int(self.state.get("retry_attempts", {}).get(thread_id, 0))
        base = max(1, int(self.config.get("model_retry_base_seconds", 60)))
        maximum = max(base, int(self.config.get("model_retry_max_seconds", 900)))
        exponential = min(maximum, base * (2 ** min(attempts, 8)))
        server_hint = 0
        if self.config.get("model_retry_respect_server_hint", True):
            server_hint = retry_after_seconds(
                error_text,
                self.config.get("model_retry_server_hint_max_seconds", 3600),
            )
        target = max(exponential, server_hint)
        jitter_limit = max(0, int(self.config.get("model_retry_jitter_seconds", 15)))
        # Never exceed the configured normal ceiling solely because of jitter.
        jitter = random.randint(0, min(jitter_limit, max(0, maximum - target))) if target <= maximum else 0
        return target + jitter, exponential, server_hint, jitter

    def reasoning_for_retry(self, thread_id, record):
        remembered = normalize_reasoning_effort(
            self.state.setdefault("reasoning_efforts", {}).get(thread_id)
        )
        if remembered:
            return remembered
        item_effort = normalize_reasoning_effort((record or {}).get("reasoning_effort"))
        if item_effort:
            return item_effort
        record_effort = normalize_reasoning_effort((record or {}).get("db_reasoning_effort"))
        if record_effort:
            return record_effort
        return configured_reasoning_effort(self.config)

    def prepare_reasoning_retry(self, thread_id, record, error_text, now):
        """Choose a per-run effort override after capacity, or probe promotion later."""
        if not self.config.get("capacity_reasoning_fallback_enabled", True):
            return None, None
        current = self.reasoning_for_retry(thread_id, record)
        configured = configured_reasoning_effort(self.config)
        record_effort = normalize_reasoning_effort(
            (record or {}).get("reasoning_effort") or (record or {}).get("db_reasoning_effort")
        )
        original = normalize_reasoning_effort(
            self.state.setdefault("reasoning_originals", {}).get(thread_id)
        )
        if not original:
            # Prefer the actual thread/config effort over a remembered lower
            # fallback when recovering an older state file without originals.
            original = record_effort or configured or current
        if not original:
            return None, None
        self.state.setdefault("reasoning_originals", {})[thread_id] = original

        if is_model_capacity_error(error_text):
            current = current or original
            minimum = self.config.get("capacity_reasoning_minimum", "low")
            minimum = normalize_reasoning_effort(minimum) or "low"
            last_capacity_map = self.state.setdefault("reasoning_last_capacity_at", {})
            fallback_since = int(last_capacity_map.get(thread_id) or now)
            last_capacity_map.setdefault(thread_id, now)
            cooldown = max(60, int(self.config.get("capacity_reasoning_promote_after_seconds", 900)))
            if (
                current == minimum
                and current != original
                and self.config.get("capacity_reasoning_promote_enabled", True)
                and now - fallback_since >= cooldown
            ):
                self.state.setdefault("reasoning_efforts", {})[thread_id] = original
                last_capacity_map[thread_id] = now
                return original, {
                    "fallback": False,
                    "from": current,
                    "promotion": True,
                }
            next_effort = lower_reasoning_effort(current, minimum)
            self.state.setdefault("reasoning_efforts", {})[thread_id] = next_effort
            if current != next_effort:
                self.remember_reasoning_restore(thread_id, record, original, next_effort, now)
            return next_effort, {
                "fallback": next_effort != current,
                "from": current,
                "promotion": False,
            }

        # Once the configured cooldown has elapsed, retry at the original effort
        # to test whether the provider's high-capacity pool has recovered.
        current = normalize_reasoning_effort(
            self.state.setdefault("reasoning_efforts", {}).get(thread_id)
        )
        if (
            current
            and current != original
            and self.config.get("capacity_reasoning_promote_enabled", True)
        ):
            last_capacity = int(self.state.setdefault("reasoning_last_capacity_at", {}).get(thread_id) or 0)
            cooldown = max(60, int(self.config.get("capacity_reasoning_promote_after_seconds", 900)))
            if now - last_capacity >= cooldown:
                self.state["reasoning_efforts"][thread_id] = original
                self.state["reasoning_last_capacity_at"][thread_id] = now
                return original, {
                    "fallback": False,
                    "from": current,
                    "promotion": True,
                }
        return current, None

    def schedule_retry(self, thread_id, status, record, now, source):
        turn_id = status.get("turn_id")
        if not turn_id or turn_id in self.state.get("resumed_turns", {}):
            return False
        if self.is_inflight(thread_id=thread_id, turn_id=turn_id):
            return False
        blocked = self.state.setdefault("blocked", {})
        blocked_item = blocked.get(thread_id)
        if blocked_item and blocked_item.get("turn_id") == turn_id:
            return False
        if blocked_item:
            blocked.pop(thread_id, None)
        scheduled = self.state.setdefault("scheduled_retries", [])
        if any(item.get("turn_id") == turn_id for item in scheduled):
            return False
        attempts = int(self.state.setdefault("retry_attempts", {}).get(thread_id, 0))
        maximum = int(self.config.get("model_retry_max_attempts", 20))
        until_success = bool(self.config.get("model_retry_until_success", True))
        if not until_success and attempts >= maximum:
            self.set_event("无人值守重试已达上限：{}".format(record.get("title") or thread_id), now)
            return False
        error = status.get("error") or {}
        error_text = (
            json.dumps(error, ensure_ascii=False)
            if isinstance(error, dict)
            else str(error)
        )
        reasoning_effort, reasoning_meta = self.prepare_reasoning_retry(
            thread_id, record, error_text, now
        )
        delay, exponential_delay, server_hint, jitter = self.retry_delay(thread_id, error_text)
        scheduled.append(
            {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "title": record.get("title") or thread_id,
                "cwd": record.get("cwd") or str(Path.home()),
                "rollout_path": str(record.get("path") or ""),
                "detected_at": now,
                "not_before": now + delay,
                "source": source,
                "error": str(error_text or "任务长时间无响应")[:240],
                "attempt": attempts + 1,
                "delay_strategy": "指数退避+随机错峰",
                "exponential_delay_seconds": exponential_delay,
                "server_hint_seconds": server_hint or None,
                "jitter_seconds": jitter,
                "gateway_failover_bypass": retryable_gateway_error(error_text),
            }
        )
        if reasoning_effort:
            scheduled[-1]["reasoning_effort"] = reasoning_effort
        if reasoning_meta:
            scheduled[-1]["reasoning_fallback"] = bool(reasoning_meta.get("fallback"))
            scheduled[-1]["reasoning_fallback_from"] = reasoning_meta.get("from")
            scheduled[-1]["reasoning_promotion"] = bool(reasoning_meta.get("promotion"))
        delay_note = ""
        if server_hint:
            delay_note = "（已遵守上游建议等待 {} 秒）".format(server_hint)
        if reasoning_meta and reasoning_meta.get("fallback"):
            delay_note += "；模型满载，已降为“{}”".format(reasoning_effort_label(reasoning_effort))
        elif reasoning_meta and reasoning_meta.get("promotion"):
            delay_note += "；正在重新尝试“{}”档位".format(reasoning_effort_label(reasoning_effort))
        self.set_event(
            "已安排无人值守重试：{}，{} 秒后第 {} 次尝试{}".format(
                record.get("title") or thread_id, delay, attempts + 1, delay_note
            ),
            now,
        )
        return True

    def discover_retryable_failures(self, now):
        if not self.config.get("model_retry_enabled", True):
            return
        # Avoid competing with the circuit recovery flow. Retry discovery can
        # continue while another task is being resumed; launch_next enforces
        # the configured concurrency limit.
        if self.state.get("phase") in {"circuit_open", "recovery_grace"}:
            return
        scan_interval = max(2, int(self.config.get("retry_error_scan_seconds", 10)))
        last_error_scan = int(self.state.get("last_error_scan_at") or 0)
        if now - last_error_scan < scan_interval:
            return
        cursor = int(self.state.get("retry_scan_cursor") or now)
        watch_seconds = max(1, int(float(self.config.get("model_retry_watch_hours", 12)) * 3600))
        scan_floor = max(cursor - 2, now - watch_seconds)
        sessions_dir = self.config["codex_sessions_dir"]
        ids = set()
        pattern = os.path.join(sessions_dir, "**", "rollout-*.jsonl")
        for path in glob.iglob(pattern, recursive=True):
            try:
                modified = int(os.path.getmtime(path))
            except OSError:
                continue
            if modified < scan_floor:
                continue
            match = re.search(r"(01[0-9a-f]{2}[0-9a-f-]{32})\.jsonl$", path, re.IGNORECASE)
            if match:
                ids.add(match.group(1))
        records = thread_paths_from_db(self.config, ids)
        changed = False
        for thread_id in ids:
            record = records.get(thread_id, {})
            path_text = record.get("path") or fallback_thread_path(self.config, thread_id)
            if not path_text:
                continue
            record["path"] = path_text
            changed = self.discover_reasoning_restore_candidate(thread_id, record, now) or changed
            status = latest_turn_status(Path(path_text))
            completed_at = as_epoch(status.get("completed_at"), 0) or 0
            if status.get("state") == "interrupted" and completed_at >= scan_floor:
                changed = self.schedule_retry(thread_id, status, record, now, "retryable_error") or changed
            elif status.get("state") == "failed_nonretryable" and completed_at >= scan_floor:
                error_text = json.dumps(status.get("error") or {}, ensure_ascii=False)
                reason = blocking_reason(error_text)
                if reason:
                    item = {
                        "thread_id": thread_id,
                        "turn_id": status.get("turn_id"),
                        "title": record.get("title") or thread_id,
                    }
                    changed = self.mark_blocked(item, reason, now) or changed
        self.state["retry_scan_cursor"] = now
        self.state["last_error_scan_at"] = now

        # A dropped stream may never write task_complete. Only watch turns that began
        # after this daemon version started, and require the full idle protection time.
        last_stall_scan = int(self.state.get("last_retry_scan_at") or 0)
        if now - last_stall_scan >= 60:
            self.state["last_retry_scan_at"] = now
            watch_started = int(self.state.get("watch_started_at") or now)
            idle_seconds = int(self.config.get("active_thread_idle_seconds", 240))
            recent_ids = set()
            for path in glob.iglob(pattern, recursive=True):
                try:
                    modified = int(os.path.getmtime(path))
                except OSError:
                    continue
                if modified > now - idle_seconds or modified < watch_started:
                    continue
                match = re.search(r"(01[0-9a-f]{2}[0-9a-f-]{32})\.jsonl$", path, re.IGNORECASE)
                if match:
                    recent_ids.add(match.group(1))
            recent_records = thread_paths_from_db(self.config, recent_ids)
            for thread_id in recent_ids:
                record = recent_records.get(thread_id, {})
                path_text = record.get("path") or fallback_thread_path(self.config, thread_id)
                if not path_text:
                    continue
                record["path"] = path_text
                status = latest_turn_status(Path(path_text))
                started_at = as_epoch(status.get("started_at"), 0) or 0
                if status.get("state") == "active" and started_at >= watch_started:
                    changed = self.schedule_retry(thread_id, status, record, now, "stalled_turn") or changed
        if changed:
            save_state(self.state)

    def promote_due_retries(self, now):
        # Don't compete with circuit-breaker recovery flow.
        if self.state.get("phase") in {"circuit_open", "recovery_grace"}:
            return
        inflight = self.state.setdefault("inflight", {})
        max_parallel = max(1, int(self.config.get("max_parallel_resumes", 1)))
        if len(inflight) >= max_parallel:
            return
        scheduled = self.state.get("scheduled_retries") or []
        due = [item for item in scheduled if int(item.get("not_before") or 0) <= now]
        if not due:
            return
        due.sort(key=lambda item: int(item.get("not_before") or 0))
        item = due[0]
        scheduled.remove(item)
        rollout = Path(item.get("rollout_path") or "")
        if not rollout.is_file():
            self.set_event("跳过无效 rollout：{}".format(item.get("title")), now)
            save_state(self.state)
            return
        current = latest_turn_status(rollout)
        if current.get("state") == "complete":
            self.state.setdefault("retry_attempts", {}).pop(item.get("thread_id"), None)
            self.state.setdefault("reasoning_efforts", {}).pop(item.get("thread_id"), None)
            self.state.setdefault("reasoning_originals", {}).pop(item.get("thread_id"), None)
            self.state.setdefault("reasoning_last_capacity_at", {}).pop(item.get("thread_id"), None)
            self.record_completed_recovery(item, current, now)
            save_state(self.state)
            return
        if current.get("turn_id") != item.get("turn_id"):
            self.state.setdefault("reasoning_efforts", {}).pop(item.get("thread_id"), None)
            self.state.setdefault("reasoning_originals", {}).pop(item.get("thread_id"), None)
            self.state.setdefault("reasoning_last_capacity_at", {}).pop(item.get("thread_id"), None)
            self.set_event("跳过已自行恢复的任务：{}".format(item.get("title")), now)
            save_state(self.state)
            return
        if current.get("state") == "active":
            stale_active = False
            if item.get("source") == "stalled_turn":
                idle_seconds = int(self.config.get("active_thread_idle_seconds", 240))
                try:
                    modified_at = int(rollout.stat().st_mtime)
                except OSError:
                    modified_at = now
                stale_active = modified_at <= now - idle_seconds
            if not stale_active:
                # User or another resume is already running; re-check later.
                item["not_before"] = now + int(self.config.get("model_retry_base_seconds", 60))
                scheduled.append(item)
                self.set_event("任务仍在执行，延后重试：{}".format(item.get("title")), now)
                save_state(self.state)
                return

        queue = self.state.setdefault("queue", [])
        if any(existing.get("thread_id") == item.get("thread_id") for existing in queue):
            save_state(self.state)
            return
        queue.append(item)
        self.state["phase"] = "resuming"
        self.set_event("无人值守重试到点：{}".format(item.get("title")), now)
        save_state(self.state)

    def refresh_provider_snapshot(self, now):
        last_snapshot = int(self.state.get("last_provider_snapshot_at") or 0)
        snapshot_interval = int(self.config.get("provider_snapshot_seconds", 15))
        if now - last_snapshot < snapshot_interval:
            return
        last_probe = int(self.state.get("last_balance_probe_at") or 0)
        probe_interval = int(self.config.get("balance_probe_seconds", 900))
        should_probe_balance = now - last_probe >= probe_interval
        last_billing_probe = int(self.state.get("last_billing_probe_at") or 0)
        billing_probe_interval = int(self.config.get("billing_probe_seconds", 300))
        should_probe_billing = now - last_billing_probe >= billing_probe_interval
        last_exchange_probe = int(self.state.get("last_exchange_rate_probe_at") or 0)
        exchange_probe_interval = int(self.config.get("exchange_rate_probe_seconds", 21600))
        should_probe_exchange = now - last_exchange_probe >= exchange_probe_interval
        try:
            write_provider_snapshot(
                self.config,
                self.state,
                probe_balances=should_probe_balance,
                probe_billing=should_probe_billing,
                probe_exchange=should_probe_exchange,
                now=now,
            )
            self.state["last_provider_snapshot_at"] = now
            if should_probe_balance:
                self.state["last_balance_probe_at"] = now
            if should_probe_billing:
                self.state["last_billing_probe_at"] = now
            if should_probe_exchange:
                self.state["last_exchange_rate_probe_at"] = now
        except Exception as exc:
            logging.warning("cannot refresh provider snapshot: %s", exc)

    def reconcile_proxy_route(self, now):
        last_check = int(self.state.get("proxy_route_checked_at") or 0)
        interval = max(10, int(self.config.get("codex_proxy_route_check_seconds", 30)))
        if now - last_check < interval:
            return
        previous = self.state.get("proxy_route_status")
        result = ensure_codex_proxy_routing(self.config)
        status = result.get("status") or "error"
        detail = result.get("detail") or "Codex 路由状态未知"
        self.state["proxy_route_status"] = status
        self.state["proxy_route_detail"] = detail
        self.state["proxy_route_checked_at"] = now
        if status == "repaired":
            logging.info(detail)
        elif status in {"unavailable", "error"} and status != previous:
            logging.warning(detail)

    def launch_next(self, now):
        if self.state.get("phase") != "resuming":
            return
        inflight = self.state.setdefault("inflight", {})
        if len(inflight) >= int(self.config["max_parallel_resumes"]):
            return
        queue = self.state.get("queue") or []
        if not queue:
            backlog = self.state.setdefault("candidate_backlog", [])
            if backlog and not inflight:
                batch_size = max(1, int(self.config.get("max_candidates_per_incident", 8)))
                self.state["queue"] = backlog[:batch_size]
                self.state["candidate_backlog"] = backlog[batch_size:]
                queue = self.state["queue"]
                self.set_event(
                    "继续处理剩余中断任务：本批 {} 条，尚余 {} 条".format(
                        len(queue), len(self.state["candidate_backlog"])
                    ),
                    now,
                )
            if queue:
                save_state(self.state)
            elif not inflight:
                self.state["phase"] = "idle"
                self.state["incident"] = None
                self.set_event("本轮中断任务已全部处理", now)
                save_state(self.state)
            if not queue:
                return

        if not self.dry_run:
            retry_at = int(self.state.get("proxy_route_retry_at") or 0)
            if now < retry_at:
                return
            route_result = ensure_codex_proxy_routing(self.config)
            route_status = route_result.get("status") or "error"
            route_detail = route_result.get("detail") or "Codex 路由状态未知"
            self.state["proxy_route_status"] = route_status
            self.state["proxy_route_detail"] = route_detail
            self.state["proxy_route_checked_at"] = now
            if route_status in {"unavailable", "error"}:
                self.state["proxy_route_retry_at"] = now + max(
                    5, int(self.config.get("codex_proxy_route_retry_seconds", 15))
                )
                self.set_event("续接暂缓：{}".format(route_detail), now)
                save_state(self.state)
                return
            self.state["proxy_route_retry_at"] = 0

        item = queue.pop(0)
        remaining_queue = list(queue)
        current = latest_turn_status(Path(item["rollout_path"]))
        if current.get("turn_id") != item["turn_id"] or current.get("state") == "complete":
            self.state.setdefault("reasoning_efforts", {}).pop(item.get("thread_id"), None)
            self.state.setdefault("reasoning_originals", {}).pop(item.get("thread_id"), None)
            self.state.setdefault("reasoning_last_capacity_at", {}).pop(item.get("thread_id"), None)
            # Do not discard reasoning_restore_pending here. A user may have
            # continued the failed high-effort turn manually at medium; the
            # idle Desktop sync still needs to restore the original tier.
            if current.get("state") == "complete":
                self.record_completed_recovery(item, current, now)
            else:
                self.set_event("跳过已被手动恢复的任务：{}".format(item["title"]), now)
            save_state(self.state)
            return

        if self.dry_run:
            self.set_event("DRY RUN：将续接 {}".format(item["title"]), now)
            save_state(self.state)
            return

        bypass = None
        if item.get("gateway_failover_bypass"):
            # Persist the popped item before the bypass mutation.  A crash in
            # the tiny window before Popen must replay the task, not lose it.
            self.state["queue"] = [item] + remaining_queue
            save_state(self.state)
            bypass = temporarily_bypass_first_codex_provider(
                self.config,
                self.state,
                now,
                "OpenResty/nginx HTML 400，续接时临时换到下一条线路",
            )
            if bypass:
                item["temporary_bypass_provider_id"] = bypass.get("provider_id")
                item["temporary_bypass_provider_name"] = bypass.get("provider_name")
            self.state["queue"] = remaining_queue

        timestamp = time.strftime("%Y%m%d-%H%M%S")
        run_log = RUN_LOG_DIR / "{}-{}.log".format(timestamp, item["thread_id"])
        launch_item = dict(item)
        resume_marker = "{}-{}-{:08x}".format(
            item["thread_id"][-8:], now, random.getrandbits(32)
        )
        launch_item.update(
            {
                "status": "launching",
                "pid": None,
                "started_at": now,
                "run_log": str(run_log),
                "exit_code": None,
                "exited_at": None,
                "resume_marker": resume_marker,
            }
        )
        inflight[item["thread_id"]] = launch_item
        save_state(self.state)

        log_handle = None
        command = codex_command(
            resolve_codex_binary(self.config),
            "exec",
            "--skip-git-repo-check",
            "-c",
            "features.prevent_idle_sleep=true",
        )
        reasoning_effort = normalize_reasoning_effort(item.get("reasoning_effort"))
        if reasoning_effort:
            command.extend(["-c", 'model_reasoning_effort="{}"'.format(reasoning_effort)])
        command.extend([
            "resume",
            item["thread_id"],
            "{}\n\n[Codex熔断续聊:{}]".format(self.config["resume_prompt"], resume_marker),
        ])
        child_env = os.environ.copy()
        path_parts = list(self.config.get("extra_path", []))
        if not IS_WINDOWS:
            path_parts.extend(["/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        child_env["PATH"] = os.pathsep.join(dict.fromkeys(path_parts))
        try:
            log_handle = run_log.open("ab")
            process = subprocess.Popen(
                command,
                cwd=item["cwd"] if os.path.isdir(item["cwd"]) else str(Path.home()),
                env=child_env,
                stdout=subprocess.DEVNULL,
                stderr=log_handle,
                start_new_session=True,
            )
        except OSError as exc:
            if log_handle is not None:
                log_handle.close()
            inflight.pop(item["thread_id"], None)
            self.state["queue"] = remaining_queue
            if bypass:
                restore_temporary_failover_bypasses(
                    self.config, self.state, provider_id=bypass.get("provider_id"), now=now
                )
            failures = self.state.setdefault("launcher_failures", {})
            failures[item["thread_id"]] = int(failures.get(item["thread_id"], 0)) + 1
            if failures[item["thread_id"]] >= int(self.config.get("launcher_unknown_failure_limit", 3)):
                self.mark_blocked(item, "无法启动 Codex CLI", now)
            else:
                retry_status = dict(current)
                retry_status["error"] = {"message": "launcher error: {}".format(exc)}
                record = {
                    "title": item.get("title"),
                    "cwd": item.get("cwd"),
                    "path": item.get("rollout_path"),
                    "reasoning_effort": item.get("reasoning_effort"),
                }
                self.schedule_retry(item["thread_id"], retry_status, record, now, "launcher_error")
                self.set_event("Codex CLI 启动失败，已延后重试：{}".format(item["title"]), now)
            save_state(self.state)
            return
        launch_item.update({"status": "running", "pid": process.pid})
        self.state.setdefault("resumed_turns", {})[item["turn_id"]] = {
            "thread_id": item["thread_id"],
            "resumed_at": now,
        }
        self.state["resume_count"] = int(self.state.get("resume_count", 0)) + 1
        thread_attempts = self.state.setdefault("retry_attempts", {})
        thread_attempts[item["thread_id"]] = int(thread_attempts.get(item["thread_id"], 0)) + 1
        self.processes[process.pid] = {
            "process": process,
            "log_handle": log_handle,
            **launch_item,
        }
        self.set_event("已发起自动续接，等待确认结果：{} ({})".format(item["title"], item["thread_id"]), now)
        if self.config.get("notify"):
            notify("Codex 熔断续聊", "线路恢复，已续接：{}".format(item["title"]))
        save_state(self.state)

    def tick(self):
        started = time.monotonic()
        tick_started_at = int(time.time())
        self.state["last_tick_at"] = tick_started_at
        self.state["heartbeat_at"] = tick_started_at
        self.read_new_log_lines()
        self.reap_processes()
        now = int(time.time())
        self.reconcile_proxy_route(now)
        restore_temporary_failover_bypasses(
            self.config, self.state, now=now, expired_only=True
        )
        self.reconcile_inflight(now)
        self.reconcile_blocked_tasks(now)
        self.reconcile_circuit(now)
        self.discover_retryable_failures(now)
        recovery_changed = self.expedite_retries_after_recovery(now)
        self.refresh_provider_snapshot(now)
        restore_changed = self.restore_due_desktop_reasoning(now)
        self.prepare_queue_if_ready(now)
        self.promote_due_retries(now)
        self.launch_next(now)
        self.maintenance(now)
        if restore_changed or recovery_changed:
            save_state(self.state)
        self.state["heartbeat_at"] = int(time.time())
        self.state["last_tick_duration_ms"] = int((time.monotonic() - started) * 1000)
        self.state["last_internal_error"] = ""
        self.checkpoint(self.state["heartbeat_at"])

    def run(self):
        try:
            self.initialize_log_cursor()
        except Exception as exc:
            logging.exception("initialization failed; watcher will retry")
            self.state["last_internal_error"] = "初始化异常：{}".format(redact_sensitive_text(exc)[:300])
            self.state["tick_failures_total"] = int(self.state.get("tick_failures_total", 0)) + 1
            save_state(self.state)
        while self.running:
            try:
                if PAUSE_PATH.exists():
                    now = int(time.time())
                    self.state["heartbeat_at"] = now
                    self.state["last_tick_at"] = now
                    self.state["last_tick_duration_ms"] = 0
                    self.checkpoint(now)
                else:
                    self.tick()
            except Exception as exc:
                logging.exception("watcher tick failed; continuing")
                now = int(time.time())
                self.state["heartbeat_at"] = now
                self.state["last_tick_at"] = now
                self.state["tick_failures_total"] = int(self.state.get("tick_failures_total", 0)) + 1
                self.state["last_internal_error"] = "{}: {}".format(type(exc).__name__, redact_sensitive_text(exc)[:300])
                self.state["last_event"] = "后台单轮异常，守望器已自动继续"
                self.state["last_event_at"] = now
                try:
                    save_state(self.state)
                except Exception:
                    logging.exception("cannot persist watcher error state")
            time.sleep(max(0.2, float(self.config["poll_seconds"])))
        restore_temporary_failover_bypasses(self.config, self.state)
        self.checkpoint(force=True)


def status_payload(config):
    state = load_state()
    now = int(time.time())
    heartbeat = int(state.get("heartbeat_at") or 0)
    heartbeat_age = max(0, now - heartbeat) if heartbeat else None
    stale_after = max(10, int(config.get("heartbeat_stale_seconds", 15)))
    resolved_binary = resolve_codex_binary(config)
    state["paused"] = PAUSE_PATH.exists()
    state["cc_switch_log_exists"] = os.path.exists(config["cc_switch_log"])
    state["cc_switch_db_exists"] = os.path.exists(config["cc_switch_db"])
    state["codex_binary"] = resolved_binary
    state["codex_binary_exists"] = bool(resolved_binary and os.path.isfile(resolved_binary) and os.access(resolved_binary, os.X_OK))
    state["scheduled_retry_count"] = len(state.get("scheduled_retries") or [])
    state["reasoning_restore_pending_count"] = len(state.get("reasoning_restore_pending") or {})
    state["inflight_count"] = len(state.get("inflight") or {})
    state["blocked_count"] = len(state.get("blocked") or {})
    state["heartbeat_age_seconds"] = heartbeat_age
    state["healthy"] = heartbeat_age is not None and heartbeat_age <= stale_after and not state.get("state_recovery_error")
    return state


def inspect_current(config):
    state = load_state()
    incident = state.get("incident")
    result = status_payload(config)
    if incident and incident.get("recovered_at"):
        candidates, pending = build_candidates(config, incident, state, include_pending=True)
        result["current_candidates"] = candidates
        result["pending_active_threads"] = pending
    else:
        result["current_candidates"] = []
        result["pending_active_threads"] = []
    return result


def sleep_check_payload(config):
    """Return a read-only checklist for leaving the Mac unattended overnight."""
    status = status_payload(config)
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    add("后台心跳", status.get("healthy") and not status.get("paused"),
        "正常" if status.get("healthy") and not status.get("paused") else "后台未运行、已暂停或心跳过期")
    add("CC Switch 日志", status.get("cc_switch_log_exists"), config.get("cc_switch_log"))
    add("CC Switch 数据库", status.get("cc_switch_db_exists"), config.get("cc_switch_db"))
    binary = status.get("codex_binary") or ""
    add("Codex CLI", status.get("codex_binary_exists"), binary or "未找到可执行文件")

    login_ok = False
    login_detail = "未检查（Codex CLI 不可用）"
    if status.get("codex_binary_exists"):
        try:
            result = subprocess.run(
                codex_command(binary, "login", "status"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=12, check=False,
            )
            login_ok = result.returncode == 0
            output = redact_sensitive_text(result.stdout).strip().replace("\n", " ")
            login_detail = output[:180] if output else ("已登录" if login_ok else "登录状态检查失败")
        except (OSError, subprocess.SubprocessError) as exc:
            login_detail = redact_sensitive_text(exc)[:180]
    add("Codex 登录", login_ok, login_detail)

    power_ok = False
    power_detail = "无法读取供电状态"
    if IS_WINDOWS:
        power_ok = True
        power_detail = "Windows 电源状态由系统电源计划管理；请关闭自动睡眠"
    else:
        try:
            result = subprocess.run(
                ["/usr/bin/pmset", "-g", "batt"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=5, check=False,
            )
            power_ok = "AC Power" in result.stdout
            power_detail = "已接电；请保持开盖" if power_ok else "未接电；睡眠后无法自动续接"
        except (OSError, subprocess.SubprocessError):
            pass
    add("整夜供电", power_ok, power_detail)

    return {"ready": all(item["ok"] for item in checks), "checks": checks, "status": status}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="print current state as JSON")
    parser.add_argument("--inspect", action="store_true", help="inspect candidates without resuming")
    parser.add_argument("--once", action="store_true", help="run one watcher tick")
    parser.add_argument("--dry-run", action="store_true", help="never launch codex resume")
    parser.add_argument("--providers", action="store_true", help="print the current provider snapshot")
    parser.add_argument("--probe-balances", action="store_true", help="refresh supported provider balances")
    parser.add_argument("--probe-billing", action="store_true", help="refresh key-scoped billing multipliers")
    parser.add_argument("--probe-exchange", action="store_true", help="refresh the cached USD/CNY exchange rate")
    parser.add_argument("--sleep-check", action="store_true", help="run read-only overnight readiness checks")
    return parser.parse_args(argv)


def configure_standard_streams():
    if not IS_WINDOWS:
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv=None):
    configure_standard_streams()
    args = parse_args(argv)
    setup_logging()
    config = load_config()
    if args.status:
        print(json.dumps(status_payload(config), ensure_ascii=False, indent=2))
        return 0
    if args.inspect:
        print(json.dumps(inspect_current(config), ensure_ascii=False, indent=2))
        return 0
    if args.sleep_check:
        print(json.dumps(sleep_check_payload(config), ensure_ascii=False, indent=2))
        return 0
    if args.providers or args.probe_balances or args.probe_billing or args.probe_exchange:
        state = load_state()
        snapshot = write_provider_snapshot(
            config,
            state,
            probe_balances=args.probe_balances,
            probe_billing=args.probe_billing,
            probe_exchange=args.probe_exchange,
        )
        if args.probe_balances or args.probe_billing or args.probe_exchange:
            save_state(state)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0
    watcher = Watcher(config, dry_run=args.dry_run)
    signal.signal(signal.SIGTERM, watcher.stop)
    signal.signal(signal.SIGINT, watcher.stop)
    if args.once:
        watcher.initialize_log_cursor()
        watcher.tick()
        return 0
    watcher.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
