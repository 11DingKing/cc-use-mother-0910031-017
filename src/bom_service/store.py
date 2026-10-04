"""SQLite 仓储。

仅依赖标准库。所有写入在单一互斥锁内串行化，发布事务在锁内完成
状态复核，避免两个签署请求同时把同一版本判定为可发布。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

from .models import Line, Part, Revision, Snapshot, State, Substitute

SCHEMA = """
CREATE TABLE IF NOT EXISTS parts (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    unit        TEXT NOT NULL,
    conversions TEXT NOT NULL DEFAULT '{}',
    active      INTEGER NOT NULL DEFAULT 1,
    obsolete_on TEXT
);
CREATE TABLE IF NOT EXISTS revisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    code           TEXT NOT NULL,
    branch         TEXT NOT NULL DEFAULT 'main',
    version        INTEGER NOT NULL,
    state          TEXT NOT NULL,
    valid_from     TEXT NOT NULL,
    valid_to       TEXT,
    reason         TEXT NOT NULL DEFAULT '',
    parent_version INTEGER,
    base_version   INTEGER,
    created_at     TEXT NOT NULL,
    signed_by      TEXT,
    UNIQUE(code, branch, version)
);
CREATE TABLE IF NOT EXISTS lines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rev_code   TEXT NOT NULL,
    rev_branch TEXT NOT NULL,
    rev_ver    INTEGER NOT NULL,
    child      TEXT NOT NULL,
    qty        REAL NOT NULL,
    unit       TEXT NOT NULL,
    scrap      REAL NOT NULL DEFAULT 0,
    valid_from TEXT NOT NULL,
    valid_to   TEXT,
    note       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS substitutes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    rev_code   TEXT NOT NULL,
    rev_branch TEXT NOT NULL,
    rev_ver    INTEGER NOT NULL,
    line_child TEXT NOT NULL,
    alt        TEXT NOT NULL,
    ratio      REAL NOT NULL,
    unit       TEXT NOT NULL,
    priority   INTEGER NOT NULL DEFAULT 0,
    valid_from TEXT NOT NULL,
    valid_to   TEXT,
    note       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS snapshots (
    code      TEXT NOT NULL,
    branch    TEXT NOT NULL,
    version   INTEGER NOT NULL,
    signed_at TEXT NOT NULL,
    signed_by TEXT NOT NULL,
    checksum  TEXT NOT NULL,
    payload   TEXT NOT NULL,
    PRIMARY KEY (code, branch, version)
);
"""


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _s(value: date | None) -> str | None:
    return value.isoformat() if value else None


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ parts
    def upsert_part(self, part: Part) -> Part:
        with self._lock:
            self._conn.execute(
                """INSERT INTO parts(code, name, unit, conversions, active, obsolete_on)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(code) DO UPDATE SET
                     name=excluded.name, unit=excluded.unit,
                     conversions=excluded.conversions, active=excluded.active,
                     obsolete_on=excluded.obsolete_on""",
                (
                    part.code,
                    part.name,
                    part.unit,
                    json.dumps(part.conversions, ensure_ascii=False, sort_keys=True),
                    1 if part.active else 0,
                    _s(part.obsolete_on),
                ),
            )
            self._conn.commit()
        return part

    def get_part(self, code: str) -> Part | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM parts WHERE code=?", (code,)).fetchone()
        return self._row_to_part(row) if row else None

    def list_parts(self) -> list[Part]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM parts ORDER BY code").fetchall()
        return [self._row_to_part(row) for row in rows]

    @staticmethod
    def _row_to_part(row: sqlite3.Row) -> Part:
        return Part(
            code=row["code"],
            name=row["name"],
            unit=row["unit"],
            conversions=json.loads(row["conversions"]),
            active=bool(row["active"]),
            obsolete_on=_d(row["obsolete_on"]),
        )

    # -------------------------------------------------------------- revisions
    def next_version(self, code: str, branch: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS m FROM revisions WHERE code=? AND branch=?",
                (code, branch),
            ).fetchone()
            return (row["m"] or 0) + 1

    def insert_revision(self, rev: Revision, base_version: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO revisions(code, branch, version, state, valid_from, valid_to,
                      reason, parent_version, base_version, created_at, signed_by)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rev.code,
                    rev.branch,
                    rev.version,
                    rev.state.name,
                    _s(rev.valid_from),
                    _s(rev.valid_to),
                    rev.reason,
                    rev.parent_version,
                    base_version,
                    rev.created_at.isoformat(),
                    rev.signed_by,
                ),
            )
            self._write_contents(rev)
            self._conn.commit()

    def _write_contents(self, rev: Revision) -> None:
        self._conn.execute(
            "DELETE FROM lines WHERE rev_code=? AND rev_branch=? AND rev_ver=?",
            (rev.code, rev.branch, rev.version),
        )
        self._conn.execute(
            "DELETE FROM substitutes WHERE rev_code=? AND rev_branch=? AND rev_ver=?",
            (rev.code, rev.branch, rev.version),
        )
        for line in rev.lines:
            self._conn.execute(
                """INSERT INTO lines(rev_code, rev_branch, rev_ver, child, qty, unit,
                      scrap, valid_from, valid_to, note)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    rev.code,
                    rev.branch,
                    rev.version,
                    line.child,
                    line.qty,
                    line.unit,
                    line.scrap,
                    _s(line.valid_from),
                    _s(line.valid_to),
                    line.note,
                ),
            )
        for child, subs in rev.substitutes.items():
            for sub in subs:
                self._conn.execute(
                    """INSERT INTO substitutes(rev_code, rev_branch, rev_ver, line_child,
                          alt, ratio, unit, priority, valid_from, valid_to, note)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        rev.code,
                        rev.branch,
                        rev.version,
                        child,
                        sub.alt,
                        sub.ratio,
                        sub.unit,
                        sub.priority,
                        _s(sub.valid_from),
                        _s(sub.valid_to),
                        sub.note,
                    ),
                )

    def save_contents(self, rev: Revision) -> None:
        """覆盖版本内容，仅草拟/待确认状态允许。"""
        with self._lock:
            current = self.get_revision(rev.code, rev.branch, rev.version)
            if current is None:
                raise ValueError("版本不存在")
            if current.state not in (State.DRAFT, State.PENDING):
                from .models import BomError

                raise BomError(
                    "REVISION_FROZEN",
                    f"{rev.rev_id} 已签署冻结，不能修改",
                )
            self._write_contents(rev)
            self._conn.commit()

    def get_revision(self, code: str, branch: str, version: int) -> Revision | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM revisions WHERE code=? AND branch=? AND version=?",
                (code, branch, version),
            ).fetchone()
            return self._row_to_revision(row) if row else None

    def list_revisions(self, code: str | None = None, branch: str | None = None) -> list[Revision]:
        sql = "SELECT * FROM revisions WHERE 1=1"
        args: list = []
        if code:
            sql += " AND code=?"
            args.append(code)
        if branch:
            sql += " AND branch=?"
            args.append(branch)
        sql += " ORDER BY code, branch, version"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
            return [self._row_to_revision(row) for row in rows]

    def _row_to_revision(self, row: sqlite3.Row) -> Revision:
        lines_rows = self._conn.execute(
            "SELECT * FROM lines WHERE rev_code=? AND rev_branch=? AND rev_ver=? ORDER BY id",
            (row["code"], row["branch"], row["version"]),
        ).fetchall()
        subs_rows = self._conn.execute(
            "SELECT * FROM substitutes WHERE rev_code=? AND rev_branch=? AND rev_ver=? ORDER BY priority, id",
            (row["code"], row["branch"], row["version"]),
        ).fetchall()
        substitutes: dict[str, list[Substitute]] = {}
        for sr in subs_rows:
            substitutes.setdefault(sr["line_child"], []).append(
                Substitute(
                    alt=sr["alt"],
                    ratio=sr["ratio"],
                    unit=sr["unit"],
                    priority=sr["priority"],
                    valid_from=_d(sr["valid_from"]),
                    valid_to=_d(sr["valid_to"]),
                    note=sr["note"],
                )
            )
        return Revision(
            code=row["code"],
            version=row["version"],
            state=State[row["state"]],
            valid_from=_d(row["valid_from"]),
            valid_to=_d(row["valid_to"]),
            reason=row["reason"],
            parent_version=row["parent_version"],
            base_version=row["base_version"],
            branch=row["branch"],
            created_at=datetime.fromisoformat(row["created_at"]),
            signed_by=row["signed_by"],
            lines=[
                Line(
                    child=lr["child"],
                    qty=lr["qty"],
                    unit=lr["unit"],
                    scrap=lr["scrap"],
                    valid_from=_d(lr["valid_from"]),
                    valid_to=_d(lr["valid_to"]),
                    note=lr["note"],
                )
                for lr in lines_rows
            ],
            substitutes=substitutes,
        )

    def latest_frozen(self, code: str, branch: str = "main") -> Revision | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM revisions WHERE code=? AND branch=? AND state='FROZEN'
                   ORDER BY version DESC LIMIT 1""",
                (code, branch),
            ).fetchone()
            return self._row_to_revision(row) if row else None

    def frozen_revision_at(self, code: str, on_date: date, branch: str = "main") -> Revision | None:
        """返回在 ``on_date`` 当日生效的已发布版本（半开区间）。"""
        iso = on_date.isoformat()
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM revisions
                   WHERE code=? AND branch=? AND state='FROZEN'
                     AND valid_from<=? AND (valid_to IS NULL OR valid_to>?)
                   ORDER BY version DESC LIMIT 1""",
                (code, branch, iso, iso),
            ).fetchone()
            return self._row_to_revision(row) if row else None

    def has_revisions(self, code: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM revisions WHERE code=? LIMIT 1", (code,)
            ).fetchone()
        return row is not None

    def mark_pending(self, rev: Revision) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE revisions SET state='PENDING' WHERE code=? AND branch=? AND version=?",
                (rev.code, rev.branch, rev.version),
            )
            self._conn.commit()

    def mark_frozen(self, rev: Revision, signed_by: str) -> None:
        """把版本置为已发布；调用方必须已持有锁并完成全部校验。"""
        self._conn.execute(
            "UPDATE revisions SET state='FROZEN', signed_by=? WHERE code=? AND branch=? AND version=?",
            (signed_by, rev.code, rev.branch, rev.version),
        )

    def set_valid_to(self, code: str, branch: str, version: int, valid_to: date) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE revisions SET valid_to=? WHERE code=? AND branch=? AND version=?",
                (_s(valid_to), code, branch, version),
            )

    def commit(self) -> None:
        self._conn.commit()

    # -------------------------------------------------------------- snapshots
    def put_snapshot(self, snap: Snapshot, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO snapshots(code, branch, version, signed_at, signed_by, checksum, payload)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(code, branch, version) DO NOTHING""",
                (
                    snap.code,
                    snap.branch,
                    snap.version,
                    snap.signed_at.isoformat(),
                    snap.signed_by,
                    snap.checksum,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
            self._conn.commit()

    def get_snapshot_payload(self, code: str, branch: str, version: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM snapshots WHERE code=? AND branch=? AND version=?",
                (code, branch, version),
            ).fetchone()
        return json.loads(row["payload"]) if row else None

    def list_snapshots(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT code, branch, version, signed_at, signed_by, checksum FROM snapshots ORDER BY signed_at"
            ).fetchall()
        return [dict(row) for row in rows]
