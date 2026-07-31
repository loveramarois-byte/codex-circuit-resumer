#!/usr/bin/env python3
"""Run 50 repeatable macOS drills against the packaged Codex watcher.

These are process-level drills, not mocked unit tests: every case starts the
real daemon or app executable, uses real files/SQLite, and validates the
persisted result.  All mutation is confined to temporary directories except
for five read-only checks against the already installed watcher/CC Switch DB.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
DAEMON = PROJECT / "src" / "daemon.py"
DEFAULT_APP = PROJECT.parent / "outputs" / "Codex熔断续聊.app"
THREAD_ID = "019fb64d-d041-7842-adbf-01b165ad1b13"


def invoke(arguments, env=None, timeout=20):
    result = subprocess.run(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("command failed {}: {}".format(result.returncode, result.stderr[-400:]))
    return result.stdout


def isolated_env(runtime):
    env = os.environ.copy()
    env["CODEX_CIRCUIT_RESUMER_HOME"] = str(runtime)
    return env


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def retry_detection_drill(index, error_message):
    with tempfile.TemporaryDirectory(prefix="codex-resumer-drill-") as temporary:
        root = Path(temporary)
        runtime = root / "runtime"
        sessions = root / "sessions"
        sessions.mkdir()
        log_path = root / "cc-switch.log"
        log_path.write_text("", encoding="utf-8")
        rollout = sessions / ("rollout-" + THREAD_ID + ".jsonl")
        now = int(time.time())
        events = [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-{}".format(index), "started_at": now - 2}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-{}".format(index), "completed_at": now, "last_agent_message": None, "error": {"message": error_message}}},
        ]
        rollout.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")

        state_db = root / "state.sqlite"
        with sqlite3.connect(str(state_db)) as database:
            database.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, title TEXT)")
            database.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?)",
                (THREAD_ID, str(rollout), str(root), "50次操练-{}".format(index)),
            )
        cc_db = root / "cc.sqlite"
        with sqlite3.connect(str(cc_db)) as database:
            database.execute("CREATE TABLE proxy_request_logs (app_type TEXT, status_code INTEGER, created_at INTEGER, session_id TEXT)")
        codex_config = root / "config.toml"
        codex_config.write_text('model_reasoning_effort = "high"\n', encoding="utf-8")

        write_json(runtime / "config.json", {
            "cc_switch_log": str(log_path),
            "cc_switch_db": str(cc_db),
            "codex_state_db": str(state_db),
            "codex_sessions_dir": str(sessions),
            "codex_config": str(codex_config),
            "model_retry_base_seconds": 60,
            "model_retry_max_seconds": 900,
            "model_retry_jitter_seconds": 0,
            "retry_error_scan_seconds": 2,
            "provider_snapshot_seconds": 2147483647,
            "balance_probes_enabled": False,
            "notify": False,
        })
        invoke(["/usr/bin/python3", str(DAEMON), "--once", "--dry-run"], isolated_env(runtime))
        state = json.loads((runtime / "state.json").read_text(encoding="utf-8"))
        scheduled = state.get("scheduled_retries") or []
        if len(scheduled) != 1 or scheduled[0].get("turn_id") != "turn-{}".format(index):
            raise AssertionError("retry was not durably scheduled")
        capacity_wrappers = (
            "at capacity" in error_message
            or "stream disconnected before completion: upstream request failed" in error_message.lower()
        )
        if capacity_wrappers and scheduled[0].get("reasoning_effort") != "medium":
            raise AssertionError("capacity retry did not fall back from high to medium")


def gateway_bypass_drill(index):
    """Process-level crash recovery: P1 is bypassed for launch, then restored."""
    with tempfile.TemporaryDirectory(prefix="codex-gateway-bypass-drill-") as temporary:
        root = Path(temporary)
        runtime = root / "runtime"
        sessions = root / "sessions"
        sessions.mkdir()
        log_path = root / "cc-switch.log"
        log_path.write_text("", encoding="utf-8")
        rollout = sessions / ("rollout-" + THREAD_ID + ".jsonl")
        now = int(time.time())
        gateway_error = "<html><title>400 Bad Request</title><h1>400 Bad Request</h1><center>openresty</center></html>"
        events = [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-gateway-{}".format(index), "started_at": now - 2}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-gateway-{}".format(index), "completed_at": now, "last_agent_message": None, "error": {"message": gateway_error}}},
        ]
        rollout.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")

        state_db = root / "state.sqlite"
        with sqlite3.connect(str(state_db)) as database:
            database.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, title TEXT)")
            database.execute("INSERT INTO threads VALUES (?, ?, ?, ?)", (THREAD_ID, str(rollout), str(root), "HTML400-{}".format(index)))
        cc_db = root / "cc.sqlite"
        with sqlite3.connect(str(cc_db)) as database:
            database.execute("CREATE TABLE proxy_request_logs (app_type TEXT, status_code INTEGER, created_at INTEGER, session_id TEXT)")
            database.execute("CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT, in_failover_queue INTEGER, sort_index INTEGER)")
            database.execute("INSERT INTO providers VALUES ('p1','codex','降级P1',1,1)")
            database.execute("INSERT INTO providers VALUES ('p2','codex','正常P2',1,2)")
        fake_codex = root / "fake-codex"
        fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_codex.chmod(0o755)

        write_json(runtime / "config.json", {
            "cc_switch_log": str(log_path),
            "cc_switch_db": str(cc_db),
            "codex_state_db": str(state_db),
            "codex_sessions_dir": str(sessions),
            "codex_binary": str(fake_codex),
            "prefer_native_codex": False,
            "codex_proxy_route_repair_enabled": False,
            "model_retry_base_seconds": 1,
            "model_retry_max_seconds": 1,
            "model_retry_jitter_seconds": 0,
            "retry_error_scan_seconds": 2,
            "provider_snapshot_seconds": 2147483647,
            "balance_probes_enabled": False,
            "billing_probes_enabled": False,
            "notify": False,
        })
        env = isolated_env(runtime)
        invoke(["/usr/bin/python3", str(DAEMON), "--once", "--dry-run"], env)
        state = json.loads((runtime / "state.json").read_text(encoding="utf-8"))
        if not state.get("scheduled_retries") or not state["scheduled_retries"][0].get("gateway_failover_bypass"):
            raise AssertionError("HTML 400 did not request a gateway failover bypass")
        state["scheduled_retries"][0]["not_before"] = 0
        write_json(runtime / "state.json", state)
        invoke(["/usr/bin/python3", str(DAEMON), "--once"], env)
        with sqlite3.connect(str(cc_db)) as database:
            bypassed = database.execute("SELECT in_failover_queue FROM providers WHERE id='p1'").fetchone()[0]
        if bypassed != 0:
            raise AssertionError("P1 was not bypassed for the gateway retry")
        # A fresh daemon instance represents restart/crash recovery.
        invoke(["/usr/bin/python3", str(DAEMON), "--once", "--dry-run"], env)
        with sqlite3.connect(str(cc_db)) as database:
            restored = database.execute("SELECT in_failover_queue,sort_index FROM providers WHERE id='p1'").fetchone()
        if restored != (1, 1):
            raise AssertionError("P1 was not restored with its original order")


def provider_snapshot_drill():
    real_db = Path.home() / ".cc-switch" / "cc-switch.db"
    with tempfile.TemporaryDirectory(prefix="codex-provider-drill-") as temporary:
        runtime = Path(temporary) / "runtime"
        write_json(runtime / "config.json", {"cc_switch_db": str(real_db), "balance_probes_enabled": False})
        payload = json.loads(invoke(["/usr/bin/python3", str(DAEMON), "--providers"], isolated_env(runtime)))
        if payload.get("error") or not payload.get("providers"):
            raise AssertionError("real CC Switch provider snapshot is empty")


def state_recovery_drill(index):
    with tempfile.TemporaryDirectory(prefix="codex-state-drill-") as temporary:
        runtime = Path(temporary) / "runtime"
        runtime.mkdir(parents=True)
        write_json(runtime / "config.json", {"notify": False})
        (runtime / "state.json").write_text("{broken-json", encoding="utf-8")
        write_json(runtime / "state.backup.json", {
            "version": 2,
            "phase": "idle",
            "queue": [],
            "scheduled_retries": [{"turn_id": "saved-{}".format(index)}],
        })
        payload = json.loads(invoke(["/usr/bin/python3", str(DAEMON), "--status"], isolated_env(runtime)))
        if not payload.get("state_recovered_at") or len(payload.get("scheduled_retries") or []) != 1:
            raise AssertionError("backup state was not recovered")


def sleep_check_drill():
    payload = json.loads(invoke(["/usr/bin/python3", str(DAEMON), "--sleep-check"], timeout=30))
    if len(payload.get("checks") or []) < 6:
        raise AssertionError("sleep check returned an incomplete checklist")


def app_launch_drill(app_path):
    executable = app_path / "Contents" / "MacOS" / "CodexCircuitResumer"
    process = subprocess.Popen([str(executable)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    time.sleep(0.5)
    if process.poll() is not None:
        error = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
        raise AssertionError("app exited early: " + error[-300:])
    process.terminate()
    process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, default=DEFAULT_APP)
    args = parser.parse_args()
    started = time.monotonic()
    outcomes = []
    failures = []

    drills = []
    retry_errors = (
        "Selected model is at capacity",
        "stream disconnected before completion: Upstream request failed",
        "relay.example 503 temporarily unavailable",
    )
    for index in range(15):
        error = retry_errors[index % len(retry_errors)]
        drills.append(("故障续接", lambda index=index, error=error: retry_detection_drill(index, error)))
    drills.extend(("HTML400换线", lambda index=index: gateway_bypass_drill(index)) for index in range(5))
    drills.extend(("真实渠道", provider_snapshot_drill) for _ in range(10))
    drills.extend(("状态恢复", lambda index=index: state_recovery_drill(index)) for index in range(10))
    drills.extend(("睡前检查", sleep_check_drill) for _ in range(5))
    drills.extend(("应用启动", lambda: app_launch_drill(args.app)) for _ in range(5))

    for number, (category, drill) in enumerate(drills, 1):
        try:
            drill()
            outcomes.append(category)
        except Exception as exc:
            failures.append({"number": number, "category": category, "error": str(exc)})

    report = {
        "total": len(drills),
        "passed": len(outcomes),
        "failed": len(failures),
        "duration_seconds": round(time.monotonic() - started, 2),
        "categories": dict(Counter(outcomes)),
        "failures": failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures and len(drills) == 50 else 1


if __name__ == "__main__":
    raise SystemExit(main())
