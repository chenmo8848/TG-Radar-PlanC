from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS folder_rules (
    folder_name TEXT PRIMARY KEY,
    folder_id INTEGER,
    enabled INTEGER NOT NULL DEFAULT 0,
    alert_channel_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS keyword_rules (
    folder_name TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    pattern TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (folder_name, rule_name),
    FOREIGN KEY (folder_name) REFERENCES folder_rules(folder_name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS system_cache (
    folder_name TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    chat_title TEXT,
    PRIMARY KEY (folder_name, chat_id),
    FOREIGN KEY (folder_name) REFERENCES folder_rules(folder_name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS auto_route_rules (
    folder_name TEXT PRIMARY KEY,
    pattern TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (folder_name) REFERENCES folder_rules(folder_name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pending_route_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_name TEXT NOT NULL,
    folder_id INTEGER,
    peer_ids_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    retries INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ops_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass
class RouteTask:
    id: int
    folder_name: str
    folder_id: int | None
    peer_ids: list[int]
    status: str
    retries: int
    last_error: str | None


class RadarDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            now = self._now()
            for key, value in {
                "revision": "1",
                "total_hits": "0",
                "last_hit_folder": "",
                "last_hit_time": "",
            }.items():
                conn.execute(
                    "INSERT OR IGNORE INTO runtime_state(key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, now),
                )

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def is_empty(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM folder_rules").fetchone()
            return int(row[0]) == 0

    def bump_revision(self, conn: sqlite3.Connection | None = None) -> int:
        now = self._now()
        if conn is None:
            with self.tx() as own:
                return self.bump_revision(own)
        row = conn.execute("SELECT value FROM runtime_state WHERE key='revision'").fetchone()
        current = int(row[0]) if row else 0
        current += 1
        conn.execute(
            "INSERT INTO runtime_state(key, value, updated_at) VALUES ('revision', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (str(current), now),
        )
        return current

    def get_revision(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM runtime_state WHERE key='revision'").fetchone()
            return int(row[0]) if row else 1

    def log_event(self, level: str, action: str, detail: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO ops_log(level, action, detail, created_at) VALUES (?, ?, ?, ?)",
                (level, action, detail[:2000], self._now()),
            )

    def recent_logs(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT level, action, detail, created_at FROM ops_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def get_runtime_stats(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM runtime_state").fetchall()
            return {row[0]: row[1] for row in rows}

    def increment_hit(self, folder_name: str) -> None:
        with self.tx() as conn:
            row = conn.execute("SELECT value FROM runtime_state WHERE key='total_hits'").fetchone()
            total = int(row[0]) + 1 if row else 1
            now = self._now()
            for key, value in {
                "total_hits": str(total),
                "last_hit_folder": folder_name,
                "last_hit_time": now,
            }.items():
                conn.execute(
                    "INSERT INTO runtime_state(key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (key, value, now),
                )

    def upsert_folder(
        self,
        folder_name: str,
        folder_id: int | None,
        enabled: bool | None = None,
        alert_channel_id: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        now = self._now()

        def _apply(c: sqlite3.Connection) -> None:
            existing = c.execute(
                "SELECT enabled, alert_channel_id FROM folder_rules WHERE folder_name=?",
                (folder_name,),
            ).fetchone()
            effective_enabled = int(enabled) if enabled is not None else (int(existing["enabled"]) if existing else 0)
            effective_alert = alert_channel_id if alert_channel_id is not None or existing is None else existing["alert_channel_id"]
            c.execute(
                "INSERT INTO folder_rules(folder_name, folder_id, enabled, alert_channel_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(folder_name) DO UPDATE SET folder_id=excluded.folder_id, updated_at=excluded.updated_at",
                (folder_name, folder_id, effective_enabled, effective_alert, now, now),
            )
            if existing is None:
                c.execute(
                    "UPDATE folder_rules SET enabled=?, alert_channel_id=? WHERE folder_name=?",
                    (effective_enabled, effective_alert, folder_name),
                )

        if conn is None:
            with self.tx() as own:
                _apply(own)
        else:
            _apply(conn)

    def rename_folder(self, old_name: str, new_name: str, folder_id: int | None = None, conn: sqlite3.Connection | None = None) -> None:
        now = self._now()

        def _rename(c: sqlite3.Connection) -> None:
            c.execute("UPDATE folder_rules SET folder_name=?, folder_id=?, updated_at=? WHERE folder_name=?", (new_name, folder_id, now, old_name))
            c.execute("UPDATE keyword_rules SET folder_name=?, updated_at=? WHERE folder_name=?", (new_name, now, old_name))
            c.execute("UPDATE system_cache SET folder_name=? WHERE folder_name=?", (new_name, old_name))
            c.execute("UPDATE auto_route_rules SET folder_name=?, updated_at=? WHERE folder_name=?", (new_name, now, old_name))
            c.execute("UPDATE pending_route_tasks SET folder_name=?, updated_at=? WHERE folder_name=?", (new_name, now, old_name))

        if conn is None:
            with self.tx() as own:
                _rename(own)
                self.bump_revision(own)
        else:
            _rename(conn)

    def delete_folder(self, folder_name: str, conn: sqlite3.Connection | None = None) -> None:
        if conn is None:
            with self.tx() as own:
                own.execute("DELETE FROM folder_rules WHERE folder_name=?", (folder_name,))
                self.bump_revision(own)
        else:
            conn.execute("DELETE FROM folder_rules WHERE folder_name=?", (folder_name,))

    def list_folders(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT folder_name, folder_id, enabled, alert_channel_id, created_at, updated_at FROM folder_rules ORDER BY folder_name COLLATE NOCASE"
            ).fetchall()

    def get_folder(self, folder_name: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT folder_name, folder_id, enabled, alert_channel_id FROM folder_rules WHERE folder_name=?",
                (folder_name,),
            ).fetchone()

    def set_folder_enabled(self, folder_name: str, enabled: bool) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE folder_rules SET enabled=?, updated_at=? WHERE folder_name=?",
                (int(enabled), self._now(), folder_name),
            )
            self.bump_revision(conn)

    def set_folder_alert_channel(self, folder_name: str, channel_id: int | None) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE folder_rules SET alert_channel_id=?, updated_at=? WHERE folder_name=?",
                (channel_id, self._now(), folder_name),
            )
            self.bump_revision(conn)

    def upsert_rule(self, folder_name: str, rule_name: str, pattern: str, conn: sqlite3.Connection | None = None) -> None:
        def _apply(c: sqlite3.Connection) -> None:
            now = self._now()
            c.execute(
                "INSERT INTO keyword_rules(folder_name, rule_name, pattern, enabled, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(folder_name, rule_name) DO UPDATE SET pattern=excluded.pattern, enabled=1, updated_at=excluded.updated_at",
                (folder_name, rule_name, pattern, now, now),
            )

        if conn is None:
            with self.tx() as own:
                _apply(own)
                self.bump_revision(own)
        else:
            _apply(conn)

    def delete_rule(self, folder_name: str, rule_name: str) -> bool:
        with self.tx() as conn:
            cur = conn.execute("DELETE FROM keyword_rules WHERE folder_name=? AND rule_name=?", (folder_name, rule_name))
            if cur.rowcount:
                self.bump_revision(conn)
                return True
            return False

    def update_rule_pattern(self, folder_name: str, rule_name: str, pattern: str) -> bool:
        with self.tx() as conn:
            cur = conn.execute(
                "UPDATE keyword_rules SET pattern=?, updated_at=? WHERE folder_name=? AND rule_name=?",
                (pattern, self._now(), folder_name, rule_name),
            )
            if cur.rowcount:
                self.bump_revision(conn)
                return True
            return False

    def get_rules_for_folder(self, folder_name: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT rule_name, pattern, enabled, updated_at FROM keyword_rules WHERE folder_name=? AND enabled=1 ORDER BY rule_name COLLATE NOCASE",
                (folder_name,),
            ).fetchall()

    def count_rules_for_folder(self, folder_name: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM keyword_rules WHERE folder_name=? AND enabled=1",
                (folder_name,),
            ).fetchone()
            return int(row[0])

    def list_routes(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT folder_name, pattern, updated_at FROM auto_route_rules ORDER BY folder_name COLLATE NOCASE"
            ).fetchall()

    def set_route(self, folder_name: str, pattern: str) -> None:
        with self.tx() as conn:
            now = self._now()
            conn.execute(
                "INSERT INTO auto_route_rules(folder_name, pattern, created_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(folder_name) DO UPDATE SET pattern=excluded.pattern, updated_at=excluded.updated_at",
                (folder_name, pattern, now, now),
            )
            self.bump_revision(conn)

    def delete_route(self, folder_name: str) -> bool:
        with self.tx() as conn:
            cur = conn.execute("DELETE FROM auto_route_rules WHERE folder_name=?", (folder_name,))
            if cur.rowcount:
                self.bump_revision(conn)
                return True
            return False

    def replace_folder_cache(self, folder_name: str, items: list[tuple[int, str | None]], conn: sqlite3.Connection | None = None) -> None:
        def _apply(c: sqlite3.Connection) -> None:
            c.execute("DELETE FROM system_cache WHERE folder_name=?", (folder_name,))
            c.executemany(
                "INSERT INTO system_cache(folder_name, chat_id, chat_title) VALUES (?, ?, ?)",
                [(folder_name, chat_id, chat_title) for chat_id, chat_title in items],
            )

        if conn is None:
            with self.tx() as own:
                _apply(own)
                self.bump_revision(own)
        else:
            _apply(conn)

    def count_cache_for_folder(self, folder_name: str) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM system_cache WHERE folder_name=?", (folder_name,)).fetchone()
            return int(row[0])

    def build_target_map(self, global_alert_channel_id: int | None) -> tuple[dict[int, list[dict[str, Any]]], int]:
        target_map: dict[int, list[dict[str, Any]]] = {}
        valid_rules = 0
        with self._connect() as conn:
            folder_rows = conn.execute(
                "SELECT folder_name, alert_channel_id FROM folder_rules WHERE enabled=1 ORDER BY folder_name COLLATE NOCASE"
            ).fetchall()
            for folder in folder_rows:
                alert_channel = folder["alert_channel_id"] if folder["alert_channel_id"] is not None else global_alert_channel_id
                if alert_channel is None:
                    continue
                rule_rows = conn.execute(
                    "SELECT rule_name, pattern FROM keyword_rules WHERE folder_name=? AND enabled=1 ORDER BY rule_name COLLATE NOCASE",
                    (folder["folder_name"],),
                ).fetchall()
                if not rule_rows:
                    continue
                cache_rows = conn.execute(
                    "SELECT chat_id FROM system_cache WHERE folder_name=?",
                    (folder["folder_name"],),
                ).fetchall()
                valid_rules += len(rule_rows)
                task = {
                    "folder_name": folder["folder_name"],
                    "alert_channel": int(alert_channel),
                    "rules": [(row["rule_name"], row["pattern"]) for row in rule_rows],
                }
                for cache in cache_rows:
                    target_map.setdefault(int(cache["chat_id"]), []).append(task)
        return target_map, valid_rules

    def upsert_route_task(self, folder_name: str, folder_id: int | None, peer_ids: list[int]) -> None:
        payload = json.dumps(peer_ids, ensure_ascii=False)
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO pending_route_tasks(folder_name, folder_id, peer_ids_json, status, retries, last_error, created_at, updated_at) VALUES (?, ?, ?, 'pending', 0, NULL, ?, ?)",
                (folder_name, folder_id, payload, self._now(), self._now()),
            )

    def get_next_route_task(self) -> RouteTask | None:
        with self.tx() as conn:
            row = conn.execute(
                "SELECT id, folder_name, folder_id, peer_ids_json, status, retries, last_error FROM pending_route_tasks WHERE status IN ('pending', 'retry') ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE pending_route_tasks SET status='running', updated_at=? WHERE id=?", (self._now(), row["id"]))
            return RouteTask(
                id=int(row["id"]),
                folder_name=str(row["folder_name"]),
                folder_id=row["folder_id"],
                peer_ids=list(json.loads(row["peer_ids_json"])),
                status=str(row["status"]),
                retries=int(row["retries"]),
                last_error=row["last_error"],
            )

    def complete_route_task(self, task_id: int) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE pending_route_tasks SET status='done', updated_at=?, last_error=NULL WHERE id=?",
                (self._now(), task_id),
            )

    def fail_route_task(self, task_id: int, error: str, retry: bool = True) -> None:
        status = "retry" if retry else "failed"
        with self.tx() as conn:
            conn.execute(
                "UPDATE pending_route_tasks SET status=?, retries=retries+1, last_error=?, updated_at=? WHERE id=?",
                (status, error[:1000], self._now(), task_id),
            )

    def pending_route_count(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM pending_route_tasks WHERE status IN ('pending', 'retry', 'running')"
            ).fetchone()
            return int(row[0])

    def export_legacy_snapshot(self) -> dict[str, Any]:
        folder_rules: dict[str, Any] = {}
        system_cache: dict[str, list[int]] = {}
        auto_route_rules: dict[str, str] = {}
        with self._connect() as conn:
            for folder in conn.execute(
                "SELECT folder_name, folder_id, enabled, alert_channel_id FROM folder_rules ORDER BY folder_name COLLATE NOCASE"
            ).fetchall():
                rules_rows = conn.execute(
                    "SELECT rule_name, pattern FROM keyword_rules WHERE folder_name=? AND enabled=1 ORDER BY rule_name COLLATE NOCASE",
                    (folder["folder_name"],),
                ).fetchall()
                folder_rules[folder["folder_name"]] = {
                    "id": folder["folder_id"],
                    "enable": bool(folder["enabled"]),
                    "alert_channel_id": folder["alert_channel_id"],
                    "rules": {row["rule_name"]: row["pattern"] for row in rules_rows},
                }
                cache_rows = conn.execute(
                    "SELECT chat_id FROM system_cache WHERE folder_name=? ORDER BY chat_id",
                    (folder["folder_name"],),
                ).fetchall()
                system_cache[folder["folder_name"]] = [int(row["chat_id"]) for row in cache_rows]
            for row in conn.execute(
                "SELECT folder_name, pattern FROM auto_route_rules ORDER BY folder_name COLLATE NOCASE"
            ).fetchall():
                auto_route_rules[row["folder_name"]] = row["pattern"]
        return {
            "folder_rules": folder_rules,
            "_system_cache": system_cache,
            "auto_route_rules": auto_route_rules,
        }

    def import_legacy_snapshot(self, payload: dict[str, Any]) -> bool:
        folder_rules = payload.get("folder_rules") or {}
        system_cache = payload.get("_system_cache") or {}
        auto_routes = payload.get("auto_route_rules") or {}
        if not folder_rules and not auto_routes:
            return False

        with self.tx() as conn:
            conn.execute("DELETE FROM auto_route_rules")
            conn.execute("DELETE FROM keyword_rules")
            conn.execute("DELETE FROM system_cache")
            conn.execute("DELETE FROM folder_rules")
            now = self._now()

            for folder_name, cfg in folder_rules.items():
                folder_id = cfg.get("id")
                enabled = 1 if cfg.get("enable") else 0
                alert_channel_id = cfg.get("alert_channel_id")
                conn.execute(
                    "INSERT INTO folder_rules(folder_name, folder_id, enabled, alert_channel_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (folder_name, folder_id, enabled, alert_channel_id, now, now),
                )
                rules = cfg.get("rules") or {}
                for rule_name, pattern in rules.items():
                    conn.execute(
                        "INSERT INTO keyword_rules(folder_name, rule_name, pattern, enabled, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                        (folder_name, rule_name, str(pattern), now, now),
                    )
                for chat_id in system_cache.get(folder_name, []) or []:
                    try:
                        cid = int(chat_id)
                    except Exception:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO system_cache(folder_name, chat_id, chat_title) VALUES (?, ?, NULL)",
                        (folder_name, cid),
                    )

            for folder_name, pattern in auto_routes.items():
                if not conn.execute("SELECT 1 FROM folder_rules WHERE folder_name=?", (folder_name,)).fetchone():
                    conn.execute(
                        "INSERT INTO folder_rules(folder_name, folder_id, enabled, alert_channel_id, created_at, updated_at) VALUES (?, NULL, 0, NULL, ?, ?)",
                        (folder_name, now, now),
                    )
                conn.execute(
                    "INSERT INTO auto_route_rules(folder_name, pattern, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (folder_name, str(pattern), now, now),
                )

            self.bump_revision(conn)
        return True
