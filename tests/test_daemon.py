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

    def test_turn_status_keeps_latest_user_message_for_resume_attribution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            write_events(
                path,
                [
                    event("task_started", turn_id="turn-marked", started_at=100),
                    event("user_message", message="继续\n[Codex熔断续聊:auto-123]"),
                    event(
                        "task_complete",
                        turn_id="turn-marked",
                        completed_at=110,
                        last_agent_message="done",
                    ),
                ],
            )
            status = daemon.latest_turn_status(path)
            self.assertEqual(status["user_message"], "继续\n[Codex熔断续聊:auto-123]")

    def test_turn_status_reads_response_item_user_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            write_events(
                path,
                [
                    event("task_started", turn_id="turn-response-item", started_at=100),
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "继续"},
                                {"type": "input_text", "text": "\n[Codex熔断续聊:auto-456]"},
                            ],
                        },
                    },
                    event(
                        "task_complete",
                        turn_id="turn-response-item",
                        completed_at=110,
                        last_agent_message="done",
                    ),
                ],
            )

            status = daemon.latest_turn_status(path)

            self.assertEqual(status["user_message"], "继续\n[Codex熔断续聊:auto-456]")

    def test_turn_status_keeps_auto_marker_before_later_user_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            write_events(
                path,
                [
                    event("task_started", turn_id="turn-steered", started_at=100),
                    event("user_message", message="继续\n[Codex熔断续聊:auto-789]"),
                    event("user_message", message="顺便把测试也跑了"),
                    event(
                        "task_complete",
                        turn_id="turn-steered",
                        completed_at=110,
                        last_agent_message="done",
                    ),
                ],
            )

            status = daemon.latest_turn_status(path)

            self.assertEqual(status["user_message"], "顺便把测试也跑了")
            self.assertIn("继续\n[Codex熔断续聊:auto-789]", status["user_messages"])

    def test_windows_npm_codex_shim_uses_cmd_wrapper(self):
        with mock.patch.object(daemon, "IS_WINDOWS", True), mock.patch.dict(
            os.environ, {"COMSPEC": r"C:\Windows\System32\cmd.exe"}
        ):
            command = daemon.codex_command(r"C:\Users\me\AppData\Roaming\npm\codex.cmd", "login", "status")
        self.assertEqual(command[:4], [r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c"])
        self.assertEqual(command[-3:], [r"C:\Users\me\AppData\Roaming\npm\codex.cmd", "login", "status"])

    def test_windows_native_codex_exe_runs_directly(self):
        with mock.patch.object(daemon, "IS_WINDOWS", True):
            command = daemon.codex_command(r"C:\Tools\codex.exe", "exec")
        self.assertEqual(command, [r"C:\Tools\codex.exe", "exec"])

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


class ConfigMigrationTests(unittest.TestCase):
    def test_upgrade_changes_former_parallel_default_and_persists_it(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            daemon.ensure_dirs()
            daemon.atomic_write_json(
                daemon.CONFIG_PATH,
                {"config_schema_version": 8, "max_parallel_resumes": 1},
            )

            config = daemon.load_config()
            persisted = json.loads(daemon.CONFIG_PATH.read_text(encoding="utf-8"))

            self.assertEqual(config["max_parallel_resumes"], 2)
            self.assertEqual(persisted["max_parallel_resumes"], 2)
            self.assertEqual(persisted["config_schema_version"], 10)
            self.assertEqual(persisted["resume_prompt"], daemon.DEFAULT_RESUME_PROMPT)

    def test_upgrade_preserves_custom_parallel_limit(self):
        migrated = daemon.migrate_user_config(
            {"config_schema_version": 8, "max_parallel_resumes": 4}
        )

        self.assertEqual(migrated["max_parallel_resumes"], 4)
        self.assertEqual(migrated["config_schema_version"], 10)

    def test_upgrade_preserves_custom_resume_prompt(self):
        custom = "请按我的项目上下文继续，不要改设置。"
        migrated = daemon.migrate_user_config(
            {"config_schema_version": 9, "resume_prompt": custom}
        )

        self.assertEqual(migrated["config_schema_version"], 10)
        self.assertEqual(migrated["resume_prompt"], custom)


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
            with daemon.sqlite_connection(str(cc_db)) as db:
                db.execute(
                    "CREATE TABLE proxy_request_logs (app_type TEXT, created_at INTEGER, status_code INTEGER, session_id TEXT)"
                )
                db.execute(
                    "INSERT INTO proxy_request_logs VALUES ('codex', ?, 503, ?)",
                    (now, thread_id),
                )

            state_db = root / "state.db"
            with daemon.sqlite_connection(str(state_db)) as db:
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
                with daemon.sqlite_connection(str(cc_db)) as db:
                    db.execute(
                        "CREATE TABLE proxy_request_logs (app_type TEXT, created_at INTEGER, status_code INTEGER, session_id TEXT)"
                    )
                    db.execute(
                        "INSERT INTO proxy_request_logs VALUES ('codex', ?, 503, ?)",
                        (opened_at + 1, thread_id),
                    )

                state_db = root / "state.db"
                with daemon.sqlite_connection(str(state_db)) as db:
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
            with daemon.sqlite_connection(str(cc_db)) as db:
                db.execute(
                    "CREATE TABLE proxy_request_logs (app_type TEXT, created_at INTEGER, status_code INTEGER, session_id TEXT)"
                )
                db.execute(
                    "INSERT INTO proxy_request_logs VALUES ('codex', ?, 504, ?)",
                    (now, thread_id),
                )
            state_db = root / "state.db"
            with daemon.sqlite_connection(str(state_db)) as db:
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
            with daemon.sqlite_connection(str(cc_db)) as log_db, daemon.sqlite_connection(str(state_db)) as state_db_connection:
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

    def test_new_circuit_open_preserves_previous_pending_queue(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            config = {
                **daemon.DEFAULT_CONFIG,
                "max_candidates_per_incident": 2,
                "recovery_grace_seconds": 0,
            }
            watcher = daemon.Watcher(config, dry_run=True)
            old_item = {"thread_id": "thread-old", "turn_id": "turn-old", "title": "旧队列"}
            backlog_item = {
                "thread_id": "thread-backlog",
                "turn_id": "turn-backlog",
                "title": "旧候补",
            }
            new_item = {"thread_id": "thread-new", "turn_id": "turn-new", "title": "新任务"}
            watcher.state["phase"] = "resuming"
            watcher.state["queue"] = [old_item]
            watcher.state["candidate_backlog"] = [backlog_item]

            watcher.handle_open(100, "Open")
            watcher.handle_closed(101, "Closed")
            with mock.patch.object(
                daemon,
                "build_candidates",
                return_value=([dict(old_item), new_item], []),
            ):
                watcher.prepare_queue_if_ready(101)

            self.assertEqual(watcher.state["queue"], [old_item, backlog_item])
            self.assertEqual(watcher.state["candidate_backlog"], [new_item])
            self.assertEqual(watcher.state["phase"], "resuming")

    def test_maintenance_uses_hours_not_double_hours(self):
        state = daemon.initial_state()
        now = 200000
        state["last_cleanup_at"] = 0
        state["resumed_turns"] = {"old": {"resumed_at": now - 86401}}
        state["recovery_attributions"] = {
            "old": {"recorded_at": now - 86401},
            "recent": {"recorded_at": now - 60},
        }
        with tempfile.TemporaryDirectory() as tmp:
            with isolated_runtime(Path(tmp)):
                watcher = daemon.Watcher({**daemon.DEFAULT_CONFIG, "model_retry_watch_hours": 24}, dry_run=True)
                watcher.state = state
                watcher.maintenance(now)
                self.assertNotIn("old", watcher.state["resumed_turns"])
                self.assertNotIn("old", watcher.state["recovery_attributions"])
                self.assertIn("recent", watcher.state["recovery_attributions"])

    def test_runtime_path_contains_user_node_locations(self):
        config = dict(daemon.DEFAULT_CONFIG)
        joined = os.pathsep.join(config["extra_path"])
        normalized = joined.replace("\\", "/")
        self.assertIn(".local/bin", normalized)
        self.assertIn(".hermes/node/bin", normalized)


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
    def _reasoning_state_db(self, root, thread_id, rollout, effort="medium"):
        path = Path(root) / "state.db"
        with daemon.sqlite_connection(str(path)) as db:
            db.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT, title TEXT, reasoning_effort TEXT)"
            )
            db.execute(
                "INSERT INTO threads VALUES (?,?,?,?,?)",
                (thread_id, str(rollout), str(root), "升档测试", effort),
            )
        return path

    def test_capacity_fallback_remembers_desktop_restore(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"capacity_reasoning_promote_after_seconds": 900, "model_retry_jitter_seconds": 0})
            watcher = daemon.Watcher(config)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            watcher.schedule_retry(
                thread_id,
                {"turn_id": "turn-capacity", "error": {"message": "Selected model is at capacity"}},
                {"title": "桌面回升", "cwd": tmp, "path": str(Path(tmp) / "rollout.jsonl"), "reasoning_effort": "high"},
                1000,
                "retryable_error",
            )
            pending = watcher.state["reasoning_restore_pending"][thread_id]
            self.assertEqual(pending["target_effort"], "high")
            self.assertEqual(pending["fallback_effort"], "medium")
            self.assertEqual(pending["not_before"], 1900)

    def test_due_desktop_restore_updates_idle_thread(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            rollout = root / ("rollout-" + thread_id + ".jsonl")
            write_events(
                rollout,
                [
                    event("task_started", turn_id="turn-done", started_at=90),
                    event("task_complete", turn_id="turn-done", completed_at=100, last_agent_message="done"),
                ],
            )
            state_db = self._reasoning_state_db(root, thread_id, rollout)
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"codex_state_db": str(state_db), "capacity_reasoning_promote_enabled": True})
            watcher = daemon.Watcher(config)
            watcher.state["reasoning_restore_pending"] = {
                thread_id: {
                    "target_effort": "high",
                    "fallback_effort": "medium",
                    "not_before": 100,
                    "title": "升档测试",
                    "rollout_path": str(rollout),
                }
            }
            with mock.patch.object(daemon, "update_thread_reasoning_effort", return_value={"ok": True, "detail": "ok"}) as update:
                watcher.restore_due_desktop_reasoning(101)
            update.assert_called_once_with(config, thread_id, "high")
            self.assertNotIn(thread_id, watcher.state["reasoning_restore_pending"])
            self.assertIn("已自动恢复为“高”", watcher.state["last_event"])

    def test_due_desktop_restore_waits_for_active_turn(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            rollout = root / ("rollout-" + thread_id + ".jsonl")
            write_events(rollout, [event("task_started", turn_id="turn-active", started_at=100)])
            state_db = self._reasoning_state_db(root, thread_id, rollout)
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"codex_state_db": str(state_db), "capacity_reasoning_promote_enabled": True})
            watcher = daemon.Watcher(config)
            watcher.state["reasoning_restore_pending"] = {
                thread_id: {
                    "target_effort": "high",
                    "fallback_effort": "medium",
                    "not_before": 100,
                    "title": "执行中",
                    "rollout_path": str(rollout),
                }
            }
            with mock.patch.object(daemon, "update_thread_reasoning_effort") as update:
                watcher.restore_due_desktop_reasoning(101)
            update.assert_not_called()
            self.assertIn(thread_id, watcher.state["reasoning_restore_pending"])
            self.assertGreater(watcher.state["reasoning_restore_pending"][thread_id]["not_before"], 101)

    def test_due_desktop_restore_probe_backs_off_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            rollout = root / ("rollout-" + thread_id + ".jsonl")
            write_events(
                rollout,
                [
                    event("task_started", turn_id="turn-idle", started_at=90),
                    event("task_complete", turn_id="turn-idle", completed_at=100, last_agent_message="done"),
                ],
            )
            state_db = self._reasoning_state_db(root, thread_id, rollout)
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"codex_state_db": str(state_db), "capacity_reasoning_promote_enabled": True})
            watcher = daemon.Watcher(config)
            watcher.state["reasoning_restore_pending"] = {
                thread_id: {
                    "target_effort": "high",
                    "fallback_effort": "medium",
                    "not_before": 100,
                    "title": "低频探测",
                    "rollout_path": str(rollout),
                }
            }
            with mock.patch.object(
                daemon, "update_thread_reasoning_effort", return_value={"ok": False, "detail": "Codex 未确认保存目标档位"}
            ):
                watcher.restore_due_desktop_reasoning(101)
                pending = watcher.state["reasoning_restore_pending"][thread_id]
                self.assertEqual(pending["restore_failures"], 1)
                self.assertEqual(pending["not_before"], 401)
                watcher.restore_due_desktop_reasoning(401)
            self.assertEqual(watcher.state["reasoning_restore_pending"][thread_id]["restore_failures"], 2)
            self.assertEqual(watcher.state["reasoning_restore_pending"][thread_id]["not_before"], 1001)

    def test_empty_restore_queue_clears_stale_error(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            watcher = daemon.Watcher(dict(daemon.DEFAULT_CONFIG))
            watcher.state["last_reasoning_restore_error"] = "Codex 未确认保存目标档位"

            self.assertTrue(watcher.restore_due_desktop_reasoning(100))
            self.assertEqual(watcher.state["last_reasoning_restore_error"], "")

    def test_auto_fallback_after_capacity_is_discovered(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            rollout = root / ("rollout-" + thread_id + ".jsonl")
            write_events(
                rollout,
                [
                    event("task_started", turn_id="turn-full", started_at=100),
                    event(
                        "task_complete",
                        turn_id="turn-full",
                        completed_at=110,
                        error={"message": "Selected model is at capacity"},
                    ),
                    event("user_message", message="继续\n[Codex熔断续聊:auto-restore]"),
                    event("thread_settings_applied", thread_settings={"reasoning_effort": "medium"}),
                    event("task_started", turn_id="turn-medium", started_at=120),
                    event("task_complete", turn_id="turn-medium", completed_at=130, last_agent_message="done"),
                ],
            )
            hint = daemon.reasoning_restore_hint(rollout, "high", now=150, max_age_seconds=3600)
            self.assertIsNotNone(hint)
            self.assertEqual(hint["fallback_effort"], "medium")
            self.assertEqual(hint["target_effort"], "high")

    def test_manual_fallback_after_capacity_does_not_create_restore_hint(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            rollout = Path(tmp) / "rollout.jsonl"
            write_events(
                rollout,
                [
                    event("task_started", turn_id="turn-full", started_at=100),
                    event(
                        "task_complete",
                        turn_id="turn-full",
                        completed_at=110,
                        error={"message": "Selected model is at capacity"},
                    ),
                    event("thread_settings_applied", thread_settings={"reasoning_effort": "medium"}),
                    event("task_started", turn_id="turn-manual", started_at=120),
                    event("user_message", message="继续"),
                ],
            )

            hint = daemon.reasoning_restore_hint(rollout, "high", now=150, max_age_seconds=3600)

            self.assertIsNone(hint)

    def test_stale_restore_pending_is_cleared_after_manual_continue(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            rollout = root / "rollout.jsonl"
            write_events(
                rollout,
                [
                    event("task_started", turn_id="turn-full", started_at=100),
                    event(
                        "task_complete",
                        turn_id="turn-full",
                        completed_at=110,
                        error={"message": "Selected model is at capacity"},
                    ),
                    event("thread_settings_applied", thread_settings={"reasoning_effort": "medium"}),
                    event("task_started", turn_id="turn-manual", started_at=120),
                    event("user_message", message="继续"),
                ],
            )
            state_db = self._reasoning_state_db(root, thread_id, rollout, effort="medium")
            codex_config = root / "config.toml"
            codex_config.write_text('model_reasoning_effort = "high"\n', encoding="utf-8")
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"codex_state_db": str(state_db), "codex_config": str(codex_config)})
            watcher = daemon.Watcher(config)
            watcher.state["reasoning_restore_pending"] = {
                thread_id: {
                    "target_effort": "high",
                    "fallback_effort": "medium",
                    "last_capacity_at": 110,
                    "not_before": 1000,
                    "rollout_path": str(rollout),
                }
            }
            record = daemon.thread_paths_from_db(config, {thread_id})[thread_id]

            self.assertTrue(watcher.discover_reasoning_restore_candidate(thread_id, record, 150))
            self.assertNotIn(thread_id, watcher.state["reasoning_restore_pending"])
            self.assertIn("手动切换模型", watcher.state["last_event"])

    def test_target_event_during_active_fallback_does_not_erase_restore_hint(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            rollout = root / ("rollout-" + thread_id + ".jsonl")
            write_events(
                rollout,
                [
                    event("task_started", turn_id="turn-full", started_at=100),
                    event(
                        "task_complete",
                        turn_id="turn-full",
                        completed_at=110,
                        error={"message": "Selected model is at capacity"},
                    ),
                    event("thread_settings_applied", thread_settings={"reasoning_effort": "medium"}),
                    event("task_started", turn_id="turn-medium", started_at=120),
                    event("thread_settings_applied", thread_settings={"reasoning_effort": "high"}),
                ],
            )
            state_db = self._reasoning_state_db(root, thread_id, rollout, effort="medium")
            codex_config = root / "config.toml"
            codex_config.write_text('model_reasoning_effort = "high"\n', encoding="utf-8")
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({
                "codex_state_db": str(state_db),
                "codex_config": str(codex_config),
                "capacity_reasoning_promote_after_seconds": 60,
            })
            watcher = daemon.Watcher(config)
            record = daemon.thread_paths_from_db(config, {thread_id})[thread_id]
            self.assertTrue(watcher.discover_reasoning_restore_candidate(thread_id, record, 130))
            pending = watcher.state["reasoning_restore_pending"][thread_id]
            self.assertEqual(pending["fallback_effort"], "medium")
            self.assertEqual(pending["target_effort"], "high")

    def test_desktop_restore_remembers_xhigh_selected_before_capacity(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            rollout = root / ("rollout-" + thread_id + ".jsonl")
            write_events(
                rollout,
                [
                    event(
                        "thread_settings_applied",
                        thread_settings={"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
                    ),
                    event("task_started", turn_id="turn-xhigh", started_at=100),
                    event(
                        "task_complete",
                        turn_id="turn-xhigh",
                        completed_at=110,
                        error={"message": "Selected model is at capacity"},
                    ),
                    event(
                        "thread_settings_applied",
                        thread_settings={"model": "gpt-5.6-sol", "reasoning_effort": "high"},
                    ),
                ],
            )
            state_db = self._reasoning_state_db(root, thread_id, rollout, effort="high")
            codex_config = root / "config.toml"
            codex_config.write_text('model_reasoning_effort = "high"\n', encoding="utf-8")
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"codex_state_db": str(state_db), "codex_config": str(codex_config)})
            watcher = daemon.Watcher(config)
            record = daemon.thread_paths_from_db(config, {thread_id})[thread_id]
            self.assertTrue(watcher.discover_reasoning_restore_candidate(thread_id, record, 130))
            pending = watcher.state["reasoning_restore_pending"][thread_id]
            self.assertEqual(pending["fallback_effort"], "high")
            self.assertEqual(pending["target_effort"], "xhigh")

    def test_existing_high_restore_queue_is_upgraded_to_detected_xhigh(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            rollout = root / ("rollout-" + thread_id + ".jsonl")
            write_events(
                rollout,
                [
                    event("thread_settings_applied", thread_settings={"reasoning_effort": "xhigh"}),
                    event("task_complete", turn_id="full", completed_at=110, error={"message": "Selected model is at capacity"}),
                    event("thread_settings_applied", thread_settings={"reasoning_effort": "medium"}),
                ],
            )
            state_db = self._reasoning_state_db(root, thread_id, rollout, effort="medium")
            config = {**daemon.DEFAULT_CONFIG, "codex_state_db": str(state_db)}
            watcher = daemon.Watcher(config)
            watcher.state["reasoning_restore_pending"] = {
                thread_id: {"target_effort": "high", "fallback_effort": "medium", "not_before": 200}
            }
            record = daemon.thread_paths_from_db(config, {thread_id})[thread_id]
            self.assertTrue(watcher.discover_reasoning_restore_candidate(thread_id, record, 130))
            self.assertEqual(watcher.state["reasoning_restore_pending"][thread_id]["target_effort"], "xhigh")

    def test_xhigh_capacity_falls_back_each_tier_then_promotes_to_xhigh(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"capacity_reasoning_promote_after_seconds": 60})
            watcher = daemon.Watcher(config, dry_run=True)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            record = {"title": "极高降档", "reasoning_effort": "xhigh"}
            for now, expected in ((100, "high"), (110, "medium"), (120, "low")):
                effort, meta = watcher.prepare_reasoning_retry(
                    thread_id, record, "Selected model is at capacity", now
                )
                self.assertEqual(effort, expected)
                self.assertTrue(meta["fallback"])
                record["reasoning_effort"] = expected
            effort, meta = watcher.prepare_reasoning_retry(
                thread_id, record, "503 temporarily unavailable", 200
            )
            self.assertEqual(effort, "xhigh")
            self.assertTrue(meta["promotion"])

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
                with daemon.sqlite_connection(str(state_db)) as db:
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

    def test_due_retry_uses_free_parallel_slot(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            rollout = root / ("rollout-" + thread_id + ".jsonl")
            now = int(time.time())
            write_events(
                rollout,
                [
                    event("task_started", turn_id="turn-parallel", started_at=now - 5),
                    event(
                        "task_complete",
                        turn_id="turn-parallel",
                        completed_at=now - 1,
                        last_agent_message=None,
                        error={"message": "Selected model is at capacity"},
                    ),
                ],
            )
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"max_parallel_resumes": 2, "codex_sessions_dir": str(root)})
            watcher = daemon.Watcher(config, dry_run=True)
            watcher.state["phase"] = "resuming"
            watcher.state["inflight"] = {
                "other-thread": {"thread_id": "other-thread", "status": "running"}
            }
            watcher.state["scheduled_retries"] = [
                {
                    "thread_id": thread_id,
                    "turn_id": "turn-parallel",
                    "title": "等待中的独立任务",
                    "cwd": str(root),
                    "rollout_path": str(rollout),
                    "not_before": now - 1,
                }
            ]

            watcher.promote_due_retries(now)

            self.assertEqual([item["thread_id"] for item in watcher.state["queue"]], [thread_id])
            self.assertEqual(watcher.state["scheduled_retries"], [])
            self.assertEqual(watcher.state["phase"], "resuming")

    def test_stalled_active_turn_uses_free_parallel_slot_when_idle(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            rollout = root / ("rollout-" + thread_id + ".jsonl")
            now = int(time.time())
            write_events(
                rollout,
                [event("task_started", turn_id="turn-stalled", started_at=now - 1000)],
            )
            os.utime(rollout, (now - 1000, now - 1000))
            config = dict(daemon.DEFAULT_CONFIG)
            config.update(
                {
                    "max_parallel_resumes": 2,
                    "active_thread_idle_seconds": 240,
                    "codex_sessions_dir": str(root),
                }
            )
            watcher = daemon.Watcher(config, dry_run=True)
            watcher.state["phase"] = "resuming"
            watcher.state["inflight"] = {
                "other-thread": {"thread_id": "other-thread", "status": "running"}
            }
            watcher.state["scheduled_retries"] = [
                {
                    "thread_id": thread_id,
                    "turn_id": "turn-stalled",
                    "title": "等待中的僵住任务",
                    "cwd": str(root),
                    "rollout_path": str(rollout),
                    "not_before": now - 1,
                    "source": "stalled_turn",
                }
            ]

            watcher.promote_due_retries(now)

            self.assertEqual([item["thread_id"] for item in watcher.state["queue"]], [thread_id])
            self.assertEqual(watcher.state["scheduled_retries"], [])

    def test_recent_active_stalled_turn_is_delayed(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            rollout = root / ("rollout-" + thread_id + ".jsonl")
            now = int(time.time())
            write_events(
                rollout,
                [event("task_started", turn_id="turn-fresh", started_at=now - 5)],
            )
            config = dict(daemon.DEFAULT_CONFIG)
            config.update(
                {
                    "max_parallel_resumes": 2,
                    "active_thread_idle_seconds": 240,
                    "codex_sessions_dir": str(root),
                }
            )
            watcher = daemon.Watcher(config, dry_run=True)
            watcher.state["phase"] = "resuming"
            watcher.state["scheduled_retries"] = [
                {
                    "thread_id": thread_id,
                    "turn_id": "turn-fresh",
                    "title": "仍在输出的任务",
                    "cwd": str(root),
                    "rollout_path": str(rollout),
                    "not_before": now - 1,
                    "source": "stalled_turn",
                }
            ]

            watcher.promote_due_retries(now)

            self.assertEqual(watcher.state["queue"], [])
            self.assertEqual(len(watcher.state["scheduled_retries"]), 1)
            self.assertGreater(watcher.state["scheduled_retries"][0]["not_before"], now)

    def test_retry_discovery_continues_while_another_resume_is_running(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            thread_id = "019fb64d-d041-7842-adbf-01b165ad1b13"
            rollout = root / ("rollout-" + thread_id + ".jsonl")
            now = int(time.time())
            write_events(
                rollout,
                [
                    event("task_started", turn_id="turn-discover", started_at=now - 5),
                    event(
                        "task_complete",
                        turn_id="turn-discover",
                        completed_at=now - 1,
                        last_agent_message=None,
                        error={"message": "Selected model is at capacity"},
                    ),
                ],
            )
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"codex_sessions_dir": str(root), "codex_state_db": str(root / "missing.db")})
            watcher = daemon.Watcher(config, dry_run=True)
            watcher.state["phase"] = "resuming"
            watcher.state["inflight"] = {"other-thread": {"thread_id": "other-thread"}}
            watcher.state["last_error_scan_at"] = 0
            watcher.state["retry_scan_cursor"] = now - 10

            watcher.discover_retryable_failures(now)

            self.assertEqual(len(watcher.state["scheduled_retries"]), 1)
            self.assertEqual(watcher.state["scheduled_retries"][0]["thread_id"], thread_id)

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
                with daemon.sqlite_connection(str(state_db)) as db:
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
            self.assertIn(daemon.DEFAULT_RESUME_PROMPT, command[-1])
            self.assertIn("[Codex熔断续聊:", command[-1])
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
            watcher.state["reasoning_restore_pending"] = {
                self.thread_id: {
                    "target_effort": "high",
                    "fallback_effort": "low",
                    "not_before": now + 900,
                }
            }
            watcher.finish_inflight(item, 0, now)
            self.assertNotIn(self.thread_id, watcher.state["reasoning_efforts"])
            self.assertNotIn(self.thread_id, watcher.state["reasoning_originals"])
            self.assertNotIn(self.thread_id, watcher.state["reasoning_last_capacity_at"])
            self.assertIn(self.thread_id, watcher.state["reasoning_restore_pending"])

    def test_auto_resume_success_requires_matching_launch_marker(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            rollout = root / ("rollout-" + self.thread_id + ".jsonl")
            now = int(time.time())
            marker = "auto-marker-123"
            write_events(
                rollout,
                [
                    event("task_started", turn_id="turn-auto", started_at=now - 2),
                    event("user_message", message="继续\n[Codex熔断续聊:{}]".format(marker)),
                    event("user_message", message="补充：完成后再跑一次测试"),
                    event("task_complete", turn_id="turn-auto", completed_at=now, last_agent_message="done"),
                ],
            )
            watcher = daemon.Watcher(dict(daemon.DEFAULT_CONFIG))
            item = self.queue_item(root, rollout, turn_id="turn-failed")
            item["resume_marker"] = marker
            watcher.state["inflight"] = {self.thread_id: dict(item)}

            watcher.finish_inflight(item, 0, now)

            self.assertEqual(watcher.state["resume_success_count"], 1)
            self.assertEqual(watcher.state["manual_recovery_count"], 0)
            self.assertIn("自动续接成功", watcher.state["last_event"])

    def test_completed_recovery_attribution_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            watcher = daemon.Watcher(dict(daemon.DEFAULT_CONFIG))
            marker = "idempotent-marker"
            item = {
                "thread_id": self.thread_id,
                "turn_id": "turn-failed",
                "title": "只计一次",
                "resume_marker": marker,
            }
            current = {
                "state": "complete",
                "turn_id": "turn-auto",
                "user_message": "继续\n[Codex熔断续聊:{}]".format(marker),
            }

            first = watcher.record_completed_recovery(item, current, 100)
            second = watcher.record_completed_recovery(item, current, 101)

            self.assertEqual(first, "auto")
            self.assertEqual(second, "auto")
            self.assertEqual(watcher.state["resume_success_count"], 1)
            self.assertEqual(len(watcher.state["recovery_attributions"]), 1)

    def test_manual_continue_is_not_counted_as_auto_resume_success(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            rollout = root / ("rollout-" + self.thread_id + ".jsonl")
            now = int(time.time())
            write_events(
                rollout,
                [
                    event("task_started", turn_id="turn-manual", started_at=now - 2),
                    event("user_message", message="继续"),
                    event("task_complete", turn_id="turn-manual", completed_at=now, last_agent_message="done"),
                ],
            )
            watcher = daemon.Watcher(dict(daemon.DEFAULT_CONFIG))
            item = self.queue_item(root, rollout, turn_id="turn-failed")
            item["resume_marker"] = "different-auto-marker"
            watcher.state["inflight"] = {self.thread_id: dict(item)}

            watcher.finish_inflight(item, 0, now)

            self.assertEqual(watcher.state["resume_success_count"], 0)
            self.assertEqual(watcher.state["manual_recovery_count"], 1)
            self.assertIn("手动继续后完成", watcher.state["last_event"])

    def test_same_turn_self_completion_is_not_counted_as_manual_recovery(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            rollout = root / ("rollout-" + self.thread_id + ".jsonl")
            now = int(time.time())
            write_events(
                rollout,
                [
                    event("task_started", turn_id="turn-original", started_at=now - 2),
                    event("user_message", message="原始项目要求"),
                    event(
                        "task_complete",
                        turn_id="turn-original",
                        completed_at=now,
                        last_agent_message="done",
                    ),
                ],
            )
            watcher = daemon.Watcher(dict(daemon.DEFAULT_CONFIG))
            item = self.queue_item(root, rollout, turn_id="turn-original")

            watcher.finish_inflight(item, 0, now)

            self.assertEqual(watcher.state["resume_success_count"], 0)
            self.assertEqual(watcher.state["manual_recovery_count"], 0)
            self.assertIn("任务已自行完成", watcher.state["last_event"])

    def test_gateway_success_expedites_retry_without_server_hint(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            now = int(time.time())
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"retry_recovery_probe_seconds": 15, "retry_recovery_grace_seconds": 20})
            watcher = daemon.Watcher(config)
            watcher.state["scheduled_retries"] = [
                {
                    "thread_id": self.thread_id,
                    "turn_id": "turn-wake",
                    "title": "恢复唤醒",
                    "detected_at": now - 100,
                    "not_before": now + 900,
                    "server_hint_seconds": None,
                }
            ]
            with mock.patch.object(daemon, "latest_success_after", return_value=now - 1):
                self.assertTrue(watcher.expedite_retries_after_recovery(now))
            self.assertEqual(watcher.state["scheduled_retries"][0]["not_before"], now + 20)
            self.assertIn("线路已恢复", watcher.state["last_event"])

    def test_gateway_success_preserves_explicit_retry_after(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            now = int(time.time())
            watcher = daemon.Watcher(dict(daemon.DEFAULT_CONFIG))
            watcher.state["scheduled_retries"] = [
                {
                    "thread_id": self.thread_id,
                    "turn_id": "turn-hint",
                    "title": "遵守上游等待",
                    "detected_at": now - 100,
                    "not_before": now + 900,
                    "server_hint_seconds": 900,
                }
            ]
            with mock.patch.object(daemon, "latest_success_after", return_value=now - 1):
                self.assertFalse(watcher.expedite_retries_after_recovery(now))
            self.assertEqual(watcher.state["scheduled_retries"][0]["not_before"], now + 900)

    def test_gateway_success_only_expedites_failures_older_than_success(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            now = int(time.time())
            watcher = daemon.Watcher(dict(daemon.DEFAULT_CONFIG))
            watcher.state["scheduled_retries"] = [
                {
                    "thread_id": self.thread_id,
                    "turn_id": "turn-before-success",
                    "title": "旧故障",
                    "detected_at": now - 100,
                    "not_before": now + 900,
                    "server_hint_seconds": None,
                },
                {
                    "thread_id": "019aaaab-bbbb-7ccc-8ddd-eeeeeeeeeeee",
                    "turn_id": "turn-after-success",
                    "title": "新故障",
                    "detected_at": now - 10,
                    "not_before": now + 900,
                    "server_hint_seconds": None,
                },
            ]

            with mock.patch.object(daemon, "latest_success_after", return_value=now - 50):
                self.assertTrue(watcher.expedite_retries_after_recovery(now))

            old_failure, new_failure = watcher.state["scheduled_retries"]
            self.assertEqual(old_failure["not_before"], now + 20)
            self.assertEqual(old_failure["recovery_detected_at"], now - 50)
            self.assertEqual(new_failure["not_before"], now + 900)
            self.assertNotIn("recovery_detected_at", new_failure)

    def test_due_retry_records_manual_completion_instead_of_auto_success(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            rollout = root / ("rollout-" + self.thread_id + ".jsonl")
            now = int(time.time())
            write_events(
                rollout,
                [
                    event("task_started", turn_id="turn-manual", started_at=now - 2),
                    event("user_message", message="继续"),
                    event("task_complete", turn_id="turn-manual", completed_at=now, last_agent_message="done"),
                ],
            )
            watcher = daemon.Watcher(dict(daemon.DEFAULT_CONFIG))
            watcher.state["scheduled_retries"] = [
                {
                    **self.queue_item(root, rollout, turn_id="turn-failed"),
                    "not_before": now,
                }
            ]

            watcher.promote_due_retries(now)

            self.assertEqual(watcher.state["scheduled_retries"], [])
            self.assertEqual(watcher.state["manual_recovery_count"], 1)
            self.assertIn("你手动继续后完成", watcher.state["last_event"])

    def test_launch_queue_records_manual_completion_before_starting_cli(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            rollout = root / ("rollout-" + self.thread_id + ".jsonl")
            now = int(time.time())
            write_events(
                rollout,
                [
                    event("task_started", turn_id="turn-manual", started_at=now - 2),
                    event("user_message", message="继续"),
                    event("task_complete", turn_id="turn-manual", completed_at=now, last_agent_message="done"),
                ],
            )
            config = dict(daemon.DEFAULT_CONFIG)
            config.update({"notify": False, "codex_proxy_route_repair_enabled": False})
            watcher = daemon.Watcher(config)
            watcher.state["phase"] = "resuming"
            watcher.state["queue"] = [self.queue_item(root, rollout, turn_id="turn-failed")]
            with mock.patch.object(daemon.subprocess, "Popen") as popen:
                watcher.launch_next(now)
            popen.assert_not_called()
            self.assertEqual(watcher.state["manual_recovery_count"], 1)
            self.assertIn("你手动继续后完成", watcher.state["last_event"])

    def test_dry_run_never_updates_desktop_reasoning(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            config = dict(daemon.DEFAULT_CONFIG)
            watcher = daemon.Watcher(config, dry_run=True)
            watcher.state["reasoning_restore_pending"] = {
                self.thread_id: {
                    "target_effort": "high",
                    "fallback_effort": "medium",
                    "not_before": 100,
                }
            }
            with mock.patch.object(daemon, "update_thread_reasoning_effort") as update:
                changed = watcher.restore_due_desktop_reasoning(101)
            self.assertFalse(changed)
            update.assert_not_called()
            self.assertIn(self.thread_id, watcher.state["reasoning_restore_pending"])

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

    def test_upgrade_separates_legacy_launch_count_from_precise_metrics(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            daemon.ensure_dirs()
            legacy = daemon.initial_state()
            legacy.pop("resume_metrics_started_at")
            legacy.pop("legacy_resume_count")
            legacy["resume_count"] = 19
            daemon.atomic_write_json(daemon.STATE_PATH, legacy)

            restored = daemon.load_state()

            self.assertEqual(restored["legacy_resume_count"], 19)
            self.assertEqual(restored["resume_count"], 0)
            self.assertEqual(restored["resume_success_count"], 0)
            self.assertEqual(restored["manual_recovery_count"], 0)
            self.assertGreater(restored["resume_metrics_started_at"], 0)

    def test_upgrade_tolerates_invalid_legacy_launch_count(self):
        for invalid_value in ("not-a-number", {"count": 19}, -3):
            with self.subTest(invalid_value=invalid_value), tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
                daemon.ensure_dirs()
                legacy = daemon.initial_state()
                legacy.pop("resume_metrics_started_at")
                legacy.pop("legacy_resume_count")
                legacy["resume_count"] = invalid_value
                daemon.atomic_write_json(daemon.STATE_PATH, legacy)

                restored = daemon.load_state()

                self.assertEqual(restored["legacy_resume_count"], 0)
                self.assertEqual(restored["resume_count"], 0)
                self.assertGreater(restored["resume_metrics_started_at"], 0)

    def test_gateway_retry_temporarily_bypasses_p1_without_reordering(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            cc_db = root / "cc.db"
            with daemon.sqlite_connection(str(cc_db)) as db:
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
            with daemon.sqlite_connection(str(cc_db)) as db:
                rows = db.execute(
                    "SELECT id,in_failover_queue,sort_index FROM providers ORDER BY sort_index"
                ).fetchall()
            self.assertEqual(rows, [("p1", 0, 1), ("p2", 1, 2)])

            self.assertTrue(daemon.restore_temporary_failover_bypasses(config, state))
            with daemon.sqlite_connection(str(cc_db)) as db:
                rows = db.execute(
                    "SELECT id,in_failover_queue,sort_index FROM providers ORDER BY sort_index"
                ).fetchall()
            self.assertEqual(rows, [("p1", 1, 1), ("p2", 1, 2)])
            self.assertEqual(state["temporary_failover_bypasses"], [])

    def test_restart_restores_a_crash_interrupted_bypass(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            cc_db = root / "cc.db"
            with daemon.sqlite_connection(str(cc_db)) as db:
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
            with daemon.sqlite_connection(str(cc_db)) as db:
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
            with daemon.sqlite_connection(str(cc_db)) as db:
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
            waiting_item = {
                **item,
                "thread_id": "019fb64d-d041-7842-adbf-01b165ad1b99",
                "title": "后续排队任务",
            }
            watcher.state["phase"] = "resuming"
            watcher.state["queue"] = [item, waiting_item]
            with mock.patch.object(daemon.subprocess, "Popen", side_effect=OSError("temporary spawn failure")):
                watcher.launch_next(int(time.time()))
            with daemon.sqlite_connection(str(cc_db)) as db:
                self.assertEqual(db.execute("SELECT in_failover_queue FROM providers WHERE id='p1'").fetchone()[0], 1)
            self.assertEqual(watcher.state["queue"], [waiting_item])
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

    def test_obsolete_blocked_warning_clears_after_newer_turn(self):
        with tempfile.TemporaryDirectory() as tmp, isolated_runtime(tmp):
            root = Path(tmp)
            rollout = root / "rollout.jsonl"
            write_events(
                rollout,
                [
                    event("task_started", turn_id="turn-auth"),
                    event(
                        "task_complete",
                        turn_id="turn-auth",
                        last_agent_message=None,
                        error={"message": "Not logged in"},
                    ),
                    event("task_started", turn_id="turn-recovered"),
                    event("task_complete", turn_id="turn-recovered", last_agent_message="已完成"),
                ],
            )
            state_db = root / "state.db"
            with daemon.sqlite_connection(str(state_db)) as db:
                db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, cwd TEXT, title TEXT)")
                db.execute(
                    "INSERT INTO threads VALUES (?, ?, ?, ?)",
                    (self.thread_id, str(rollout), str(root), "已恢复的任务"),
                )
            watcher = daemon.Watcher({**daemon.DEFAULT_CONFIG, "codex_state_db": str(state_db)})
            watcher.state["blocked"] = {
                self.thread_id: {
                    "thread_id": self.thread_id,
                    "turn_id": "turn-auth",
                    "title": "已恢复的任务",
                    "reason": "鉴权失败",
                }
            }

            self.assertTrue(watcher.reconcile_blocked_tasks(int(time.time())))
            self.assertEqual(watcher.state["blocked"], {})

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
            with daemon.sqlite_connection(str(cc_db)) as db:
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
        with daemon.sqlite_connection(str(path)) as db:
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
            if os.name != "nt":
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
            with daemon.sqlite_connection(str(cc_db)) as db:
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
            with daemon.sqlite_connection(str(cc_db)) as db:
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
            with daemon.sqlite_connection(str(cc_db)) as db:
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
            with daemon.sqlite_connection(str(cc_db)) as db:
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
            with daemon.sqlite_connection(str(cc_db)) as db:
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
            with daemon.sqlite_connection(str(cc_db)) as db:
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
            with daemon.sqlite_connection(str(cc_db)) as db:
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
