"""同步状态库：记录「哪两个会话是同一个」「上次同步时的内容指纹」。

没有它，每次都要靠猜测配对；有了它，重复运行是幂等的。
"""
import json
import os
import sqlite3
import time

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS pairs (
    pair_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    zcode_id     TEXT UNIQUE,
    wb_id        TEXT UNIQUE,
    workspace    TEXT,
    fingerprint  TEXT,
    z_hash       TEXT,
    w_hash       TEXT,
    z_updated    INTEGER,
    w_updated    INTEGER,
    synced_at    INTEGER,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_pairs_fp ON pairs(fingerprint);

CREATE TABLE IF NOT EXISTS memory_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    canonical_hash TEXT,
    z_hash        TEXT,
    w_hash        TEXT,
    updated_at    INTEGER
);

CREATE TABLE IF NOT EXISTS runs (
    run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER,
    mode       TEXT,
    summary    TEXT
);

CREATE TABLE IF NOT EXISTS tombstones (
    fingerprint TEXT PRIMARY KEY,
    side        TEXT,
    created_at  INTEGER,
    note        TEXT
);
"""


def connect():
    paths.ensure_dirs()
    c = sqlite3.connect(paths.STATE_DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


# ---------------------------------------------------------------- 配对
def all_pairs(c):
    return [dict(r) for r in c.execute("select * from pairs")]


def get_pair(c, zcode_id=None, wb_id=None, fingerprint=None):
    if zcode_id:
        r = c.execute("select * from pairs where zcode_id=?", (zcode_id,)).fetchone()
        return dict(r) if r else None
    if wb_id:
        r = c.execute("select * from pairs where wb_id=?", (wb_id,)).fetchone()
        return dict(r) if r else None
    if fingerprint:
        r = c.execute("select * from pairs where fingerprint=?", (fingerprint,)).fetchone()
        return dict(r) if r else None
    return None


def upsert_pair(c, zcode_id, wb_id, workspace, fingerprint,
                z_hash=None, w_hash=None, z_updated=None, w_updated=None, note=None):
    now = int(time.time() * 1000)
    row = get_pair(c, zcode_id=zcode_id)
    if row:
        c.execute("""update pairs set wb_id=?, workspace=?, fingerprint=?,
                     z_hash=coalesce(?, z_hash), w_hash=coalesce(?, w_hash),
                     z_updated=coalesce(?, z_updated), w_updated=coalesce(?, w_updated),
                     synced_at=?, note=coalesce(?, note) where pair_id=?""",
                  (wb_id, workspace, fingerprint, z_hash, w_hash, z_updated, w_updated,
                   now, note, row["pair_id"]))
        return row["pair_id"]
    cur = c.execute("""insert into pairs
        (zcode_id, wb_id, workspace, fingerprint, z_hash, w_hash, z_updated, w_updated, synced_at, note)
        values (?,?,?,?,?,?,?,?,?,?)""",
                    (zcode_id, wb_id, workspace, fingerprint, z_hash, w_hash,
                     z_updated, w_updated, now, note))
    return cur.lastrowid


def update_hashes(c, pair_id, z_hash=None, w_hash=None, z_updated=None, w_updated=None):
    c.execute("""update pairs set z_hash=coalesce(?, z_hash), w_hash=coalesce(?, w_hash),
                 z_updated=coalesce(?, z_updated), w_updated=coalesce(?, w_updated),
                 synced_at=? where pair_id=?""",
              (z_hash, w_hash, z_updated, w_updated, int(time.time() * 1000), pair_id))


def drop_pair(c, pair_id):
    c.execute("delete from pairs where pair_id=?", (pair_id,))


# ---------------------------------------------------------------- 记忆状态
def get_memory_state(c):
    r = c.execute("select * from memory_state where id=1").fetchone()
    if not r:
        return {"canonical_hash": None, "z_hash": None, "w_hash": None, "updated_at": None}
    return dict(r)


def set_memory_state(c, **kw):
    cur = get_memory_state(c)
    cur.update(kw)
    c.execute("""insert into memory_state (id, canonical_hash, z_hash, w_hash, updated_at)
                 values (1,?,?,?,?)
                 on conflict(id) do update set canonical_hash=excluded.canonical_hash,
                   z_hash=excluded.z_hash, w_hash=excluded.w_hash, updated_at=excluded.updated_at""",
              (cur["canonical_hash"], cur["z_hash"], cur["w_hash"],
               int(time.time() * 1000)))


# ---------------------------------------------------------------- 墓碑（已删除的对话）
def tombstones(c):
    return {r["fingerprint"]: dict(r) for r in c.execute("select * from tombstones")}


def add_tombstone(c, fingerprint, side, note=""):
    if not fingerprint:
        return
    c.execute("""insert into tombstones (fingerprint, side, created_at, note) values (?,?,?,?)
                 on conflict(fingerprint) do update set side=excluded.side, note=excluded.note""",
              (fingerprint, side, int(time.time() * 1000), note))


def drop_tombstone(c, fingerprint):
    c.execute("delete from tombstones where fingerprint=?", (fingerprint,))


# ---------------------------------------------------------------- 运行记录
def log_run(c, mode, summary):
    c.execute("insert into runs (started_at, mode, summary) values (?,?,?)",
              (int(time.time() * 1000), mode, json.dumps(summary, ensure_ascii=False)))
    c.commit()
