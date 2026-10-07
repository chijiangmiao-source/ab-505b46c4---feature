"""SQLite 持久化层。

授权族、刷新凭证与轮换记录全部落盘到 SQLite 文件（挂载卷），
因此服务进程/容器重启后，已提交的轮换结果仍可凭原标识恢复。
"""
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS families (
    family_id         TEXT PRIMARY KEY,
    terminal_id       TEXT NOT NULL UNIQUE,          -- 一个终端同一时刻绑定一个授权族
    status            TEXT NOT NULL DEFAULT 'active', -- active | revoked
    revocation_reason TEXT,
    generation        INTEGER NOT NULL DEFAULT 1,     -- 当前代次，初始为 1
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id       TEXT NOT NULL REFERENCES families(family_id),
    credential_hash TEXT NOT NULL UNIQUE,             -- 仅存哈希，避免明文落库
    generation      INTEGER NOT NULL,
    status          TEXT NOT NULL,                    -- current | rotated | revoked
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rotations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id           TEXT NOT NULL REFERENCES families(family_id),
    terminal_id         TEXT NOT NULL,
    rotation_id         TEXT NOT NULL,                -- 客户端提供的稳定轮换标识（幂等键）
    old_credential_hash TEXT NOT NULL,
    new_credential      TEXT NOT NULL,                -- 后继凭证明文：重放时必须原样取回
    new_credential_hash TEXT NOT NULL,
    new_generation      INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE (family_id, old_credential_hash, rotation_id)  -- 幂等约束
);

-- 轮换链审计：仅追加。只有「实际改变授权族」的动作才会落记录
-- （接受轮换 / 异标识重用触发的撤销）；幂等重放不产生记录。
-- 这里只保存不可逆哈希，审计结果永远无法还原可用凭证。
CREATE TABLE IF NOT EXISTS rotation_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id            TEXT NOT NULL REFERENCES families(family_id),
    event_type           TEXT NOT NULL,                   -- rotation | revocation
    rotation_id          TEXT,                             -- 触发记录提交时使用的稳定轮换标识
    old_credential_hash  TEXT,                             -- 轮换前凭证指纹来源（撤销记录为被重用的旧凭证）
    new_credential_hash  TEXT,                             -- 轮换后凭证指纹来源；撤销记录为 NULL
    resulting_generation INTEGER,                          -- 结果代次；撤销时代次不推进
    status               TEXT NOT NULL,                    -- accepted | revoked
    reason               TEXT,
    created_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rotation_events_family
    ON rotation_events (family_id, id);
"""


class Storage:
    """单连接 + 进程级锁；所有写操作在 BEGIN IMMEDIATE 事务中完成。"""

    def __init__(self, path):
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self):
        """串行化的事务块：提交后数据立即可靠，异常则整体回滚。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ---- families ----

    def insert_family(self, family_id, terminal_id, created_at):
        self._conn.execute(
            "INSERT INTO families (family_id, terminal_id, status, generation, created_at)"
            " VALUES (?, ?, 'active', 1, ?)",
            (family_id, terminal_id, created_at),
        )

    def find_family_by_terminal(self, terminal_id):
        row = self._conn.execute(
            "SELECT * FROM families WHERE terminal_id = ?", (terminal_id,)
        ).fetchone()
        return dict(row) if row else None

    def find_family(self, family_id):
        row = self._conn.execute(
            "SELECT * FROM families WHERE family_id = ?", (family_id,)
        ).fetchone()
        return dict(row) if row else None

    def set_family_generation(self, family_id, generation):
        self._conn.execute(
            "UPDATE families SET generation = ? WHERE family_id = ?",
            (generation, family_id),
        )

    def revoke_family(self, family_id, reason):
        self._conn.execute(
            "UPDATE families SET status = 'revoked', revocation_reason = ?"
            " WHERE family_id = ?",
            (reason, family_id),
        )

    # ---- credentials ----

    def insert_credential(self, family_id, credential_hash, generation, status, created_at):
        self._conn.execute(
            "INSERT INTO credentials (family_id, credential_hash, generation, status, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (family_id, credential_hash, generation, status, created_at),
        )

    def find_credential(self, family_id, credential_hash):
        row = self._conn.execute(
            "SELECT * FROM credentials WHERE family_id = ? AND credential_hash = ?",
            (family_id, credential_hash),
        ).fetchone()
        return dict(row) if row else None

    def list_credentials(self, family_id):
        rows = self._conn.execute(
            "SELECT * FROM credentials WHERE family_id = ? ORDER BY id ASC",
            (family_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_credential_status(self, credential_id, status):
        self._conn.execute(
            "UPDATE credentials SET status = ? WHERE id = ?", (status, credential_id)
        )

    def revoke_all_credentials(self, family_id):
        self._conn.execute(
            "UPDATE credentials SET status = 'revoked' WHERE family_id = ?", (family_id,)
        )

    # ---- rotations ----

    def insert_rotation(self, family_id, terminal_id, rotation_id,
                        old_credential_hash, new_credential, new_credential_hash,
                        new_generation, created_at):
        self._conn.execute(
            "INSERT INTO rotations (family_id, terminal_id, rotation_id,"
            " old_credential_hash, new_credential, new_credential_hash, new_generation, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (family_id, terminal_id, rotation_id, old_credential_hash,
             new_credential, new_credential_hash, new_generation, created_at),
        )

    def find_rotation_by_old_hash(self, family_id, old_credential_hash):
        row = self._conn.execute(
            "SELECT * FROM rotations WHERE family_id = ? AND old_credential_hash = ?",
            (family_id, old_credential_hash),
        ).fetchone()
        return dict(row) if row else None

    def count_rotations(self, family_id):
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM rotations WHERE family_id = ?", (family_id,)
        ).fetchone()
        return row["n"]

    # ---- rotation_events（轮换链审计，仅追加）----

    def insert_rotation_event(self, family_id, event_type, created_at,
                              rotation_id=None, old_credential_hash=None,
                              new_credential_hash=None, resulting_generation=None,
                              status=None, reason=None):
        self._conn.execute(
            "INSERT INTO rotation_events (family_id, event_type, rotation_id,"
            " old_credential_hash, new_credential_hash, resulting_generation,"
            " status, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (family_id, event_type, rotation_id, old_credential_hash,
             new_credential_hash, resulting_generation, status, reason, created_at),
        )

    def list_rotation_events(self, family_id):
        rows = self._conn.execute(
            "SELECT * FROM rotation_events WHERE family_id = ? ORDER BY id ASC",
            (family_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_rotation_events(self, family_id):
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM rotation_events WHERE family_id = ?",
            (family_id,),
        ).fetchone()
        return row["n"]
