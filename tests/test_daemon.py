import importlib.util
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "src" / "daemon.py"
SPEC = importlib.util.spec_from_file_location("circuit_daemon", MODULE_PATH)
daemon = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daemon)


def write_events(path, events):
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def event(payload_type, **payload):
    body = {"type": payload_type}
    body.update(payload)
    return {"type": "event_msg", "payload": body}


def isolated_runtime(root):
    app_home = Path(root) / "runtime"
    return mock.patch.multiple(
        daemon,
        APP_HOME=app_home,
        CONFIG_PATH=app_home / "config.json",
        STATE_PATH=app_home / "state.json",
        PAUSE_PATH=app_home / "PAUSED",
        LOG_PATH=app_home / "logs" / "watcher.log",
        RUN_LOG_DIR=app_home / "logs" / "runs",
        PROVIDERS_PATH=app_home / "providers.json",
        EXCHANGE_RATE_CACHE_PATH=app_home / "exchange-rate.json",
        DEFAULT_CONFIG={**daemon.DEFAULT_CONFIG, "codex_proxy_route_repair_enabled": False},
    )


class TurnStatusTests(unittest.TestCase):
    def test_successful_turn_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            write_events(
                path,
                [
                    event("task_started", turn_id="turn-1"),
                    event("task_complete", turn_id="turn-1", last_agent_message="done"),
                ],
            )
            self.assertEqual(daemon.latest_turn_status(path)["state"], "complete")

    def test_overloaded_turn_is_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            write_events(
                path,
                [
                    event("task_started", turn_id="turn-2"),
                    event(
                        "task_complete",
                        turn_id="turn-2",
                        last_agent_message=None,
                        error={"codex_error_info": "server_overloaded"},
                    ),
                ],
            )
            status = daemon.latest_turn_status(path)
            self.assertEqual(status["state"], "interrupted")
            self.assertEqual(status["turn_id"], "turn-2")

    def test_insufficient_balance_is_interrupted_for_failover(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            write_events(
                path,
                [
                    event("task_started", turn_id="turn-balance"),
                    event(
                        "task_complete",
                        turn_id="turn-balance",
                        last_agent_message=None,
                        error={
                            "code": "INSUFFICIENT_BALANCE",
                            "message": "用户余额不足，请充值后重试 / Insufficient account balance",
                        },
                    ),
                ],
            )
            self.assertEqual(daemon.latest_turn_status(path)["state"], "interrupted")

    def test_bad_request_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            write_events(
                path,
                [
                    event("task_started", turn_id="turn-3"),
                    event(
                        "task_complete",
                        turn_id="turn-3",
                        last_agent_message=None,
                        error={"message": "invalid request body", "code": "bad_request"},
                    ),
                ],
            )
            self.assertEqual(daemon.latest_turn_status(path)["state"], "failed_nonretryable")

    def test_openresty_html_bad_request_is_retried_as_gateway_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            write_events(
                path,
                [
                    event("task_started", turn_id="turn-gateway-400"),
                    event(
                        "task_complete",
                        turn_id="turn-gateway-400",
                        last_agent_message=None,
                        error={
                            "message": "<html><head><title>400 Bad Request</title></head>"
                            "<body><h1>400 Bad Request</h1><center>openresty</center></body></html>"
                        },
                    ),
                ],
            )
            self.assertTrue(daemon.retryable_gateway_error(path.read_text(encoding="utf-8")))
            self.assertEqual(daemon.latest_turn_status(path)["state"], "interrupted")

    def test_json_bad_request_never_matches_gateway_failure(self):
        self.assertFalse(
            daemon.retryable_gateway_error('{"code":"bad_request","message":"invalid request body"}')
        )


class DetectionTests(unittest.TestCase):
    def test_transition_parser(self):
        opened = "[2026-07-31][11:24:08][WARN] 熔断器 Closed → Open"
        closed = "[2026-07-31][11:25:45][INFO] 熔断器 HalfOpen → Closed (恢复正常)"
        self.assertEqual(daemon.event_kind(opened), "open")
        self.assertEqual(daemon.event_kind(closed), "closed")

    def test_failed_session_becomes_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            rollout = root / ("rollout-" + thread_id + ".jsonl")
            write_events(
                rollout,
                [
                    event("task_started", turn_id="turn-overload"),
                    event(
                        "task_complete",
                        turn_id="turn-overload",
                        last_agent_message=None,
                        error={"message": "Selected model is at capacity", "codex_error_info": "server_overloaded"},
                    ),
                ],
            )
            now = int(time.time())
            os.utime(str(rollout), (now, now))

            cc_db = root / "cc.db"
            with sqlite3.connect(str(cc_db)) as db:
                db.execute(
                    "CREATE TABLE proxy_request_logs (app_type TEXT, created_at INTEGER, status_code INTEGER, session_id TEXT)"
                )
                db.execute(
                    "INSERT INTO proxy_request_logs VALUES ('codex', ?, 503, ?)",
                    (now, thread_id),
                )

            state_db = root / "state.db"
            with sqlite3.connect(str(state_db)) as db:
                db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, title TEXT)")
                db.execute(
                    "INSERT INTO threads VALUES (?, ?, ?, ?)",
                    (thread_id, str(rollout), str(root), "熔断测试任务"),
                )

            config = dict(daemon.DEFAULT_CONFIG)
            config.update(
                {
                    "cc_switch_db": str(cc_db),
                    "codex_state_db": str(state_db),
                    "codex_sessions_dir": str(root),
                    "recovery_grace_seconds": 0,
                }
            )
            incident = {"opened_at": now - 2, "recovered_at": now + 2}
            candidates = daemon.build_candidates(config, incident, daemon.initial_state(), now + 3)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["thread_id"], thread_id)
            self.assertEqual(candidates[0]["turn_id"], "turn-overload")


    def test_open_to_closed_replay_queues_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_paths = (daemon.APP_HOME, daemon.STATE_PATH, daemon.PAUSE_PATH)
            daemon.APP_HOME = root / "runtime"
            daemon.STATE_PATH = daemon.APP_HOME / "state.json"
            daemon.PAUSE_PATH = daemon.APP_HOME / "PAUSED"
            old_run_log_dir = daemon.RUN_LOG_DIR
            daemon.RUN_LOG_DIR = daemon.APP_HOME / "logs" / "runs"
            try:
                thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
                rollout = root / ("rollout-" + thread_id + ".jsonl")
                write_events(
                    rollout,
                    [
                        event("task_started", turn_id="turn-replay"),
                        event(
                            "task_complete",
                            turn_id="turn-replay",
                            last_agent_message=None,
                            error={"codex_error_info": "server_overloaded"},
                        ),
                    ],
                )
                opened_at = int(time.time()) - 4
                recovered_at = opened_at + 2
                os.utime(str(rollout), (recovered_at, recovered_at))

                cc_db = root / "cc.db"
                with sqlite3.connect(str(cc_db)) as db:
                    db.execute(
                        "CREATE TABLE proxy_request_logs (app_type TEXT, created_at INTEGER, status_code INTEGER, session_id TEXT)"
                    )
                    db.execute(
                        "INSERT INTO proxy_request_logs VALUES ('codex', ?, 503, ?)",
                        (opened_at + 1, thread_id),
                    )

                state_db = root / "state.db"
                with sqlite3.connect(str(state_db)) as db:
                    db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, title TEXT)")
                    db.execute(
                        "INSERT INTO threads VALUES (?, ?, ?, ?)",
                        (thread_id, str(rollout), str(root), "回放任务"),
                    )

                config = dict(daemon.DEFAULT_CONFIG)
                config.update(
                    {
                        "cc_switch_db": str(cc_db),
                        "codex_state_db": str(state_db),
                        "codex_sessions_dir": str(root),
                        "recovery_grace_seconds": 0,
                        "notify": False,
                    }
                )
                watcher = daemon.Watcher(config)
                watcher.handle_open(opened_at, "熔断器 Closed → Open")
                watcher.handle_closed(recovered_at, "熔断器 HalfOpen → Closed (恢复正常)")
                watcher.prepare_queue_if_ready(recovered_at + 1)
                self.assertEqual(len(watcher.state["queue"]), 1)
                daemon.RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
                fake_process = mock.Mock(pid=4242)
                fake_process.poll.return_value = None
                with mock.patch.object(daemon.subprocess, "Popen", return_value=fake_process) as popen:
                    watcher.launch_next(recovered_at + 1)
                self.assertIn("turn-replay", watcher.state["resumed_turns"])
                self.assertIn(thread_id, watcher.state["inflight"])
                command = popen.call_args.args[0]
                self.assertIn("--skip-git-repo-check", command)
                self.assertIn("features.prevent_idle_sleep=true", command)

                duplicate = daemon.build_candidates(
                    config,
                    {"opened_at": opened_at, "recovered_at": recovered_at},
                    watcher.state,
                    recovered_at + 2,
                )
                self.assertEqual(duplicate, [])
                watcher.processes[4242]["log_handle"].close()
            finally:
                daemon.APP_HOME, daemon.STATE_PATH, daemon.PAUSE_PATH = old_paths
                daemon.RUN_LOG_DIR = old_run_log_dir

    def test_recently_active_turn_stays_pending_then_becomes_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            rollout = root / ("rollout-" + thread_id + ".jsonl")
            write_events(rollout, [event("task_started", turn_id="turn-stuck")])
            now = int(time.time())
            os.utime(str(rollout), (now, now))

            cc_db = root / "cc.db"
            with sqlite3.connect(str(cc_db)) as db:
                db.execute(
                    "CREATE TABLE proxy_request_logs (app_type TEXT, created_at INTEGER, status_code INTEGER, session_id TEXT)"
                )
                db.execute(
                    "INSERT INTO proxy_request_logs VALUES ('codex', ?, 504, ?)",
                    (now, thread_id),
                )
            state_db = root / "state.db"
            with sqlite3.connect(str(state_db)) as db:
                db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, title TEXT)")
                db.execute("INSERT INTO threads VALUES (?, ?, ?, ?)", (thread_id, str(rollout), str(root), "卡住任务"))

            config = dict(daemon.DEFAULT_CONFIG)
            config.update(
                {
                    "cc_switch_db": str(cc_db),
                    "codex_state_db": str(state_db),
                    "codex_sessions_dir": str(root),
                    "active_thread_idle_seconds": 240,
                }
            )
            incident = {"opened_at": now - 1, "recovered_at": now + 1}
            ready, pending = daemon.build_candidates(
                config, incident, daemon.initial_state(), now + 2, include_pending=True
            )
            self.assertEqual(ready, [])
            self.assertEqual(len(pending), 1)

            ready, pending = daemon.build_candidates(
                config, incident, daemon.initial_state(), now + 241, include_pending=True
            )
            self.assertEqual(len(ready), 1)
            self.assertEqual(pending, [])

    def test_recovery_keeps_candidates_beyond_launch_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = int(time.time())
            cc_db = root / "cc.db"
            state_db = root / "state.db"
            with sqlite3.connect(str(cc_db)) as log_db, sqlite3.connect(str(state_db)) as state_db_connection:
                log_db.execute("CREATE TABLE proxy_request_logs (app_type TEXT, created_at INTEGER, status_code INTEGER, session_id TEXT)")
                state_db_connection.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, title TEXT)")
                for index in range(3):
                    thread_id = "019fb64d-d041-7842-adbf-01b165ad1b{:02x}".format(index)
                    rollout = root / ("rollout-" + thread_id + ".jsonl")
                    write_events(rollout, [
                        event("task_started", turn_id="turn-{}".format(index)),
                        event("task_complete", turn_id="turn-{}".format(index), last_agent_message=None,
                              error={"codex_error_info": "server_overloaded"}),
                    ])
                    os.utime(str(rollout), (now, now))
                    state_db_connection.execute("INSERT INTO threads VALUES (?, ?, ?, ?)", (thread_id, str(rollout), str(root), "任务 {}".format(index)))
                    log_db.execute("INSERT INTO proxy_request_logs VALUES ('codex', ?, 503, ?)", (now, thread_id))
            with isolated_runtime(root):
                config = dict(daemon.DEFAULT_CONFIG)
                config.update({"cc_switch_db": str(cc_db), "codex_state_db": str(state_db), "codex_sessions_dir": str(root),
                               "max_candidates_per_incident": 2, "recovery_grace_seconds": 0, "notify": False})
                watcher = daemon.Watcher(config, dry_run=True)
                opened = now - 2
                watcher.handle_open(opened, "熔断器 Closed → Open")
                watcher.handle_closed(now - 1, "熔断器 HalfOpen → Closed")
                watcher.prepare_queue_if_ready(now)
                self.assertEqual(len(watcher.state["queue"]), 2)
                self.assertEqual(len(watcher.state["candidate_backlog"]), 1)
                watcher.launch_next(now)
                watcher.launch_next(now)
                watcher.launch_next(now)
                self.assertEqual(watcher.state["phase"], "resuming")
                self.assertEqual(len(watcher.state["candidate_backlog"]), 0)

    def test_maintenance_uses_hours_not_double_hours(self):
        state = daemon.initial_state()
        now = 200000
        state["last_cleanup_at"] = 0
        state["resumed_turns"] = {"old": {"resumed_at": now - 86401}}
        with tempfile.TemporaryDirectory() as tmp:
            with isolated_runtime(Path(tmp)):
                watcher = daemon.Watcher({**daemon.DEFAULT_CONFIG, "model_retry_watch_hours": 24}, dry_run=True)
                watcher.state = state
                watcher.maintenance(now)
                self.assertNotIn("old", watcher.state["resumed_turns"])

    def test_runtime_path_contains_user_node_locations(self):
        config = dict(daemon.DEFAULT_CONFIG)
        joined = os.pathsep.join(config["extra_path"])
        self.assertIn(".local/bin", joined)
        self.assertIn(".hermes/node/bin", joined)


class CodexStateDatabaseTests(unittest.TestCase):
    def test_modern_state_database_is_preferred_over_legacy_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            modern = home / ".codex" / "state_5.sqlite"
            legacy = home / ".codex" / "sqlite" / "state_5.sqlite"
            modern.parent.mkdir(parents=True)
            legacy.parent.mkdir(parents=True)
            modern.touch()
            legacy.touch()
            config = {"codex_state_db": str(legacy)}

            self.assertEqual(daemon.resolve_codex_state_db(config, home=home), modern)

    def test_legacy_state_database_remains_a_compatible_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            legacy = home / ".codex" / "sqlite" / "state_5.sqlite"
            legacy.parent.mkdir(parents=True)
            legacy.touch()
            config = {"codex_state_db": str(legacy)}

            self.assertEqual(daemon.resolve_codex_state_db(config, home=home), legacy)

    def test_custom_state_database_path_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            modern = home / ".codex" / "state_5.sqlite"
            custom = home / "my-codex" / "custom.sqlite"
            modern.parent.mkdir(parents=True)
            modern.touch()
            config = {"codex_state_db": str(custom)}

            self.assertEqual(daemon.resolve_codex_state_db(config, home=home), custom)

    def test_unreadable_database_warning_is_throttled(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.sqlite"
            broken.write_text("not sqlite", encoding="utf-8")
            config = {"codex_state_db": str(broken)}
            daemon._WARNING_THROTTLE.clear()
            with mock.patch.object(daemon.logging, "warning") as warning:
                daemon.thread_paths_from_db(config, {"019fb64d-d041-7842-adbf-01b165ad1b13"})
                daemon.thread_paths_from_db(config, {"019fb64d-d041-7842-adbf-01b165ad1b13"})

            self.assertEqual(warning.call_count, 1)


class ModelRetryTests(unittest.TestCase):
    def test_capacity_fallback_moves_high_to_medium_to_low_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"codex_config": str(Path(tmp) / "missing.toml"), "model_retry_jitter_seconds": 0})
            watcher = daemon.Watcher(config, dry_run=True)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            record = {"title": "降档", "cwd": tmp, "path": str(Path(tmp) / "rollout.jsonl"), "reasoning_effort": "high"}
            for expected, current in (("medium", "high"), ("low", "medium"), ("low", "low")):
                watcher.state["scheduled_retries"] = []
                watcher.state["resumed_turns"] = {}
                watcher.schedule_retry(
                    thread_id,
                    {"turn_id": "turn-{}".format(expected), "error": {"message": "Selected model is at capacity"}},
                    record,
                    1000,
                    "retryable_error",
                )
                item = watcher.state["scheduled_retries"][0]
                self.assertEqual(item["reasoning_effort"], expected)
                self.assertEqual(item["reasoning_fallback_from"], current)
                record["reasoning_effort"] = expected

    def test_capacity_recovery_promotes_back_to_original_effort(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"capacity_reasoning_promote_after_seconds": 900, "model_retry_jitter_seconds": 0})
            watcher = daemon.Watcher(config, dry_run=True)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            watcher.state["reasoning_efforts"] = {thread_id: "low"}
            watcher.state["reasoning_originals"] = {thread_id: "high"}
            watcher.state["reasoning_last_capacity_at"] = {thread_id: 1}
            scheduled = watcher.schedule_retry(
                thread_id,
                {"turn_id": "turn-promote", "error": {"message": "503 temporarily unavailable"}},
                {"title": "升档", "cwd": tmp, "path": str(Path(tmp) / "rollout.jsonl")},
                1000,
                "retryable_error",
            )
            self.assertTrue(scheduled)
            item = watcher.state["scheduled_retries"][0]
            self.assertEqual(item["reasoning_effort"], "high")
            self.assertTrue(item["reasoning_promotion"])

    def test_minimum_effort_periodically_retries_original_after_capacity(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"capacity_reasoning_promote_after_seconds": 900, "model_retry_jitter_seconds": 0})
            watcher = daemon.Watcher(config, dry_run=True)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            watcher.state["reasoning_efforts"] = {thread_id: "low"}
            watcher.state["reasoning_originals"] = {thread_id: "high"}
            watcher.state["reasoning_last_capacity_at"] = {thread_id: 1}
            watcher.schedule_retry(
                thread_id,
                {"turn_id": "turn-capacity-probe", "error": {"codex_error_info": "server_overloaded"}},
                {"title": "探测高档", "cwd": tmp, "path": str(Path(tmp) / "rollout.jsonl")},
                1000,
                "retryable_error",
            )
            item = watcher.state["scheduled_retries"][0]
            self.assertEqual(item["reasoning_effort"], "high")
            self.assertTrue(item["reasoning_promotion"])

    def test_capacity_error_classifier_is_precise(self):
        self.assertTrue(daemon.is_model_capacity_error("Selected model is at capacity"))
        self.assertTrue(daemon.is_model_capacity_error("server_overloaded"))
        self.assertTrue(
            daemon.is_model_capacity_error(
                "stream disconnected before completion: Upstream request failed"
            )
        )
        self.assertFalse(daemon.is_model_capacity_error('{"code":"bad_request","message":"capacity field invalid"}'))

    def test_desktop_upstream_wrapper_triggers_high_to_medium_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"codex_config": str(Path(tmp) / "missing.toml"), "model_retry_jitter_seconds": 0})
            watcher = daemon.Watcher(config, dry_run=True)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            record = {
                "title": "桌面端上游失败",
                "cwd": tmp,
                "path": str(Path(tmp) / "rollout.jsonl"),
                "reasoning_effort": "high",
            }
            scheduled = watcher.schedule_retry(
                thread_id,
                {
                    "turn_id": "turn-desktop-upstream",
                    "error": {
                        "message": "stream disconnected before completion: Upstream request failed"
                    },
                },
                record,
                1000,
                "retryable_error",
            )
            self.assertTrue(scheduled)
            item = watcher.state["scheduled_retries"][0]
            self.assertEqual(item["reasoning_effort"], "medium")
            self.assertEqual(item["reasoning_fallback_from"], "high")

    def test_capacity_error_schedules_exponential_backoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_paths = (daemon.APP_HOME, daemon.STATE_PATH, daemon.PAUSE_PATH)
            daemon.APP_HOME = root / "runtime"
            daemon.STATE_PATH = daemon.APP_HOME / "state.json"
            daemon.PAUSE_PATH = daemon.APP_HOME / "PAUSED"
            try:
                thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
                rollout = root / ("rollout-" + thread_id + ".jsonl")
                now = int(time.time())
                write_events(
                    rollout,
                    [
                        event("task_started", turn_id="turn-cap", started_at=now - 10),
                        event(
                            "task_complete",
                            turn_id="turn-cap",
                            started_at=now - 10,
                            completed_at=now - 1,
                            last_agent_message=None,
                            error={
                                "message": "Selected model is at capacity. Please try a different model.",
                                "codex_error_info": "server_overloaded",
                            },
                        ),
                    ],
                )
                os.utime(str(rollout), (now, now))
                state_db = root / "state.db"
                with sqlite3.connect(str(state_db)) as db:
                    db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, title TEXT)")
                    db.execute(
                        "INSERT INTO threads VALUES (?, ?, ?, ?)",
                        (thread_id, str(rollout), str(root), "满载测试"),
                    )
                config = dict(daemon.DEFAULT_CONFIG)
                config.update(
                    {
                        "codex_state_db": str(state_db),
                        "codex_sessions_dir": str(root),
                        "model_retry_base_seconds": 60,
                        "model_retry_max_seconds": 900,
                        "model_retry_jitter_seconds": 0,
                    }
                )
                watcher = daemon.Watcher(config, dry_run=True)
                watcher.state["retry_scan_cursor"] = now - 5
                watcher.discover_retryable_failures(now)
                self.assertEqual(len(watcher.state["scheduled_retries"]), 1)
                item = watcher.state["scheduled_retries"][0]
                self.assertEqual(item["turn_id"], "turn-cap")
                self.assertEqual(item["not_before"], now + 60)
                self.assertEqual(item["attempt"], 1)
                self.assertEqual(item["delay_strategy"], "指数退避+随机错峰")

                watcher.state["retry_attempts"][thread_id] = 1
                watcher.state["scheduled_retries"] = []
                watcher.state["retry_scan_cursor"] = now - 5
                # same turn already resumed should not reschedule
                watcher.state["resumed_turns"] = {"turn-cap": {"thread_id": thread_id}}
                watcher.discover_retryable_failures(now + 1)
                self.assertEqual(watcher.state["scheduled_retries"], [])
            finally:
                daemon.APP_HOME, daemon.STATE_PATH, daemon.PAUSE_PATH = old_paths

    def test_retry_delay_respects_safe_server_hint_and_adds_bounded_jitter(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({
                "model_retry_base_seconds": 60,
                "model_retry_max_seconds": 900,
                "model_retry_jitter_seconds": 15,
                "model_retry_respect_server_hint": True,
            })
            watcher = daemon.Watcher(config)
            with mock.patch.object(daemon.random, "randint", return_value=7):
                delay, exponential, hint, jitter = watcher.retry_delay(
                    "thread-1", "429 rate limited; Retry-After: 120 seconds"
                )
            self.assertEqual((delay, exponential, hint, jitter), (127, 60, 120, 7))
            self.assertEqual(daemon.retry_after_seconds("try again in 2 minutes"), 120)
            self.assertEqual(daemon.retry_after_seconds("retry_after=9999999"), 0)

    def test_due_retry_promotes_to_queue_and_skips_completed_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_paths = (daemon.APP_HOME, daemon.STATE_PATH, daemon.PAUSE_PATH)
            daemon.APP_HOME = root / "runtime"
            daemon.STATE_PATH = daemon.APP_HOME / "state.json"
            daemon.PAUSE_PATH = daemon.APP_HOME / "PAUSED"
            try:
                thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
                rollout = root / ("rollout-" + thread_id + ".jsonl")
                now = int(time.time())
                write_events(
                    rollout,
                    [
                        event("task_started", turn_id="turn-due", started_at=now - 5),
                        event(
                            "task_complete",
                            turn_id="turn-due",
                            completed_at=now - 1,
                            last_agent_message=None,
                            error={"message": "relay.example 503 temporarily unavailable"},
                        ),
                    ],
                )
                config = dict(daemon.DEFAULT_CONFIG)
                config.update({"codex_sessions_dir": str(root), "codex_state_db": str(root / "missing.db")})
                watcher = daemon.Watcher(config, dry_run=True)
                watcher.state["scheduled_retries"] = [
                    {
                        "thread_id": thread_id,
                        "turn_id": "turn-due",
                        "title": "到点任务",
                        "cwd": str(root),
                        "rollout_path": str(rollout),
                        "not_before": now - 1,
                    }
                ]
                watcher.promote_due_retries(now)
                self.assertEqual(len(watcher.state["queue"]), 1)
                self.assertEqual(watcher.state["phase"], "resuming")

                watcher.state["phase"] = "idle"
                watcher.state["queue"] = []
                watcher.state["scheduled_retries"] = [
                    {
                        "thread_id": thread_id,
                        "turn_id": "turn-due",
                        "title": "已完成任务",
                        "cwd": str(root),
                        "rollout_path": str(rollout),
                        "not_before": now,
                    }
                ]
                write_events(
                    rollout,
                    [
                        event("task_started", turn_id="turn-due", started_at=now),
                        event("task_complete", turn_id="turn-due", completed_at=now, last_agent_message="done"),
                    ],
                )
                watcher.promote_due_retries(now)
                self.assertEqual(watcher.state["queue"], [])
                self.assertEqual(watcher.state["scheduled_retries"], [])
            finally:
                daemon.APP_HOME, daemon.STATE_PATH, daemon.PAUSE_PATH = old_paths

    def test_retry_watch_window_ignores_old_capacity_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_paths = (daemon.APP_HOME, daemon.STATE_PATH, daemon.PAUSE_PATH)
            daemon.APP_HOME = root / "runtime"
            daemon.STATE_PATH = daemon.APP_HOME / "state.json"
            daemon.PAUSE_PATH = daemon.APP_HOME / "PAUSED"
            try:
                thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
                rollout = root / ("rollout-" + thread_id + ".jsonl")
                now = int(time.time())
                old = now - 7200
                write_events(
                    rollout,
                    [
                        event("task_started", turn_id="turn-old", started_at=old - 10),
                        event(
                            "task_complete",
                            turn_id="turn-old",
                            completed_at=old,
                            last_agent_message=None,
                            error={"codex_error_info": "server_overloaded"},
                        ),
                    ],
                )
                os.utime(str(rollout), (old, old))
                state_db = root / "state.db"
                with sqlite3.connect(str(state_db)) as db:
                    db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, title TEXT)")
                    db.execute("INSERT INTO threads VALUES (?, ?, ?, ?)", (thread_id, str(rollout), str(root), "旧满载"))
                config = dict(daemon.DEFAULT_CONFIG)
                config.update(
                    {
                        "codex_state_db": str(state_db),
                        "codex_sessions_dir": str(root),
                        "model_retry_watch_hours": 1,
                    }
                )
                watcher = daemon.Watcher(config, dry_run=True)
                watcher.state["retry_scan_cursor"] = now - 9000
                watcher.discover_retryable_failures(now)
                self.assertEqual(watcher.state["scheduled_retries"], [])
            finally:
                daemon.APP_HOME, daemon.STATE_PATH, daemon.PAUSE_PATH = old_paths

    def test_retry_attempt_limit_is_respected(self):
        state = daemon.initial_state()
        thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
        state["retry_attempts"] = {thread_id: 2}
        config = dict(daemon.DEFAULT_CONFIG)
        config["model_retry_max_attempts"] = 2
        config["model_retry_until_success"] = False
        old_paths = (daemon.APP_HOME, daemon.STATE_PATH, daemon.PAUSE_PATH)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daemon.APP_HOME = root / "runtime"
            daemon.STATE_PATH = daemon.APP_HOME / "state.json"
            daemon.PAUSE_PATH = daemon.APP_HOME / "PAUSED"
            try:
                daemon.save_state(state)
                watcher = daemon.Watcher(config, dry_run=True)
                scheduled = watcher.schedule_retry(
                    thread_id,
                    {"turn_id": "turn-limit", "error": {"message": "503"}},
                    {"title": "次数上限", "cwd": str(root), "path": str(root / "rollout.jsonl")},
                    int(time.time()),
                    "retryable_error",
                )
                self.assertFalse(scheduled)
                self.assertEqual(watcher.state["scheduled_retries"], [])
            finally:
                daemon.APP_HOME, daemon.STATE_PATH, daemon.PAUSE_PATH = old_paths

    def test_retry_until_success_ignores_attempt_limit(self):
        state = daemon.initial_state()
        thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
        state["retry_attempts"] = {thread_id: 99}
        config = dict(daemon.DEFAULT_CONFIG)
        config["model_retry_max_attempts"] = 2
        config["model_retry_until_success"] = True
        old_paths = (daemon.APP_HOME, daemon.STATE_PATH, daemon.PAUSE_PATH)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daemon.APP_HOME = root / "runtime"
            daemon.STATE_PATH = daemon.APP_HOME / "state.json"
            daemon.PAUSE_PATH = daemon.APP_HOME / "PAUSED"
            try:
                daemon.save_state(state)
                watcher = daemon.Watcher(config, dry_run=True)
                scheduled = watcher.schedule_retry(
                    thread_id,
                    {"turn_id": "turn-overnight", "error": {"message": "Selected model is at capacity"}},
                    {"title": "整夜守候", "cwd": str(root), "path": str(root / "rollout.jsonl")},
                    int(time.time()),
                    "retryable_error",
                )
                self.assertTrue(scheduled)
                self.assertEqual(len(watcher.state["scheduled_retries"]), 1)
                self.assertEqual(watcher.state["scheduled_retries"][0]["attempt"], 100)
            finally:
                daemon.APP_HOME, daemon.STATE_PATH, daemon.PAUSE_PATH = old_paths

    def test_provider_error_classifier(self):
        self.assertEqual(daemon.classify_provider_error("insufficient balance"), "余额不足")
        self.assertEqual(daemon.classify_provider_error("Selected model is at capacity"), "上游账号池满")
        self.assertEqual(daemon.classify_provider_error("503 temporarily unavailable"), "上游临时故障")


class ReliabilityTests(unittest.TestCase):
    thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"

    def make_interrupted_rollout(self, root, turn_id="turn-failed"):
        rollout = Path(root) / ("rollout-" + self.thread_id + ".jsonl")
        now = int(time.time())
        write_events(
            rollout,
            [
                event("task_started", turn_id=turn_id, started_at=now - 5),
                event(
                    "task_complete",
                    turn_id=turn_id,
                    completed_at=now - 1,
                    last_agent_message=None,
                    error={"message": "Selected model is at capacity"},
                ),
            ],
        )
        return rollout

    def queue_item(self, root, rollout, turn_id="turn-failed"):
        return {
            "thread_id": self.thread_id,
            "turn_id": turn_id,
            "title": "可靠性测试",
            "cwd": str(root),
            "rollout_path": str(rollout),
        }

    def test_launch_passes_reasoning_override_before_resume(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            rollout = self.make_interrupted_rollout(root)
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"notify": False, "codex_proxy_route_repair_enabled": False})
            watcher = daemon.Watcher(config)
            item = self.queue_item(root, rollout)
            item["reasoning_effort"] = "medium"
            watcher.state["phase"] = "resuming"
            watcher.state["queue"] = [item]
            process = mock.Mock(pid=4321)
            with mock.patch.object(daemon.subprocess, "Popen", return_value=process) as popen:
                watcher.launch_next(int(time.time()))
            command = popen.call_args.args[0]
            self.assertIn("model_reasoning_effort=\"medium\"", command)
            self.assertLess(command.index("model_reasoning_effort=\"medium\""), command.index("resume"))
            for launched in watcher.processes.values():
                launched["log_handle"].close()

    def test_success_clears_temporary_reasoning_state(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            rollout = root / ("rollout-" + self.thread_id + ".jsonl")
            now = int(time.time())
            write_events(
                rollout,
                [
                    event("task_started", turn_id="turn-done", started_at=now - 2),
                    event("task_complete", turn_id="turn-done", completed_at=now, last_agent_message="done"),
                ],
            )
            watcher = daemon.Watcher(dict(daemon.DEFAULT_CONFIG))
            item = self.queue_item(root, rollout, turn_id="turn-done")
            watcher.state["inflight"] = {self.thread_id: dict(item)}
            watcher.state["reasoning_efforts"] = {self.thread_id: "low"}
            watcher.state["reasoning_originals"] = {self.thread_id: "high"}
            watcher.state["reasoning_last_capacity_at"] = {self.thread_id: now - 10}
            watcher.finish_inflight(item, 0, now)
            self.assertNotIn(self.thread_id, watcher.state["reasoning_efforts"])
            self.assertNotIn(self.thread_id, watcher.state["reasoning_originals"])
            self.assertNotIn(self.thread_id, watcher.state["reasoning_last_capacity_at"])

    def test_corrupt_primary_recovers_queue_from_backup(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            state = daemon.initial_state()
            state["queue"] = [{"thread_id": self.thread_id, "turn_id": "turn-safe"}]
            daemon.save_state(state)
            daemon.STATE_PATH.write_text("{broken", encoding="utf-8")
            restored = daemon.load_state()
            self.assertEqual(restored["queue"], state["queue"])
            self.assertGreater(restored["state_recovered_at"], 0)
            self.assertIn("安全备份", restored["last_event"])

    def test_gateway_retry_temporarily_bypasses_p1_without_reordering(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            cc_db = root / "cc.db"
            with sqlite3.connect(str(cc_db)) as db:
                db.execute(
                    "CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT, "
                    "in_failover_queue INTEGER, sort_index INTEGER)"
                )
                db.execute("INSERT INTO providers VALUES ('p1','codex','慢线路',1,1)")
                db.execute("INSERT INTO providers VALUES ('p2','codex','正常线路',1,2)")
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"cc_switch_db": str(cc_db), "notify": False})
            state = daemon.initial_state()

            bypass = daemon.temporarily_bypass_first_codex_provider(
                config, state, int(time.time()), "OpenResty HTML 400"
            )
            self.assertEqual(bypass["provider_id"], "p1")
            with sqlite3.connect(str(cc_db)) as db:
                rows = db.execute(
                    "SELECT id,in_failover_queue,sort_index FROM providers ORDER BY sort_index"
                ).fetchall()
            self.assertEqual(rows, [("p1", 0, 1), ("p2", 1, 2)])

            self.assertTrue(daemon.restore_temporary_failover_bypasses(config, state))
            with sqlite3.connect(str(cc_db)) as db:
                rows = db.execute(
                    "SELECT id,in_failover_queue,sort_index FROM providers ORDER BY sort_index"
                ).fetchall()
            self.assertEqual(rows, [("p1", 1, 1), ("p2", 1, 2)])
            self.assertEqual(state["temporary_failover_bypasses"], [])

    def test_restart_restores_a_crash_interrupted_bypass(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            cc_db = root / "cc.db"
            with sqlite3.connect(str(cc_db)) as db:
                db.execute(
                    "CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT, "
                    "in_failover_queue INTEGER, sort_index INTEGER)"
                )
                db.execute("INSERT INTO providers VALUES ('p1','codex','慢线路',1,1)")
                db.execute("INSERT INTO providers VALUES ('p2','codex','正常线路',1,2)")
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"cc_switch_db": str(cc_db), "notify": False})
            state = daemon.initial_state()
            daemon.temporarily_bypass_first_codex_provider(config, state, int(time.time()), "test")

            daemon.Watcher(config)
            with sqlite3.connect(str(cc_db)) as db:
                flags = db.execute(
                    "SELECT in_failover_queue FROM providers ORDER BY sort_index"
                ).fetchall()
            self.assertEqual(flags, [(1,), (1,)])
            self.assertEqual(daemon.load_state()["temporary_failover_bypasses"], [])

    def test_gateway_bypass_popen_failure_restores_p1_and_requeues_task(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            rollout = self.make_interrupted_rollout(root, turn_id="turn-gateway-launch")
            cc_db = root / "cc.db"
            with sqlite3.connect(str(cc_db)) as db:
                db.execute(
                    "CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT, "
                    "in_failover_queue INTEGER, sort_index INTEGER)"
                )
                db.execute("INSERT INTO providers VALUES ('p1','codex','慢线路',1,1)")
                db.execute("INSERT INTO providers VALUES ('p2','codex','正常线路',1,2)")
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"cc_switch_db": str(cc_db), "notify": False, "model_retry_base_seconds": 1})
            watcher = daemon.Watcher(config)
            item = self.queue_item(root, rollout, turn_id="turn-gateway-launch")
            item["gateway_failover_bypass"] = True
            watcher.state["phase"] = "resuming"
            watcher.state["queue"] = [item]
            with mock.patch.object(daemon.subprocess, "Popen", side_effect=OSError("temporary spawn failure")):
                watcher.launch_next(int(time.time()))
            with sqlite3.connect(str(cc_db)) as db:
                self.assertEqual(db.execute("SELECT in_failover_queue FROM providers WHERE id='p1'").fetchone()[0], 1)
            self.assertEqual(watcher.state["queue"], [])
            self.assertEqual(len(watcher.state["scheduled_retries"]), 1)

    def test_popen_failure_does_not_permanently_deduplicate(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            rollout = self.make_interrupted_rollout(root)
            daemon.ensure_dirs()
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"notify": False, "model_retry_base_seconds": 1})
            watcher = daemon.Watcher(config)
            watcher.state["phase"] = "resuming"
            watcher.state["queue"] = [self.queue_item(root, rollout)]
            with mock.patch.object(daemon.subprocess, "Popen", side_effect=OSError("temporary spawn failure")):
                watcher.launch_next(int(time.time()))
            self.assertNotIn("turn-failed", watcher.state["resumed_turns"])
            self.assertEqual(watcher.state["inflight"], {})
            self.assertEqual(watcher.state["retry_attempts"], {})
            self.assertEqual(len(watcher.state["scheduled_retries"]), 1)
            self.assertEqual(watcher.state["scheduled_retries"][0]["rollout_path"], str(rollout))

    def test_transient_cli_exit_requeues_same_turn(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            rollout = self.make_interrupted_rollout(root)
            run_log = daemon.RUN_LOG_DIR / "run.jsonl"
            run_log.parent.mkdir(parents=True, exist_ok=True)
            run_log.write_text("503 temporarily unavailable\n", encoding="utf-8")
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"notify": False, "model_retry_base_seconds": 1})
            watcher = daemon.Watcher(config)
            item = self.queue_item(root, rollout)
            item["run_log"] = str(run_log)
            watcher.state["inflight"] = {self.thread_id: dict(item)}
            watcher.state["resumed_turns"] = {"turn-failed": {"thread_id": self.thread_id, "resumed_at": int(time.time())}}
            watcher.finish_inflight(item, 1, int(time.time()))
            self.assertNotIn("turn-failed", watcher.state["resumed_turns"])
            self.assertEqual(len(watcher.state["scheduled_retries"]), 1)
            self.assertNotIn(self.thread_id, watcher.state["blocked"])

    def test_auth_failure_is_blocked_instead_of_looping(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            rollout = self.make_interrupted_rollout(root)
            run_log = daemon.RUN_LOG_DIR / "auth.jsonl"
            run_log.parent.mkdir(parents=True, exist_ok=True)
            run_log.write_text("Not logged in. Run codex login.\n", encoding="utf-8")
            config = dict(daemon.DEFAULT_CONFIG)
            config["notify"] = False
            watcher = daemon.Watcher(config)
            item = self.queue_item(root, rollout)
            item["run_log"] = str(run_log)
            watcher.state["inflight"] = {self.thread_id: dict(item)}
            watcher.finish_inflight(item, 1, int(time.time()))
            self.assertEqual(watcher.state["scheduled_retries"], [])
            self.assertEqual(watcher.state["blocked"][self.thread_id]["reason"], "需要重新登录 Codex")

    def test_restart_adopts_live_orphan_resume(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            rollout = self.make_interrupted_rollout(root)
            config = dict(daemon.DEFAULT_CONFIG)
            watcher = daemon.Watcher(config)
            item = self.queue_item(root, rollout)
            item.update({"pid": None, "started_at": int(time.time())})
            watcher.state["inflight"] = {self.thread_id: item}
            with mock.patch.object(daemon, "find_resume_pid", return_value=4321), mock.patch.object(
                daemon, "process_matches_resume", return_value=True
            ):
                watcher.reconcile_inflight(int(time.time()))
            self.assertEqual(watcher.state["inflight"][self.thread_id]["pid"], 4321)
            self.assertNotIn("exited_at", watcher.state["inflight"][self.thread_id])

    def test_missed_closed_log_is_reconciled_from_success_request(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            now = int(time.time())
            cc_db = root / "cc.db"
            with sqlite3.connect(str(cc_db)) as db:
                db.execute("CREATE TABLE proxy_request_logs (app_type TEXT, status_code INTEGER, created_at INTEGER)")
                db.execute("INSERT INTO proxy_request_logs VALUES ('codex', 200, ?)", (now - 2,))
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"cc_switch_db": str(cc_db), "circuit_reconcile_seconds": 10})
            watcher = daemon.Watcher(config)
            watcher.state["phase"] = "circuit_open"
            watcher.state["incident"] = {"opened_at": now - 20, "recovered_at": None}
            watcher.reconcile_circuit(now)
            self.assertEqual(watcher.state["phase"], "recovery_grace")
            self.assertEqual(watcher.state["incident"]["recovered_at"], now - 2)

    def test_tick_exception_is_recorded_and_loop_continues(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            config = dict(daemon.DEFAULT_CONFIG)
            watcher = daemon.Watcher(config)
            with mock.patch.object(watcher, "tick", side_effect=RuntimeError("injected tick fault")), mock.patch.object(
                daemon.time, "sleep", side_effect=lambda _seconds: setattr(watcher, "running", False)
            ):
                watcher.run()
            self.assertEqual(watcher.state["tick_failures_total"], 1)
            self.assertGreater(watcher.state["heartbeat_at"], 0)
            self.assertIn("RuntimeError", watcher.state["last_internal_error"])

    def test_run_logs_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            daemon.RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
            now = int(time.time())
            for index in range(9):
                path = daemon.RUN_LOG_DIR / ("%02d.jsonl" % index)
                path.write_text("x", encoding="utf-8")
                stamp = now - index * 10
                os.utime(path, (stamp, stamp))
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"run_log_max_files": 5, "run_log_retention_days": 7})
            daemon.cleanup_run_logs(config, now)
            remaining = list(daemon.RUN_LOG_DIR.glob("*.jsonl"))
            self.assertEqual(len(remaining), 5)
            self.assertTrue((daemon.RUN_LOG_DIR / "00.jsonl").exists())

    def test_status_reports_fresh_heartbeat_and_counts(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            fake_codex = Path(tmp) / "codex"
            fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_codex.chmod(0o755)
            state = daemon.initial_state()
            state["heartbeat_at"] = int(time.time())
            state["inflight"] = {self.thread_id: {"thread_id": self.thread_id}}
            state["blocked"] = {"other": {"turn_id": "turn-blocked"}}
            daemon.save_state(state)
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"codex_binary": str(fake_codex), "prefer_native_codex": False})
            payload = daemon.status_payload(config)
            self.assertTrue(payload["healthy"])
            self.assertEqual(payload["inflight_count"], 1)
            self.assertEqual(payload["blocked_count"], 1)
            self.assertTrue(payload["codex_binary_exists"])

    def test_sensitive_values_are_redacted(self):
        raw = 'Authorization: Bearer placeholder.token.value api_key="redaction-test-token"'
        cleaned = daemon.redact_sensitive_text(raw)
        self.assertNotIn("placeholder.token.value", cleaned)
        self.assertNotIn("redaction-test-token", cleaned)

    def test_idle_state_checkpoint_is_throttled(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            config = dict(daemon.DEFAULT_CONFIG)
            config["state_checkpoint_seconds"] = 10
            watcher = daemon.Watcher(config)
            with mock.patch.object(daemon, "save_state") as save:
                watcher.checkpoint(100)
                watcher.checkpoint(105)
                watcher.checkpoint(109)
                watcher.checkpoint(110)
            self.assertEqual(save.call_count, 2)

    def test_retry_error_scan_is_throttled(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            config = dict(daemon.DEFAULT_CONFIG)
            config["retry_error_scan_seconds"] = 10
            watcher = daemon.Watcher(config)
            watcher.state["last_error_scan_at"] = 100
            with mock.patch.object(daemon.glob, "iglob") as scan:
                watcher.discover_retryable_failures(105)
            scan.assert_not_called()

    def test_twelve_hour_backoff_stays_bounded_and_stops_on_success(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"model_retry_base_seconds": 60, "model_retry_max_seconds": 900, "model_retry_until_success": True})
            watcher = daemon.Watcher(config)
            started = int(time.time())
            now = started
            delays = []
            attempt = 0
            while now < started + 12 * 3600:
                turn_id = "turn-night-%d" % attempt
                status = {"turn_id": turn_id, "error": {"message": "Selected model is at capacity"}}
                record = {"title": "整夜任务", "cwd": str(root), "path": str(root / "night.jsonl")}
                self.assertTrue(watcher.schedule_retry(self.thread_id, status, record, now, "simulation"))
                self.assertFalse(watcher.schedule_retry(self.thread_id, status, record, now, "duplicate"))
                queued = watcher.state["scheduled_retries"].pop()
                delays.append(queued["not_before"] - now)
                now = queued["not_before"]
                watcher.state["resumed_turns"][turn_id] = {"thread_id": self.thread_id, "resumed_at": now}
                watcher.state["retry_attempts"][self.thread_id] = attempt + 1
                attempt += 1
            self.assertGreater(attempt, 40)
            self.assertLessEqual(max(delays), 900)
            self.assertEqual(len(watcher.state["resumed_turns"]), attempt)

            rollout = root / "success.jsonl"
            write_events(rollout, [event("task_started", turn_id="turn-success"), event("task_complete", turn_id="turn-success", last_agent_message="done")])
            watcher.state["scheduled_retries"] = [{
                "thread_id": self.thread_id,
                "turn_id": "turn-success",
                "title": "整夜任务",
                "cwd": str(root),
                "rollout_path": str(rollout),
                "not_before": now,
            }]
            watcher.state["phase"] = "idle"
            watcher.promote_due_retries(now)
            self.assertEqual(watcher.state["scheduled_retries"], [])
            self.assertNotIn(self.thread_id, watcher.state["retry_attempts"])


class ProxyRoutingTests(unittest.TestCase):
    def make_proxy_db(self, path):
        with sqlite3.connect(str(path)) as db:
            db.execute(
                """CREATE TABLE proxy_config (
                    app_type TEXT PRIMARY KEY,
                    proxy_enabled INTEGER,
                    listen_address TEXT,
                    listen_port INTEGER,
                    enabled INTEGER,
                    auto_failover_enabled INTEGER
                )"""
            )
            db.execute(
                "INSERT INTO proxy_config VALUES ('codex',1,'127.0.0.1',15721,1,1)"
            )

    def test_patch_codex_route_uses_cc_switch_without_touching_other_provider(self):
        original = """model = \"gpt-5.6-sol\"
model_provider = \"custom\"

[model_providers.custom]
base_url = \"https://sub.aiboys.xyz\"
wire_api = \"responses\"
experimental_bearer_token = \"secret-current\"

[model_providers.other]
base_url = \"https://keep.example\"
experimental_bearer_token = \"secret-other\"
"""
        patched = daemon.patch_codex_proxy_route_text(original, "http://127.0.0.1:15721/v1")
        route = daemon.read_codex_active_route_text(patched)
        self.assertEqual(route["base_url"], "http://127.0.0.1:15721/v1")
        self.assertEqual(route["wire_api"], "responses")
        self.assertTrue(route["proxy_managed"])
        self.assertIn('base_url = "https://keep.example"', patched)
        self.assertIn('experimental_bearer_token = "secret-other"', patched)

    def test_ensure_codex_route_repairs_bypass_and_creates_private_backup(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            db_path = root / "cc-switch.db"
            config_path = root / "config.toml"
            backup_path = root / "config.before-resumer.toml"
            self.make_proxy_db(db_path)
            original = """model_provider = \"custom\"
[model_providers.custom]
base_url = \"https://sub.aiboys.xyz\"
wire_api = \"responses\"
experimental_bearer_token = \"secret-current\"
"""
            config_path.write_text(original, encoding="utf-8")
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({
                "cc_switch_db": str(db_path),
                "codex_config": str(config_path),
                "codex_proxy_backup": str(backup_path),
                "codex_proxy_route_repair_enabled": True,
            })
            with mock.patch.object(daemon, "local_proxy_listening", return_value=True):
                result = daemon.ensure_codex_proxy_routing(config)
            self.assertEqual(result["status"], "repaired")
            self.assertEqual(
                daemon.read_codex_active_route_text(config_path.read_text(encoding="utf-8"))["base_url"],
                "http://127.0.0.1:15721/v1",
            )
            self.assertEqual(backup_path.read_text(encoding="utf-8"), original)
            self.assertEqual(backup_path.stat().st_mode & 0o777, 0o600)

    def test_launch_waits_instead_of_hitting_direct_route_when_proxy_is_down(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            config = dict(daemon.DEFAULT_CONFIG)
            config["codex_proxy_route_repair_enabled"] = True
            watcher = daemon.Watcher(config)
            watcher.state["phase"] = "resuming"
            watcher.state["queue"] = [{
                "thread_id": "019fb64d-d041-7842-adbf-01b165ad1b13",
                "turn_id": "turn-balance",
                "title": "余额不足测试",
                "cwd": tmp,
                "rollout_path": str(Path(tmp) / "not-read-yet.jsonl"),
            }]
            with mock.patch.object(
                daemon,
                "ensure_codex_proxy_routing",
                return_value={"status": "unavailable", "detail": "CC Switch 本地代理尚未监听"},
            ), mock.patch.object(daemon.subprocess, "Popen") as popen:
                watcher.launch_next(1000)
            popen.assert_not_called()
            self.assertEqual(len(watcher.state["queue"]), 1)
            self.assertEqual(watcher.state["proxy_route_status"], "unavailable")

    def test_watcher_periodically_repairs_route_before_any_failure(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            config = dict(daemon.DEFAULT_CONFIG)
            config["codex_proxy_route_repair_enabled"] = True
            watcher = daemon.Watcher(config)
            with mock.patch.object(
                daemon,
                "ensure_codex_proxy_routing",
                return_value={"status": "repaired", "detail": "Codex 已自动接回 CC Switch 故障转移"},
            ) as repair:
                watcher.reconcile_proxy_route(1000)
                watcher.reconcile_proxy_route(1005)
            self.assertEqual(repair.call_count, 1)
            self.assertEqual(watcher.state["proxy_route_status"], "repaired")


class ExchangeRateTests(unittest.TestCase):
    def test_exchange_rate_rejects_implausible_values(self):
        self.assertTrue(daemon.valid_usd_cny_rate(7.25))
        self.assertFalse(daemon.valid_usd_cny_rate(0))
        self.assertFalse(daemon.valid_usd_cny_rate(99))

    def test_exchange_rate_caches_live_value_and_falls_back_offline(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            state = daemon.initial_state()
            config = dict(daemon.DEFAULT_CONFIG)
            with mock.patch.object(daemon, "query_usd_cny_rate", return_value=7.25):
                live = daemon.resolve_usd_cny_rate(config, state, probe=True, now=1000)
            self.assertEqual(live["rate"], 7.25)
            self.assertEqual(live["source"], "live")
            self.assertTrue(daemon.EXCHANGE_RATE_CACHE_PATH.exists())

            state["exchange_rate_cache"] = {}
            with mock.patch.object(daemon, "query_usd_cny_rate", return_value=None):
                cached = daemon.resolve_usd_cny_rate(config, state, probe=True, now=2000)
            self.assertEqual(cached["rate"], 7.25)
            self.assertEqual(cached["source"], "cache")

    def test_balance_conversion_converts_usd_once_and_keeps_cny(self):
        self.assertEqual(daemon.balance_in_cny({"status": "ok", "amount": "10", "currency": "USD"}, 7.2), 72.0)
        self.assertEqual(daemon.balance_in_cny({"status": "ok", "amount": "72", "currency": "CNY"}, 7.2), 72.0)


class ProviderSnapshotTests(unittest.TestCase):
    def test_legacy_provider_schema_and_one_x_logged_multiplier_are_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = int(time.time())
            cc_db = root / "legacy.db"
            with sqlite3.connect(str(cc_db)) as db:
                db.execute("CREATE TABLE providers (id TEXT, name TEXT, app_type TEXT)")
                db.execute(
                    "CREATE TABLE proxy_request_logs (provider_id TEXT, app_type TEXT, status_code INTEGER, "
                    "latency_ms INTEGER, created_at INTEGER, error_message TEXT, model TEXT, request_model TEXT, "
                    "total_cost_usd TEXT, cost_multiplier TEXT)"
                )
                db.execute("INSERT INTO providers VALUES ('legacy','Legacy-0.08','codex')")
                db.execute(
                    "INSERT INTO proxy_request_logs VALUES ('legacy','codex',200,500,?,'','gpt','gpt','2','1.0')",
                    (now,),
                )
            config = {**daemon.DEFAULT_CONFIG, "cc_switch_db": str(cc_db)}
            state = daemon.initial_state()
            state["billing_cache"] = {"codex:legacy": {"status": "error", "detail": "temporary timeout"}}
            snapshot = daemon.build_provider_snapshot(config, state, now=now)
            self.assertIsNone(snapshot["error"])
            self.assertEqual(len(snapshot["providers"]), 1)
            provider = snapshot["providers"][0]
            self.assertEqual(provider["actual_multiplier"], 1.0)
            self.assertEqual(provider["actual_multiplier_source"], "请求记录")
            self.assertTrue(provider["multiplier_changed"])

    def test_anthropic_provider_config_is_detected_without_exposing_key(self):
        config = json.dumps({
            "env": {
                "ANTHROPIC_BASE_URL": "https://relay.example",
                "ANTHROPIC_AUTH_TOKEN": "private-claude-token",
            }
        })
        details = daemon.extract_provider_details(config)
        self.assertEqual(details["base_url"], "https://relay.example")
        self.assertTrue(details["api_key"])
        safe = {"base_url": details["base_url"], "models": details["models"]}
        self.assertNotIn("private-claude-token", json.dumps(safe))

    def test_sub2api_billing_probe_reads_effective_multiplier(self):
        payload = json.dumps({
            "object": "sub2api.key_billing",
            "group_rate_multiplier": 0.08,
            "resolved_rate_multiplier": 0.06,
            "effective_rate_multiplier": 0.06,
            "peak_rate_enabled": False,
            "observed_at": "2026-07-31T09:00:00Z",
        }).encode()

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self): return payload

        with mock.patch.object(daemon, "urlopen", return_value=Response()):
            result = daemon.query_sub2api_billing("https://relay.example", "sk-private")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["group_multiplier"], 0.08)
        self.assertEqual(result["effective_multiplier"], 0.06)
        self.assertNotIn("sk-private", json.dumps(result))

    def test_snapshot_keeps_cc_switch_order_and_totals_verified_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = int(time.time())
            cc_db = root / "ordered.db"
            with sqlite3.connect(str(cc_db)) as db:
                db.execute("CREATE TABLE providers (id TEXT,name TEXT,settings_config TEXT,website_url TEXT,is_current INTEGER,in_failover_queue INTEGER,sort_index INTEGER,cost_multiplier TEXT,provider_type TEXT,app_type TEXT)")
                db.execute("CREATE TABLE provider_health (provider_id TEXT,app_type TEXT,is_healthy INTEGER,consecutive_failures INTEGER,last_success_at INTEGER,last_failure_at INTEGER,last_error TEXT)")
                db.execute("CREATE TABLE proxy_request_logs (provider_id TEXT,app_type TEXT,status_code INTEGER,latency_ms INTEGER,created_at INTEGER,error_message TEXT,model TEXT,request_model TEXT,total_cost_usd TEXT,cost_multiplier TEXT)")
                db.execute("INSERT INTO providers VALUES ('a','A-0.08','{}','',0,1,2,'1.0','openai','codex')")
                db.execute("INSERT INTO providers VALUES ('b','B-0.1','{}','',1,1,1,'1.0','openai','codex')")
                db.execute("INSERT INTO provider_health VALUES ('a','codex',1,0,?,NULL,'')", (now,))
                db.execute("INSERT INTO provider_health VALUES ('b','codex',1,0,?,NULL,'')", (now,))
                db.execute("INSERT INTO proxy_request_logs VALUES ('a','codex',200,1000,?,'','gpt','gpt','10','1')", (now,))
                db.execute("INSERT INTO proxy_request_logs VALUES ('b','codex',200,1000,?,'','gpt','gpt','20','1')", (now,))
            state = daemon.initial_state()
            state["billing_cache"] = {
                "a": {"status": "ok", "effective_multiplier": 0.06, "group_multiplier": 0.06, "resolved_multiplier": 0.06},
                "b": {"status": "ok", "effective_multiplier": 0.08, "group_multiplier": 0.08, "resolved_multiplier": 0.08},
            }
            config = dict(daemon.DEFAULT_CONFIG)
            config["cc_switch_db"] = str(cc_db)
            snapshot = daemon.build_provider_snapshot(config, state, now=now)
            self.assertEqual([p["name"] for p in snapshot["providers"]], ["B-0.1", "A-0.08"])
            self.assertEqual(snapshot["providers"][0]["actual_multiplier_source"], "网站实测")
            self.assertTrue(snapshot["providers"][0]["multiplier_changed"])
            self.assertAlmostEqual(snapshot["total_standard_cost_24h_usd"], 30.0)
            self.assertAlmostEqual(snapshot["total_estimated_actual_cost_24h_usd"], 2.2)
            self.assertAlmostEqual(
                snapshot["total_standard_cost_24h_cny"],
                snapshot["total_standard_cost_24h_usd"] * snapshot["usd_cny_rate"],
            )
            self.assertAlmostEqual(
                snapshot["total_estimated_actual_cost_24h_cny"],
                snapshot["total_estimated_actual_cost_24h_usd"] * snapshot["usd_cny_rate"],
            )
            self.assertGreater(snapshot["usd_cny_rate"], 5)

    def test_snapshot_excludes_claude_code_but_keeps_claude_desktop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = int(time.time())
            cc_db = root / "apps.db"
            with sqlite3.connect(str(cc_db)) as db:
                db.execute("CREATE TABLE providers (id TEXT,name TEXT,settings_config TEXT,website_url TEXT,is_current INTEGER,in_failover_queue INTEGER,sort_index INTEGER,cost_multiplier TEXT,provider_type TEXT,app_type TEXT)")
                db.execute("CREATE TABLE provider_health (provider_id TEXT,app_type TEXT,is_healthy INTEGER,consecutive_failures INTEGER,last_success_at INTEGER,last_failure_at INTEGER,last_error TEXT)")
                db.execute("CREATE TABLE proxy_request_logs (provider_id TEXT,app_type TEXT,status_code INTEGER,latency_ms INTEGER,created_at INTEGER,error_message TEXT,model TEXT,request_model TEXT,total_cost_usd TEXT,cost_multiplier TEXT)")
                db.execute("INSERT INTO providers VALUES ('c1','Codex','{}','',1,1,0,'1.0','openai','codex')")
                db.execute("INSERT INTO providers VALUES ('cc1','Claude Code','{}','',1,1,0,'1.0','anthropic','claude')")
                db.execute("INSERT INTO providers VALUES ('cd1','Claude Desktop','{}','',1,1,0,'1.0','anthropic','claude-desktop')")
            config = dict(daemon.DEFAULT_CONFIG)
            config["cc_switch_db"] = str(cc_db)
            snapshot = daemon.build_provider_snapshot(config, daemon.initial_state(), now=now)
            self.assertEqual([p["app_type"] for p in snapshot["providers"]], ["codex", "claude-desktop"])
            self.assertEqual(set(snapshot["app_summaries"]), {"codex", "claude-desktop"})

    def test_snapshot_reconciles_cc_switch_total_and_discloses_unpriced_relay_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = int(time.time())
            cc_db = root / "cost-reconciliation.db"
            with sqlite3.connect(str(cc_db)) as db:
                db.execute("CREATE TABLE providers (id TEXT,name TEXT,settings_config TEXT,website_url TEXT,is_current INTEGER,in_failover_queue INTEGER,sort_index INTEGER,cost_multiplier TEXT,provider_type TEXT,app_type TEXT)")
                db.execute("CREATE TABLE provider_health (provider_id TEXT,app_type TEXT,is_healthy INTEGER,consecutive_failures INTEGER,last_success_at INTEGER,last_failure_at INTEGER,last_error TEXT)")
                db.execute("CREATE TABLE proxy_request_logs (provider_id TEXT,app_type TEXT,status_code INTEGER,latency_ms INTEGER,created_at INTEGER,error_message TEXT,model TEXT,request_model TEXT,total_cost_usd TEXT,cost_multiplier TEXT,data_source TEXT)")
                db.execute("INSERT INTO providers VALUES ('p1','Relay-0.1','{}','',1,1,0,'1.0','openai','codex')")
                db.execute("INSERT INTO proxy_request_logs VALUES ('p1','codex',200,100,?,'','gpt','gpt','10','1','proxy')", (now,))
                db.execute("INSERT INTO proxy_request_logs VALUES ('deleted','codex',200,100,?,'','gpt','gpt','5','1','proxy')", (now,))
                db.execute("INSERT INTO proxy_request_logs VALUES ('_codex_session','codex',200,100,?,'','gpt','gpt','100','1','codex_session')", (now,))
                db.execute("INSERT INTO proxy_request_logs VALUES ('p1','codex',200,100,?,'','gpt','gpt','7','1','proxy')", (now - 172800,))
            state = daemon.initial_state()
            state["billing_cache"] = {
                "p1": {"status": "ok", "effective_multiplier": 0.1, "group_multiplier": 0.1, "resolved_multiplier": 0.1}
            }
            config = dict(daemon.DEFAULT_CONFIG)
            config["cc_switch_db"] = str(cc_db)
            snapshot = daemon.build_provider_snapshot(config, state, now=now)
            summary = snapshot["app_summaries"]["codex"]
            self.assertAlmostEqual(summary["cc_switch_standard_cost_today_usd"], 115.0)
            self.assertAlmostEqual(summary["relay_standard_cost_today_usd"], 15.0)
            self.assertAlmostEqual(summary["verified_relay_standard_cost_today_usd"], 10.0)
            self.assertAlmostEqual(summary["estimated_relay_actual_cost_today_usd"], 1.0)
            self.assertAlmostEqual(summary["unpriced_relay_standard_cost_today_usd"], 5.0)

    def test_provider_snapshot_infers_multiplier_and_health_without_key_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = int(time.time())
            cc_db = root / "cc.db"
            with sqlite3.connect(str(cc_db)) as db:
                db.execute(
                    """
                    CREATE TABLE providers (
                        id TEXT, name TEXT, settings_config TEXT, website_url TEXT,
                        is_current INTEGER, in_failover_queue INTEGER, cost_multiplier REAL,
                        provider_type TEXT, app_type TEXT, sort_index INTEGER
                    )
                    """
                )
                db.execute(
                    """
                    CREATE TABLE provider_health (
                        provider_id TEXT, app_type TEXT, is_healthy INTEGER,
                        consecutive_failures INTEGER, last_success_at INTEGER,
                        last_failure_at INTEGER, last_error TEXT
                    )
                    """
                )
                db.execute(
                    """
                    CREATE TABLE proxy_request_logs (
                        provider_id TEXT, app_type TEXT, status_code INTEGER,
                        latency_ms INTEGER, created_at INTEGER, error_message TEXT,
                        model TEXT, request_model TEXT
                    )
                    """
                )
                settings = json.dumps(
                    {
                        "base_url": "https://relay.example/v1",
                        "api_key": "redaction-test-token",
                        "model": "gpt-5.6-sol",
                    }
                )
                db.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("p1", "Sub2API-0.05", settings, "https://relay.example", 1, 1, 1.0, "openai", "codex", 1),
                )
                db.execute(
                    "INSERT INTO provider_health VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("p1", "codex", 1, 0, now - 1, None, None),
                )
                db.execute(
                    "INSERT INTO proxy_request_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("p1", "codex", 200, 17000, now - 30, "", "gpt-5.6-sol", "gpt-5.6-sol"),
                )
                db.execute(
                    "INSERT INTO proxy_request_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("p1", "codex", 200, 19000, now - 20, "", "gpt-5.6-sol", "gpt-5.6-sol"),
                )
            config = dict(daemon.DEFAULT_CONFIG)
            config["cc_switch_db"] = str(cc_db)
            snapshot = daemon.build_provider_snapshot(config, daemon.initial_state(), now=now)
            provider = snapshot["providers"][0]
            self.assertEqual(provider["multiplier"], "0.05")
            self.assertEqual(provider["multiplier_source"], "名称")
            self.assertEqual(provider["usability"], "usable")
            self.assertEqual(provider["success_rate_24h"], 100.0)
            self.assertEqual(provider["avg_latency_ms"], 18000.0)
            self.assertEqual(provider["balance"], "渠道未提供查询接口")
            self.assertNotIn("sk-secret", json.dumps(snapshot, ensure_ascii=False))

    def test_provider_snapshot_uses_cc_switch_degraded_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = int(time.time())
            cc_db = root / "degraded.db"
            with sqlite3.connect(str(cc_db)) as db:
                db.execute("CREATE TABLE providers (id TEXT,name TEXT,settings_config TEXT,website_url TEXT,is_current INTEGER,in_failover_queue INTEGER,sort_index INTEGER,cost_multiplier TEXT,provider_type TEXT,app_type TEXT)")
                db.execute("CREATE TABLE provider_health (provider_id TEXT,app_type TEXT,is_healthy INTEGER,consecutive_failures INTEGER,last_success_at INTEGER,last_failure_at INTEGER,last_error TEXT)")
                db.execute("CREATE TABLE proxy_request_logs (provider_id TEXT,app_type TEXT,status_code INTEGER,latency_ms INTEGER,created_at INTEGER,error_message TEXT,model TEXT,request_model TEXT,total_cost_usd TEXT,cost_multiplier TEXT)")
                db.execute("INSERT INTO providers VALUES ('p1','Sub2API-0.05','{}','',1,1,1,'1','openai','codex')")
                db.execute("INSERT INTO provider_health VALUES ('p1','codex',1,2,?,?,'400 Bad Request from openresty')", (now - 60, now - 1))
            config = dict(daemon.DEFAULT_CONFIG)
            config["cc_switch_db"] = str(cc_db)
            provider = daemon.build_provider_snapshot(config, daemon.initial_state(), now=now)["providers"][0]
            self.assertEqual(provider["usability"], "degraded")
            self.assertIn("连续 2 次失败", provider["failure_reason"])

    def test_provider_snapshot_marks_empty_balance_from_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = int(time.time())
            cc_db = root / "cc.db"
            with sqlite3.connect(str(cc_db)) as db:
                db.execute(
                    """
                    CREATE TABLE providers (
                        id TEXT, name TEXT, settings_config TEXT, website_url TEXT,
                        is_current INTEGER, in_failover_queue INTEGER, cost_multiplier REAL,
                        provider_type TEXT, app_type TEXT, sort_index INTEGER
                    )
                    """
                )
                db.execute(
                    """
                    CREATE TABLE provider_health (
                        provider_id TEXT, app_type TEXT, is_healthy INTEGER,
                        consecutive_failures INTEGER, last_success_at INTEGER,
                        last_failure_at INTEGER, last_error TEXT
                    )
                    """
                )
                db.execute(
                    """
                    CREATE TABLE proxy_request_logs (
                        provider_id TEXT, app_type TEXT, status_code INTEGER,
                        latency_ms INTEGER, created_at INTEGER, error_message TEXT,
                        model TEXT, request_model TEXT
                    )
                    """
                )
                db.execute(
                    "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("p2", "100 Hub-0.08", "{}", "https://sub.aiboys.xyz", 0, 1, 1.0, "openai", "codex", 2),
                )
                db.execute(
                    "INSERT INTO provider_health VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("p2", "codex", 0, 3, None, now - 1, "Insufficient account balance"),
                )
                db.execute(
                    "INSERT INTO proxy_request_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("p2", "codex", 503, 0, now - 10, "Insufficient account balance", "gpt-5.5", "gpt-5.5"),
                )
            config = dict(daemon.DEFAULT_CONFIG)
            config["cc_switch_db"] = str(cc_db)
            snapshot = daemon.build_provider_snapshot(config, daemon.initial_state(), now=now)
            provider = snapshot["providers"][0]
            self.assertEqual(provider["error_class"], "余额不足")
            self.assertEqual(provider["balance"], "余额不足")
            self.assertEqual(provider["balance_state"], "empty")
            self.assertEqual(provider["usability"], "unavailable")


if __name__ == "__main__":
    unittest.main()
