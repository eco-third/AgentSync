"""WorkBuddy 适配器：sessions 表 + projects/<slug>/<sessionId>.jsonl <-> 统一模型。

一个会话 = DB 里一行 + 一个 jsonl 事件流；事件靠 UUIDv7 的 id 排序、parentId 串链。
"""
import json
import os
import sqlite3
import subprocess
import time

from .model import (Conversation, Event, KIND_ASSISTANT, KIND_REASONING, KIND_TOOL_CALL,
                    KIND_TOOL_RESULT, KIND_USER, new_uuid, norm_usage, reset_seq,
                    slugify_path, strip_harness_noise, uuid7, workspace_name)

SIDE = "wb"
ACTIVE_STATUS = {"pending", "working", "planning"}


def is_running():
    try:
        out = subprocess.run(["tasklist"], capture_output=True, text=True,
                             errors="replace", timeout=20).stdout.lower()
        return "workbuddy" in out
    except Exception:
        return False


def connect(db_path, readonly=False):
    if readonly:
        c = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True, timeout=15)
    else:
        c = sqlite3.connect(db_path, timeout=25)
        c.execute("pragma busy_timeout=25000")
    c.row_factory = sqlite3.Row
    return c


def jsonl_path(projects_dir, cwd, sid):
    return os.path.join(projects_dir, slugify_path(cwd), sid + ".jsonl")


# ---------------------------------------------------------------- 读
def list_sessions(db_path, include_deleted=False):
    c = connect(db_path, readonly=True)
    rows = []
    where = "" if include_deleted else "where deleted_at is null "
    for r in c.execute(f"""select id, cwd, title, custom_title, created_at, updated_at,
                                 last_activity_at, status, deleted_at, conversation_origin
                          from sessions {where}order by created_at"""):
        d = dict(r)
        d["workspace"] = workspace_name(d["cwd"])
        d["title"] = d["custom_title"] or d["title"] or "(无标题)"
        d["deleted"] = d["deleted_at"] is not None
        rows.append(d)
    c.close()
    return rows


def read_conversation(projects_dir, row):
    path = jsonl_path(projects_dir, row["cwd"], row["id"])
    events = []
    if os.path.exists(path):
        events = _parse_jsonl(path)
    return Conversation(side=SIDE, native_id=row["id"], workspace=row["workspace"],
                        workspace_path=row["cwd"], title=row["title"],
                        created=row["created_at"], updated=row["updated_at"],
                        events=events, status=row["status"] or "",
                        deleted=row.get("deleted", False))


def _usage_model(o):
    """取出这一行携带的真实 token 用量与模型名。

    WorkBuddy 不把用量放在顶层，而是 `message.usage` 或 `providerData.rawUsage`；
    实测 800+ 行都有，所以写回 Zcode 时不必估算。

    两个源各有短板，取长补短：
      · `message.usage`  —— input/output/total/cache_read 语义明确，cache 有值；
      · `providerData.rawUsage` —— 多出 `completion_tokens_details.reasoning_tokens`。
    实测同一个 step 里 rawUsage 的 cache 字段可能报 0，所以以 message.usage 为准，
    reasoning 从 rawUsage 补。
    """
    pd = o.get("providerData") if isinstance(o.get("providerData"), dict) else {}
    m = o.get("message")
    mu = m.get("usage") if isinstance(m, dict) else None
    ru = pd.get("rawUsage") if isinstance(pd.get("rawUsage"), dict) else None
    u = norm_usage(mu) or norm_usage(ru)
    if u is not None and isinstance(ru, dict):
        if not u["reasoning"]:
            det = ru.get("completion_tokens_details")
            if isinstance(det, dict) and isinstance(det.get("reasoning_tokens"), (int, float)):
                u["reasoning"] = int(det["reasoning_tokens"])
        if not u["cache_read"]:
            for k in ("cache_read_input_tokens", "prompt_cache_hit_tokens", "cached_tokens"):
                v = ru.get(k)
                if isinstance(v, (int, float)) and v:
                    u["cache_read"] = int(v)
                    break
    model = pd.get("requestModelId") or pd.get("model") or ""
    return u, model


def _parse_jsonl(path):
    events = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get("type")
            ts = o.get("timestamp") or 0
            usage, model = _usage_model(o)

            def ev(kind, **kw):
                return Event(kind, ts, usage=usage, model=model, **kw)

            if t == "message":
                role = o.get("role")
                for b in o.get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "input_text":
                        txt = strip_harness_noise(b.get("text") or "")
                        if txt:
                            events.append(ev(KIND_USER, text=txt))
                    elif b.get("type") == "output_text":
                        txt = (b.get("text") or "").strip()
                        if txt:
                            events.append(ev(KIND_ASSISTANT, text=txt))
            elif t == "reasoning":
                txt = "".join(b.get("text") or "" for b in (o.get("rawContent") or [])
                              if isinstance(b, dict)).strip()
                if txt:
                    events.append(ev(KIND_REASONING, text=txt))
            elif t == "function_call":
                events.append(ev(KIND_TOOL_CALL, name=o.get("name") or "tool",
                                 call_id=o.get("callId") or "", args=o.get("arguments") or ""))
            elif t == "function_call_result":
                out = o.get("output") or {}
                text = out.get("text") if isinstance(out, dict) else str(out)
                events.append(ev(KIND_TOOL_RESULT, name=o.get("name") or "tool",
                                 call_id=o.get("callId") or "", output=text or "",
                                 status=o.get("status") or "completed"))
    return events


def read_all(db_path, projects_dir, include_deleted=False):
    rows = list_sessions(db_path, include_deleted=include_deleted)
    convs = []
    for r in rows:
        convs.append(read_conversation(projects_dir, r))
    return convs, rows


# ---------------------------------------------------------------- 写
def _render_jsonl(conv, sid, cwd):
    created = conv.created or int(time.time() * 1000)
    reset_seq(created - 1)
    lines = [json.dumps({"type": "session-meta", "id": uuid7(created), "sessionId": sid,
                         "timestamp": created, "meta": {"codebuddy.ai/hostKind": "unopted",
                                                        "agentsync": {"peer": "zcode",
                                                                      "peerId": conv.native_id}}},
                        ensure_ascii=False)]
    if conv.title:
        lines.append(json.dumps({"id": uuid7(created + 1), "timestamp": created + 1,
                                 "type": "ai-title", "aiTitle": conv.title,
                                 "sessionId": sid, "cwd": cwd}, ensure_ascii=False))
    reset_seq(created)
    last_id = None
    for e in sorted(conv.events, key=lambda x: x.ts):
        ts = e.ts or created
        if e.kind == KIND_USER:
            obj = {"id": uuid7(ts), "timestamp": ts, "type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": e.text}],
                   "providerData": {"startsNewUserRequest": True, "agent": "cli"}}
        elif e.kind == KIND_ASSISTANT:
            obj = {"id": uuid7(ts), "timestamp": ts, "type": "message", "role": "assistant",
                   "status": "completed",
                   "content": [{"type": "output_text", "text": e.text}]}
        elif e.kind == KIND_REASONING:
            obj = {"id": uuid7(ts), "timestamp": ts, "type": "reasoning", "content": [],
                   "rawContent": [{"type": "reasoning_text", "text": e.text}]}
        elif e.kind == KIND_TOOL_CALL:
            obj = {"id": uuid7(ts), "timestamp": ts, "type": "function_call",
                   "callId": e.call_id or new_uuid(), "name": e.name or "tool",
                   "arguments": e.args or "{}"}
        elif e.kind == KIND_TOOL_RESULT:
            obj = {"id": uuid7(ts), "timestamp": ts, "type": "function_call_result",
                   "callId": e.call_id or "", "name": e.name or "tool",
                   "status": e.status or "completed",
                   "output": {"type": "text", "text": e.output or ""}}
        else:
            continue
        if last_id:
            obj["parentId"] = last_id
        last_id = obj["id"]
        obj["sessionId"] = sid
        obj["cwd"] = cwd
        lines.append(json.dumps(obj, ensure_ascii=False))
    return "\n".join(lines) + "\n"


def _template_row(c):
    r = c.execute("select * from sessions where deleted_at is null "
                  "order by updated_at desc limit 1").fetchone()
    if not r:
        raise RuntimeError("WorkBuddy 侧没有任何既有会话，缺少可复制的模板行")
    return dict(r)


def write_conversation(db_path, projects_dir, conv, cwd, existing_id=None, old_cwd=None):
    """existing_id 为空 -> 新建；否则覆盖内容。返回 (id, updated, 事件数)。

    old_cwd：覆盖时如果工作区目录变了，用它把旧 jsonl 删掉并把 sessions.cwd 改过来，
    否则 App 里会同时看到「两个同名工作区」的分组。
    """
    path_dir = os.path.join(projects_dir, slugify_path(cwd))
    os.makedirs(path_dir, exist_ok=True)
    c = connect(db_path)
    try:
        if existing_id:
            sid = existing_id
            text = _render_jsonl(conv, sid, cwd)
            _atomic_write(os.path.join(path_dir, sid + ".jsonl"), text)
            updated = max(conv.updated or 0, int(time.time() * 1000) if not conv.updated else 0)
            with c:
                c.execute("update sessions set title=?, cwd=?, updated_at=?, last_activity_at=?, "
                          "status='completed' where id=?",
                          (conv.title, cwd, updated, conv.updated or updated, sid))
            if old_cwd and slugify_path(old_cwd) != slugify_path(cwd):
                old_path = jsonl_path(projects_dir, old_cwd, sid)
                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass
            return sid, updated, len(conv.events)

        sid = new_uuid()
        text = _render_jsonl(conv, sid, cwd)
        _atomic_write(os.path.join(path_dir, sid + ".jsonl"), text)
        row = _template_row(c)
        cols = list(row.keys())
        row.update({
            "id": sid, "cwd": cwd, "title": conv.title or "(同步会话)",
            "custom_title": None, "status": "completed",
            "created_at": conv.created or int(time.time() * 1000),
            "updated_at": conv.updated or int(time.time() * 1000),
            "last_activity_at": conv.updated or int(time.time() * 1000),
            "deleted_at": None, "is_playground": 0, "source_mode": "craft", "mode": "craft",
            "unread": 0, "agent_dirty": None, "is_background_automation": None,
            "expert_id": None, "group_id": None, "conversation_origin": "agentsync",
        })
        with c:
            c.execute(f"insert or replace into sessions ({','.join(cols)}) "
                      f"values ({','.join('?' * len(cols))})", [row[k] for k in cols])
        return sid, row["updated_at"], len(conv.events)
    finally:
        c.close()


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def soft_delete(db_path, sid):
    """按 WorkBuddy 自己的方式软删除（它靠 deleted_at 标记，不是物理删除）。"""
    c = connect(db_path)
    try:
        with c:
            c.execute("update sessions set deleted_at=? where id=?", (int(time.time() * 1000), sid))
    finally:
        c.close()


def delete_conversation(db_path, projects_dir, sid, cwd):
    c = connect(db_path)
    try:
        with c:
            c.execute("delete from sessions where id=?", (sid,))
        p = jsonl_path(projects_dir, cwd, sid)
        if os.path.exists(p):
            os.remove(p)
    finally:
        c.close()
