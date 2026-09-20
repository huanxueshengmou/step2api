"""SQLite 存储层。

表结构：

``accounts``       账号（API Key 密文落库）+ 额度缓存 + 运行期状态
``proxies``        代理条目
``proxy_pools``    代理池
``pool_members``   池成员（多对多）
``sessions``       粘性路由的会话 → 账号映射
``usage_logs``     请求日志
``kv``             零散配置
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .config import Settings
from .crypto import get_box, key_fingerprint, key_hint

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS accounts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT    NOT NULL,
    api_key_enc          TEXT    NOT NULL,
    key_hint             TEXT    NOT NULL DEFAULT '',
    key_fp               TEXT    NOT NULL DEFAULT '',
    enabled              INTEGER NOT NULL DEFAULT 1,
    weight               INTEGER NOT NULL DEFAULT 1,
    priority             INTEGER NOT NULL DEFAULT 100,
    group_name           TEXT    NOT NULL DEFAULT 'default',
    note                 TEXT    NOT NULL DEFAULT '',

    -- 分级体系：两条通道各自独立
    plan_base            TEXT    NOT NULL DEFAULT '',
    balance_base         TEXT    NOT NULL DEFAULT '',
    plan_endpoint        TEXT,

    -- 额度缓存（Step Plan 订阅维度）
    plan_name            TEXT,
    plan_status          TEXT,
    credits_remaining    REAL,
    credits_total        REAL,
    credits_used         REAL,
    quota_reset_at       TEXT,
    quota_expires_at     TEXT,
    quota_source         TEXT,
    quota_ok             INTEGER NOT NULL DEFAULT 0,
    quota_error          TEXT,
    quota_checked_at     TEXT,

    -- 金额缓存（按量计费维度）
    balance              REAL,
    cash_balance         REAL,
    voucher_balance      REAL,
    account_type         TEXT,
    balance_checked_at   TEXT,

    -- 路由与代理
    max_concurrency      INTEGER NOT NULL DEFAULT 0,
    proxy_mode           TEXT    NOT NULL DEFAULT 'inherit',  -- inherit | direct | pool | dedicated
    pool_id              INTEGER REFERENCES proxy_pools(id) ON DELETE SET NULL,
    proxy_id             INTEGER REFERENCES proxies(id) ON DELETE SET NULL,
    proxy_rotation       INTEGER NOT NULL DEFAULT 1,

    -- 运行期状态
    status               TEXT    NOT NULL DEFAULT 'unknown', -- healthy | degraded | cooldown | disabled | unknown
    last_error           TEXT,
    cooldown_until       TEXT,
    fail_streak          INTEGER NOT NULL DEFAULT 0,
    success_count        INTEGER NOT NULL DEFAULT 0,
    failure_count        INTEGER NOT NULL DEFAULT 0,
    total_requests       INTEGER NOT NULL DEFAULT 0,
    -- 由后台额度刷新按上游实际用量回填
    total_credits_used   REAL    NOT NULL DEFAULT 0,
    last_used_at         TEXT,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_key_fp ON accounts(key_fp) WHERE key_fp <> '';
CREATE INDEX IF NOT EXISTS idx_accounts_group ON accounts(group_name, enabled);

CREATE TABLE IF NOT EXISTS proxies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    label         TEXT    NOT NULL DEFAULT '',
    url_enc       TEXT    NOT NULL,
    url_redacted  TEXT    NOT NULL DEFAULT '',
    scheme        TEXT    NOT NULL DEFAULT 'http',
    enabled       INTEGER NOT NULL DEFAULT 1,
    status        TEXT    NOT NULL DEFAULT 'unknown',  -- healthy | unhealthy | unknown | disabled
    latency_ms    REAL,
    last_error    TEXT,
    last_check_at TEXT,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS proxy_pools (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,
    strategy       TEXT    NOT NULL DEFAULT 'round_robin',
    affinity       TEXT    NOT NULL DEFAULT 'sticky', -- sticky | rotate
    cooldown       REAL    NOT NULL DEFAULT 60,
    fallback_direct INTEGER NOT NULL DEFAULT 1,
    enabled        INTEGER NOT NULL DEFAULT 1,
    note           TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pools_name ON proxy_pools(name);

CREATE TABLE IF NOT EXISTS pool_members (
    pool_id   INTEGER NOT NULL REFERENCES proxy_pools(id) ON DELETE CASCADE,
    proxy_id  INTEGER NOT NULL REFERENCES proxies(id) ON DELETE CASCADE,
    weight    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (pool_id, proxy_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_key  TEXT PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    pool_id      INTEGER,
    proxy_url    TEXT,
    hits         INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS usage_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT NOT NULL,
    account_id     INTEGER,
    account_name   TEXT,
    session_key    TEXT,
    method         TEXT,
    path           TEXT,
    model          TEXT,
    channel        TEXT,          -- plan | api
    status_code    INTEGER,
    duration_ms    REAL,
    attempts       INTEGER DEFAULT 1,
    proxy_label    TEXT,
    prompt_tokens  INTEGER,
    completion_tokens INTEGER,
    total_tokens   INTEGER,
    error          TEXT
);

CREATE INDEX IF NOT EXISTS idx_logs_created ON usage_logs(created_at DESC);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """SQLite 封装。使用单连接 + 锁，够用且没有异步依赖。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_dirs()
        self.path: Path = settings.db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        self._box = get_box(settings)

    # ------------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def init(self) -> None:
        with self.tx() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO kv(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 通用 -----------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.tx() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    # -- kv -------------------------------------------------------------
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- 账号 -----------------------------------------------------------
    def encrypt_key(self, api_key: str) -> str:
        return self._box.encrypt(api_key)

    def decrypt_key(self, account_row: dict | sqlite3.Row) -> str:
        enc = account_row["api_key_enc"] if not isinstance(account_row, dict) else account_row.get("api_key_enc")
        return self._box.decrypt(enc or "")

    def create_account(
        self,
        *,
        name: str,
        api_key: str,
        group_name: str = "default",
        weight: int = 1,
        priority: int = 100,
        note: str = "",
        plan_base: str = "",
        balance_base: str = "",
        max_concurrency: int = 0,
        proxy_mode: str = "inherit",
        pool_id: int | None = None,
        proxy_id: int | None = None,
        proxy_rotation: bool = True,
        enabled: bool = True,
    ) -> int:
        now = _now()
        return self.execute(
            """
            INSERT INTO accounts (
                name, api_key_enc, key_hint, key_fp, enabled, weight, priority,
                group_name, note, plan_base, balance_base, max_concurrency,
                proxy_mode, pool_id, proxy_id, proxy_rotation,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                name,
                self.encrypt_key(api_key),
                key_hint(api_key),
                key_fingerprint(api_key),
                1 if enabled else 0,
                weight,
                priority,
                group_name,
                note,
                plan_base,
                balance_base,
                max_concurrency,
                proxy_mode,
                pool_id,
                proxy_id,
                1 if proxy_rotation else 0,
                now,
                now,
            ),
        )

    def get_account(self, account_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM accounts WHERE id = ?", (account_id,))

    def get_account_by_fp(self, fp: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM accounts WHERE key_fp = ?", (fp,))

    def list_accounts(
        self,
        *,
        enabled_only: bool = False,
        group_name: str | None = None,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM accounts WHERE 1=1"
        params: list[Any] = []
        if enabled_only:
            sql += " AND enabled = 1"
        if group_name:
            sql += " AND group_name = ?"
            params.append(group_name)
        sql += " ORDER BY priority ASC, id ASC"
        return self.query(sql, params)

    def update_account(self, account_id: int, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "name", "enabled", "weight", "priority", "group_name", "note",
            "plan_base", "balance_base", "plan_endpoint", "max_concurrency",
            "proxy_mode", "pool_id", "proxy_id", "proxy_rotation",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        for key in ("enabled", "proxy_rotation"):
            if key in updates and isinstance(updates[key], bool):
                updates[key] = 1 if updates[key] else 0
        updates["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in updates)
        self.execute(
            f"UPDATE accounts SET {sets} WHERE id = ?",
            (*updates.values(), account_id),
        )

    def set_account_key(self, account_id: int, api_key: str) -> None:
        self.execute(
            "UPDATE accounts SET api_key_enc = ?, key_hint = ?, key_fp = ?, updated_at = ? "
            "WHERE id = ?",
            (
                self.encrypt_key(api_key),
                key_hint(api_key),
                key_fingerprint(api_key),
                _now(),
                account_id,
            ),
        )

    def delete_account(self, account_id: int) -> None:
        self.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    def update_account_quota(self, account_id: int, snapshot: dict) -> None:
        """写入一次额度探测结果。

        数值字段一律走 ``COALESCE``：探测失败时保留上一次已知的额度/余额，
        只更新 ``quota_ok`` / ``quota_error`` / ``quota_checked_at``，
        这样界面上呈现的是"最后一次已知值 + 本次失败原因"，而不是把数据抹成空白。
        """
        probed_at = snapshot.get("probed_at") or _now()
        balance_checked = snapshot.get("balance_checked_at") or (
            probed_at if snapshot.get("balance") is not None else None
        )
        self.execute(
            """
            UPDATE accounts SET
                plan_name = COALESCE(?, plan_name),
                plan_status = COALESCE(?, plan_status),
                credits_remaining = COALESCE(?, credits_remaining),
                credits_total = COALESCE(?, credits_total),
                credits_used = COALESCE(?, credits_used),
                quota_reset_at = COALESCE(?, quota_reset_at),
                quota_expires_at = COALESCE(?, quota_expires_at),
                quota_source = COALESCE(?, quota_source),
                quota_ok = ?,
                quota_error = ?,
                quota_checked_at = ?,
                plan_endpoint = COALESCE(?, plan_endpoint),
                balance = COALESCE(?, balance),
                cash_balance = COALESCE(?, cash_balance),
                voucher_balance = COALESCE(?, voucher_balance),
                account_type = COALESCE(?, account_type),
                balance_checked_at = COALESCE(?, balance_checked_at),
                updated_at = ?
            WHERE id = ?
            """,
            (
                snapshot.get("plan_name"),
                snapshot.get("plan_status"),
                snapshot.get("credits_remaining"),
                snapshot.get("credits_total"),
                snapshot.get("credits_used"),
                snapshot.get("reset_at"),
                snapshot.get("expires_at"),
                snapshot.get("source"),
                1 if snapshot.get("ok") else 0,
                snapshot.get("error"),
                probed_at,
                snapshot.get("plan_endpoint"),
                snapshot.get("balance"),
                snapshot.get("cash_balance"),
                snapshot.get("voucher_balance"),
                snapshot.get("account_type"),
                balance_checked,
                _now(),
                account_id,
            ),
        )

    def mark_account_result(
        self,
        account_id: int,
        *,
        success: bool,
        error: str | None = None,
        cooldown_seconds: float = 60.0,
        fail_threshold: int = 5,
    ) -> None:
        """记录一次调用结果，必要时进入冷却。"""
        row = self.get_account(account_id)
        if row is None:
            return
        now = _now()
        if success:
            self.execute(
                """
                UPDATE accounts SET
                    success_count = success_count + 1,
                    total_requests = total_requests + 1,
                    fail_streak = 0,
                    status = 'healthy',
                    last_error = NULL,
                    cooldown_until = NULL,
                    last_used_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, now, account_id),
            )
            return

        streak = int(row["fail_streak"] or 0) + 1
        cooldown_until: str | None = None
        status = "degraded"
        if streak >= fail_threshold:
            status = "cooldown"
            from datetime import timedelta

            cooldown_until = (
                datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
            ).isoformat()

        self.execute(
            """
            UPDATE accounts SET
                failure_count = failure_count + 1,
                total_requests = total_requests + 1,
                fail_streak = ?,
                status = ?,
                last_error = ?,
                cooldown_until = ?,
                last_used_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (streak, status, error, cooldown_until, now, now, account_id),
        )

    def clear_cooldown(self, account_id: int) -> None:
        self.execute(
            "UPDATE accounts SET status = 'healthy', cooldown_until = NULL, "
            "fail_streak = 0, last_error = NULL, updated_at = ? WHERE id = ?",
            (_now(), account_id),
        )


    def release_expired_cooldowns(self) -> int:
        now = _now()
        return self.execute(
            "UPDATE accounts SET status = 'healthy', cooldown_until = NULL, fail_streak = 0 "
            "WHERE status = 'cooldown' AND cooldown_until IS NOT NULL AND cooldown_until <= ?",
            (now,),
        )

    # -- 代理 -----------------------------------------------------------
    def create_proxy(
        self,
        url: str,
        *,
        label: str = "",
        enabled: bool = True,
        scheme: str = "http",
    ) -> int:
        from .proxy import redact_proxy

        now = _now()
        return self.execute(
            """
            INSERT INTO proxies (label, url_enc, url_redacted, scheme, enabled,
                                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (label, self._box.encrypt(url), redact_proxy(url), scheme, 1 if enabled else 0, now, now),
        )

    def get_proxy(self, proxy_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM proxies WHERE id = ?", (proxy_id,))

    def get_proxy_by_url(self, url: str) -> sqlite3.Row | None:
        for row in self.query("SELECT * FROM proxies"):
            if self.decrypt_proxy_url(row) == url:
                return row
        return None

    def list_proxies(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM proxies ORDER BY id ASC")

    def decrypt_proxy_url(self, row: sqlite3.Row | dict) -> str:
        enc = row["url_enc"] if not isinstance(row, dict) else row.get("url_enc")
        return self._box.decrypt(enc or "")

    def proxy_public(self, row: sqlite3.Row) -> dict:
        """把代理行转成可直接返回给前端的 dict（URL 脱敏）。"""
        return {
            "id": row["id"],
            "label": row["label"] or f"proxy-{row['id']}",
            "url": row["url_redacted"],
            "scheme": row["scheme"],
            "enabled": bool(row["enabled"]),
            "status": row["status"],
            "latency_ms": row["latency_ms"],
            "last_error": row["last_error"],
            "last_check_at": row["last_check_at"],
            "success_count": row["success_count"],
            "failure_count": row["failure_count"],
            "created_at": row["created_at"],
        }

    def update_proxy(self, proxy_id: int, **fields: Any) -> None:
        allowed = {"label", "enabled", "status", "latency_ms", "last_error", "last_check_at"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if "enabled" in updates and isinstance(updates["enabled"], bool):
            updates["enabled"] = 1 if updates["enabled"] else 0
        updates["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in updates)
        self.execute(f"UPDATE proxies SET {sets} WHERE id = ?", (*updates.values(), proxy_id))

    def delete_proxy(self, proxy_id: int) -> None:
        self.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))

    def record_proxy_result(self, proxy_id: int, *, ok: bool, latency_ms: float | None,
                            error: str | None = None) -> None:
        self.execute(
            """
            UPDATE proxies SET
                status = ?,
                latency_ms = ?,
                last_error = ?,
                last_check_at = ?,
                success_count = success_count + ?,
                failure_count = failure_count + ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                "healthy" if ok else "unhealthy",
                latency_ms,
                error,
                _now(),
                1 if ok else 0,
                0 if ok else 1,
                _now(),
                proxy_id,
            ),
        )

    # -- 代理池 ---------------------------------------------------------
    def create_pool(
        self,
        name: str,
        *,
        strategy: str = "round_robin",
        affinity: str = "sticky",
        cooldown: float = 60.0,
        fallback_direct: bool = True,
        note: str = "",
        enabled: bool = True,
    ) -> int:
        now = _now()
        return self.execute(
            """
            INSERT INTO proxy_pools (name, strategy, affinity, cooldown, fallback_direct,
                                     enabled, note, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (name, strategy, affinity, cooldown, 1 if fallback_direct else 0,
             1 if enabled else 0, note, now, now),
        )

    def get_pool(self, pool_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM proxy_pools WHERE id = ?", (pool_id,))

    def list_pools(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM proxy_pools ORDER BY id ASC")

    def update_pool(self, pool_id: int, **fields: Any) -> None:
        allowed = {"name", "strategy", "affinity", "cooldown", "fallback_direct", "enabled", "note"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        for key in ("fallback_direct", "enabled"):
            if key in updates and isinstance(updates[key], bool):
                updates[key] = 1 if updates[key] else 0
        updates["updated_at"] = _now()
        sets = ", ".join(f"{k} = ?" for k in updates)
        self.execute(f"UPDATE proxy_pools SET {sets} WHERE id = ?", (*updates.values(), pool_id))

    def delete_pool(self, pool_id: int) -> None:
        self.execute("DELETE FROM proxy_pools WHERE id = ?", (pool_id,))

    def set_pool_members(self, pool_id: int, proxy_ids: Sequence[int]) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM pool_members WHERE pool_id = ?", (pool_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO pool_members(pool_id, proxy_id, weight) VALUES (?,?,1)",
                [(pool_id, pid) for pid in proxy_ids],
            )
            conn.execute("UPDATE proxy_pools SET updated_at = ? WHERE id = ?", (_now(), pool_id))

    def add_pool_members(self, pool_id: int, proxy_ids: Sequence[int]) -> None:
        with self.tx() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO pool_members(pool_id, proxy_id, weight) VALUES (?,?,1)",
                [(pool_id, pid) for pid in proxy_ids],
            )

    def list_pool_members(self, pool_id: int) -> list[sqlite3.Row]:
        return self.query(
            """
            SELECT p.* FROM proxies p
            JOIN pool_members m ON m.proxy_id = p.id
            WHERE m.pool_id = ?
            ORDER BY p.id ASC
            """,
            (pool_id,),
        )

    def list_pool_member_ids(self, pool_id: int) -> list[int]:
        return [
            r["proxy_id"]
            for r in self.query(
                "SELECT proxy_id FROM pool_members WHERE pool_id = ? ORDER BY proxy_id",
                (pool_id,),
            )
        ]

    # -- 会话粘性 -------------------------------------------------------
    def get_session(self, session_key: str) -> sqlite3.Row | None:
        return self.one(
            "SELECT * FROM sessions WHERE session_key = ? AND expires_at > ?",
            (session_key, _now()),
        )

    def upsert_session(
        self,
        session_key: str,
        *,
        account_id: int,
        ttl_seconds: float,
        pool_id: int | None = None,
        proxy_url: str | None = None,
    ) -> None:
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        self.execute(
            """
            INSERT INTO sessions(session_key, account_id, pool_id, proxy_url, hits, created_at, expires_at)
            VALUES (?,?,?,?,1,?,?)
            ON CONFLICT(session_key) DO UPDATE SET
                account_id = excluded.account_id,
                pool_id = excluded.pool_id,
                proxy_url = COALESCE(excluded.proxy_url, sessions.proxy_url),
                hits = sessions.hits + 1,
                expires_at = excluded.expires_at
            """,
            (
                session_key,
                account_id,
                pool_id,
                proxy_url,
                now.isoformat(),
                (now + timedelta(seconds=ttl_seconds)).isoformat(),
            ),
        )

    def bind_session_proxy(self, session_key: str, proxy_url: str | None) -> None:
        self.execute(
            "UPDATE sessions SET proxy_url = ? WHERE session_key = ?",
            (proxy_url, session_key),
        )

    def touch_session(self, session_key: str, ttl_seconds: float) -> None:
        from datetime import timedelta

        self.execute(
            "UPDATE sessions SET expires_at = ? WHERE session_key = ?",
            ((datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(), session_key),
        )

    def drop_session(self, session_key: str) -> None:
        self.execute("DELETE FROM sessions WHERE session_key = ?", (session_key,))

    def purge_sessions(self) -> int:
        return self.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now(),))

    def list_sessions(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM sessions WHERE expires_at > ? ORDER BY expires_at DESC LIMIT ?",
            (_now(), limit),
        )

    # -- 日志 -----------------------------------------------------------
    def log_usage(self, entry: dict) -> int:
        return self.execute(
            """
            INSERT INTO usage_logs (
                created_at, account_id, account_name, session_key, method, path, model,
                channel, status_code, duration_ms, attempts, proxy_label,
                prompt_tokens, completion_tokens, total_tokens, error
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                entry.get("created_at") or _now(),
                entry.get("account_id"),
                entry.get("account_name"),
                entry.get("session_key"),
                entry.get("method"),
                entry.get("path"),
                entry.get("model"),
                entry.get("channel"),
                entry.get("status_code"),
                entry.get("duration_ms"),
                entry.get("attempts", 1),
                entry.get("proxy_label"),
                entry.get("prompt_tokens"),
                entry.get("completion_tokens"),
                entry.get("total_tokens"),
                entry.get("error"),
            ),
        )

    def list_logs(self, limit: int = 100, offset: int = 0, account_id: int | None = None) -> list[sqlite3.Row]:
        if account_id:
            return self.query(
                "SELECT * FROM usage_logs WHERE account_id = ? "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (account_id, limit, offset),
            )
        return self.query(
            "SELECT * FROM usage_logs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        )

    def prune_logs(self, keep: int) -> int:
        row = self.one("SELECT COUNT(*) AS c FROM usage_logs")
        total = int(row["c"]) if row else 0
        if total <= keep:
            return 0
        return self.execute(
            "DELETE FROM usage_logs WHERE id IN ("
            "  SELECT id FROM usage_logs ORDER BY id ASC LIMIT ?"
            ")",
            (total - keep,),
        )

    def log_summary(self, since_iso: str) -> list[sqlite3.Row]:
        return self.query(
            """
            SELECT account_id, account_name,
                   COUNT(*) AS requests,
                   SUM(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END) AS ok,
                   SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) AS failed,
                   SUM(COALESCE(total_tokens, 0)) AS tokens,
                   ROUND(AVG(duration_ms), 1) AS avg_ms
            FROM usage_logs
            WHERE created_at >= ?
            GROUP BY account_id
            ORDER BY requests DESC
            """,
            (since_iso,),
        )


_store: Store | None = None


def get_store(settings: Settings | None = None, reload: bool = False) -> Store:
    global _store
    if _store is None or reload:
        if settings is None:
            from .config import get_settings

            settings = get_settings()
        _store = Store(settings)
        _store.init()
    return _store


def reset_store() -> None:
    global _store
    if _store is not None:
        _store.close()
    _store = None


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    return data

